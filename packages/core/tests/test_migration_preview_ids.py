"""Exercise deployed main/preview stamps through real Alembic and PostgreSQL.

The fixture scripts are frozen from 7df20d82c, before the ID repair. Building
those historical schemas proves compatibility without imitating them by merely
stamping a database that already has the new schema.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from alembic import command, util
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from daimon.core._models import Base
from daimon.testing.db import DatabasePreflight, build_test_engine, require_owned_sessionmaker
from sqlalchemy import Connection, Text, event, inspect, text
from sqlalchemy.engine import ExecutionContext, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "packages/core/alembic"
FORK = "0109_github_tokens_live_index"
MAIN = "0110_microsoft_accounts"
HEAD = "0113_unattended_turn_provenance"
CHAIN = [MAIN, "0111_cold_dm_agent", "0112_routine_dm_destination", HEAD]
TABLES = {
    "microsoft_user_tokens",
    "microsoft_sign_ins",
    "direct_message_policies",
    "routines",
}


def _config(scripts: Path, connection: Connection) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(scripts))
    config.set_main_option(
        "sqlalchemy.url",
        connection.engine.url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    config.attributes["connection"] = connection
    return config


def _historical_scripts(tmp_path: Path) -> Path:
    scripts = tmp_path / "historical"
    versions = scripts / "versions"
    versions.mkdir(parents=True)
    shutil.copy(Path(__file__).parent / "fixtures/preview_alembic_env.py", scripts / "env.py")
    for source in (SCRIPTS / "versions").glob("*.py"):
        if source.stem not in CHAIN:
            shutil.copy(source, versions / source.name)
    for source in (Path(__file__).parent / "fixtures/preview_migrations").glob("*.py"):
        shutil.copy(source, versions / source.name)
    return scripts


def _assert_head(connection: Connection) -> None:
    assert connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
    assert not connection.scalar(
        text(
            "SELECT EXISTS (SELECT FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_backend_pid())"
        )
    )

    def include_name(name: str | None, kind: str, _parents: dict[str, str | None]) -> bool:
        return kind != "table" or name in TABLES

    def include_object(
        _obj: object,
        name: str | None,
        kind: str,
        _reflected: bool,
        _compare_to: object,
    ) -> bool:
        return kind != "table" or name in TABLES

    migration_context = MigrationContext.configure(
        connection, opts={"include_name": include_name, "include_object": include_object}
    )
    assert not compare_metadata(migration_context, Base.metadata)
    inspector = inspect(connection)
    constraints = {c["name"] for c in inspector.get_check_constraints("routines")}
    assert {"ck_routines_destination_kind", "ck_routines_dm_owner"} <= constraints
    # The usage table retains legacy repair columns intentionally absent from
    # the ORM. Compare only the two columns introduced by this chain.
    usage_columns = {c["name"]: c for c in inspector.get_columns("usage_reconcile_progress")}
    for name in ("unattended_turn_event_id", "unattended_rules_channel_id"):
        assert isinstance(usage_columns[name]["type"], Text)
        assert usage_columns[name]["nullable"]
    connection.commit()


def test_authoritative_chain() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == [HEAD]
    parent = FORK
    for revision in CHAIN:
        node = script.get_revision(revision)
        assert node is not None and node.down_revision == parent
        assert Path(node.path).stem == revision
        parent = revision


@pytest.fixture
async def migration_database(db_preflight: DatabasePreflight) -> AsyncIterator[AsyncEngine]:
    """An owned empty database, independent of the ORM-based shared fixtures."""
    database = f"daimon_test_nc_migration_{os.getpid()}_{secrets.token_hex(4)}"
    admin = build_test_engine(db_preflight.url, db_preflight.schema)
    require_owned_sessionmaker(admin)
    async with admin.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{database}"'))
    url = make_url(db_preflight.url).set(database=database).render_as_string(hide_password=False)
    engine = build_test_engine(url, "public")
    require_owned_sessionmaker(engine)
    try:
        yield engine
    finally:
        await engine.dispose()
        try:
            async with admin.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        finally:
            await admin.dispose()


@pytest.mark.parametrize(
    "start",
    [
        "fresh",
        MAIN,
        "0111_routine_dm_destination",
        "0113_microsoft_accounts",
        "0110_cold_dm_agent",
        "0112_unattended_turn_provenance",
    ],
)
async def test_deployed_upgrade_and_downgrade_paths(
    migration_database: AsyncEngine,
    tmp_path: Path,
    start: str,
) -> None:
    historical = _historical_scripts(tmp_path)
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            if start != "fresh":
                old_config = config if start == MAIN else _config(historical, sync)
                command.upgrade(old_config, start)
                assert sync.scalar(text("SELECT version_num FROM alembic_version")) == start
                # Sentinel rows prove the remap does not replay destructive DDL.
                sync.execute(
                    text(
                        "INSERT INTO tenants (platform, external_id) "
                        "VALUES ('discord', 'migration-test')"
                    )
                )
                if start != MAIN:
                    sync.execute(
                        text(
                            "INSERT INTO direct_message_policies "
                            "(tenant_id, enabled, dm_agent_name) "
                            "SELECT id, true, 'private' FROM tenants"
                        )
                    )
                if start in (
                    "0111_routine_dm_destination",
                    "0112_unattended_turn_provenance",
                    "0113_microsoft_accounts",
                ):
                    sync.execute(
                        text(
                            "INSERT INTO routines (tenant_id, created_by_user_id, agent_id, "
                            "agent_name, cron_expr, trigger_message, destination_kind, "
                            "destination_id, source_provenance_known, source_channel_id, source_thread_id) "
                            "SELECT id, 'owner', 'ag', 'private', '0 9 * * *', 'hello', 'dm', "
                            "'owner', true, 'source-channel', 'source-thread' FROM tenants"
                        )
                    )
                if start in (MAIN, "0113_microsoft_accounts"):
                    sync.execute(
                        text(
                            "INSERT INTO microsoft_user_tokens "
                            "(tenant_id, user_id, encrypted_refresh_token, scopes, "
                            "created_at, updated_at) "
                            "SELECT id, 'owner', decode('cafe', 'hex'), 'scope', "
                            "now(), now() FROM tenants"
                        )
                    )
                sync.commit()
            command.upgrade(config, "head")
            _assert_head(sync)
            if start not in ("fresh", MAIN):
                assert (
                    sync.scalar(text("SELECT dm_agent_name FROM direct_message_policies"))
                    == "private"
                )
            if start in (MAIN, "0113_microsoft_accounts"):
                assert sync.scalar(
                    text("SELECT encrypted_refresh_token FROM microsoft_user_tokens")
                ) == bytes.fromhex("cafe")
            sync.commit()
            # A repeat upgrade is a no-op after translation.
            command.upgrade(config, "head")
            _assert_head(sync)
            if start in (
                "0111_routine_dm_destination",
                "0112_unattended_turn_provenance",
                "0113_microsoft_accounts",
            ):
                assert sync.execute(
                    text(
                        "SELECT destination_kind, destination_id, source_provenance_known, "
                        "source_channel_id, source_thread_id FROM routines"
                    )
                ).one() == ("dm", "owner", True, "source-channel", "source-thread")
                sync.commit()
                with pytest.raises(RuntimeError, match="convert or remove DM routines"):
                    command.downgrade(config, MAIN)
                # The failed downgrade rolls back the provenance downgrade too.
                _assert_head(sync)
                sync.execute(text("DELETE FROM routines"))
                sync.commit()
            # Each new revision reverses, including Microsoft's tables.
            for target in reversed([FORK, *CHAIN[:-1]]):
                command.downgrade(config, target)
                assert sync.scalar(text("SELECT version_num FROM alembic_version")) == target
                inspector = inspect(sync)
                if target == "0112_routine_dm_destination":
                    assert "unattended_turn_event_id" not in {
                        c["name"] for c in inspector.get_columns("usage_reconcile_progress")
                    }
                if target == "0111_cold_dm_agent":
                    assert "source_provenance_known" not in {
                        c["name"] for c in inspector.get_columns("routines")
                    }
                if target == MAIN:
                    assert "dm_agent_name" not in {
                        c["name"] for c in inspector.get_columns("direct_message_policies")
                    }
                    assert inspector.has_table("microsoft_user_tokens")
                if target == FORK:
                    assert not inspector.has_table("microsoft_user_tokens")
                    assert not inspector.has_table("microsoft_sign_ins")
                sync.commit()
            command.upgrade(config, "head")
            _assert_head(sync)

        await connection.run_sync(exercise)


def _record_sql(statements: list[str]) -> Callable[..., None]:
    def record(
        _conn: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    return record


@pytest.mark.parametrize("start", [MAIN, "0111_routine_dm_destination", "0113_microsoft_accounts"])
@pytest.mark.parametrize("purge", [False, True])
async def test_stamp_never_runs_repair_ddl(
    migration_database: AsyncEngine,
    tmp_path: Path,
    start: str,
    purge: bool,
) -> None:
    historical = _historical_scripts(tmp_path)
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            command.upgrade(config if start == MAIN else _config(historical, sync), start)
            statements: list[str] = []
            recorder = _record_sql(statements)
            event.listen(sync, "before_cursor_execute", recorder)
            try:
                if start == MAIN:
                    command.stamp(config, MAIN, purge=purge)
                else:
                    with pytest.raises(util.CommandError, match=f"{start}.*alembic upgrade head"):
                        command.stamp(config, MAIN, purge=purge)
            finally:
                event.remove(sync, "before_cursor_execute", recorder)
            assert not [
                sql
                for sql in statements
                if sql.lstrip().split()[0].upper()
                in {"CREATE", "ALTER", "DROP", "TRUNCATE", "COMMENT", "GRANT", "REVOKE", "DO"}
            ]
            assert sync.scalar(text("SELECT version_num FROM alembic_version")) == start
            assert inspect(sync).has_table("microsoft_user_tokens") == (
                start != "0111_routine_dm_destination"
            )
            sync.commit()

        await connection.run_sync(exercise)


@pytest.mark.parametrize("start", ["0111_routine_dm_destination", "0113_microsoft_accounts"])
async def test_legacy_inspection_fails_read_only_with_upgrade_instructions(
    migration_database: AsyncEngine,
    tmp_path: Path,
    start: str,
) -> None:
    historical = _historical_scripts(tmp_path)
    canonical = tmp_path / "canonical"
    shutil.copytree(SCRIPTS, canonical, ignore=shutil.ignore_patterns("__pycache__"))
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            command.upgrade(_config(historical, sync), start)
            config = _config(canonical, sync)
            # A broken revision command may only write into the temporary copy.
            files_before = set((canonical / "versions").glob("*.py"))
            sync.execute(text("SET default_transaction_read_only = on"))
            sync.commit()
            actions: list[Callable[[], object]] = [
                lambda: command.current(config),
                lambda: command.history(config),
                lambda: command.history(config, rev_range="current:head"),
                lambda: command.history(config, indicate_current=True),
                lambda: command.check(config),
                lambda: command.revision(config, message="must not write"),
                lambda: command.revision(config, message="must not write", autogenerate=True),
                lambda: command.upgrade(config, f"{start}:head", sql=True),
                lambda: command.downgrade(config, f"{start}:{MAIN}", sql=True),
                lambda: command.upgrade(config, start),
                lambda: command.downgrade(config, start),
            ]
            statements: list[str] = []
            recorder = _record_sql(statements)
            event.listen(sync, "before_cursor_execute", recorder)
            try:
                for action in actions:
                    with pytest.raises(
                        util.CommandError, match=f"{start}.*alembic upgrade head"
                    ) as error:
                        action()
                    assert "Can't locate revision" not in str(error.value)
            finally:
                event.remove(sync, "before_cursor_execute", recorder)
            assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
            assert sync.scalar(text("SELECT version_num FROM alembic_version")) == start
            assert inspect(sync).has_table("microsoft_user_tokens") == (
                start == "0113_microsoft_accounts"
            )
            assert set((canonical / "versions").glob("*.py")) == files_before
            sync.commit()

        await connection.run_sync(exercise)


# Separate Python interpreters exercise the real environment/command API on
# independent sessions. Assert the session lock during each concurrent-index
# DDL statement, after autocommit_block has committed the migration transaction.
_MIGRATOR = r"""
import asyncio
import sys
from alembic import command
from alembic.config import Config
from daimon.testing.db import build_test_engine, require_owned_sessionmaker
from sqlalchemy import event, text

async def main():
    engine = build_test_engine(sys.argv[1], "public")
    require_owned_sessionmaker(engine)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT set_config('application_name', :name, false)"), {"name": sys.argv[3]})
            await connection.commit()
            def migrate(sync):
                def check_lock(conn, cursor, statement, parameters, context, executemany):
                    if statement.lstrip().startswith("SELECT pg_try_advisory_lock"):
                        print("migration-lock-attempt", flush=True)
                    if "SELECT alembic_version.version_num" in statement:
                        print("migration-head-read", flush=True)
                    if statement.lstrip().startswith("CREATE INDEX CONCURRENTLY"):
                        assert conn.scalar(text("SELECT EXISTS (SELECT FROM pg_locks WHERE locktype = 'advisory' AND granted AND pid = pg_backend_pid())"))
                        print("concurrent-index-session-lock", flush=True)
                event.listen(sync, "after_cursor_execute", check_lock)
                config = Config(sys.argv[2])
                config.attributes["connection"] = sync
                command.upgrade(config, "head")
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()
asyncio.run(main())
"""


async def _wait_for_backend(
    connection: AsyncConnection,
    name: str,
    wait_event: str,
    process: asyncio.subprocess.Process,
) -> int:
    async with asyncio.timeout(60):
        while True:
            if process.returncode is not None:
                output, _ = await process.communicate()
                raise AssertionError(output.decode())
            pid = await connection.scalar(
                text(
                    "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
                    "AND application_name = :name AND wait_event = :event"
                ),
                {"name": name, "event": wait_event},
            )
            # pg_stat_activity snapshots are cached until transaction end.
            await connection.commit()
            if pid is not None:
                assert isinstance(pid, int)
                return pid
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("start", ["0092_routine_post_source", MAIN, "0111_routine_dm_destination"])
async def test_two_processes_serialize_before_head_read_through_autocommit(
    migration_database: AsyncEngine,
    tmp_path: Path,
    start: str,
) -> None:
    historical = _historical_scripts(tmp_path)
    async with migration_database.connect() as connection:
        await connection.run_sync(
            lambda sync: command.upgrade(
                _config(historical if start == "0111_routine_dm_destination" else SCRIPTS, sync),
                start,
            )
        )
    processes: list[asyncio.subprocess.Process] = []
    url = migration_database.url.render_as_string(hide_password=False)
    try:
        async with (
            migration_database.connect() as barrier,
            migration_database.connect() as observer,
        ):
            # Monitoring must not retain snapshots that concurrent index builds
            # legitimately wait for; only the barrier owns a transaction.
            observer = await observer.execution_options(isolation_level="AUTOCOMMIT")
            # Stall the first head read. A correct migrator already holds the
            # session advisory lock here; a second must wait on that lock.
            await barrier.execute(text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE"))
            first = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _MIGRATOR,
                url,
                str(ROOT / "alembic.ini"),
                "migration-first",
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            processes.append(first)
            first_pid = await _wait_for_backend(observer, "migration-first", "relation", first)
            assert await observer.scalar(
                text(
                    "SELECT EXISTS (SELECT FROM pg_locks WHERE pid = :pid "
                    "AND locktype = 'advisory' AND granted)"
                ),
                {"pid": first_pid},
            )
            await observer.commit()
            second = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _MIGRATOR,
                url,
                str(ROOT / "alembic.ini"),
                "migration-second",
                cwd=ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            processes.append(second)
            # Observe retries rather than a blocked SQL lock call: blocking
            # SELECT pg_advisory_lock retains a snapshot and deadlocks concurrent
            # index builds. The waiting process must not read a head yet.
            second_prefix = bytearray()
            assert second.stdout is not None
            async with asyncio.timeout(60):
                attempts = 0
                while attempts < 2:
                    line = await second.stdout.readline()
                    assert line, second_prefix.decode()
                    second_prefix.extend(line)
                    assert line != b"migration-head-read\n"
                    if line == b"migration-lock-attempt\n":
                        attempts += 1
            await barrier.rollback()
            outputs = await asyncio.wait_for(
                asyncio.gather(*(process.communicate() for process in processes)), timeout=120
            )
            outputs[1] = (bytes(second_prefix) + outputs[1][0], outputs[1][1])
            for process, (output, _) in zip(processes, outputs, strict=True):
                assert process.returncode == 0, output.decode()
            if start == "0092_routine_post_source":
                # Two CREATE INDEX CONCURRENTLY in 0093, plus the one in 0109.
                assert outputs[0][0].count(b"concurrent-index-session-lock") >= 3
                assert b"concurrent-index-session-lock" not in outputs[1][0]
        async with migration_database.connect() as connection:
            await connection.run_sync(_assert_head)
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.communicate(), timeout=10)
