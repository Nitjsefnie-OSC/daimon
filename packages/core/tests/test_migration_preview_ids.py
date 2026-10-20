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
HEAD = "0117_routine_sandbox_tools"
CHAIN = [
    MAIN,
    "0111_cold_dm_agent",
    "0112_routine_dm_destination",
    "0113_unattended_turn_provenance",
    "0114_turn_attendance",
    "0115_origin_session_grants",
    "0116_thread_model_settings",
    HEAD,
]
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


LEGACY = (
    "0110_cold_dm_agent",
    "0111_routine_dm_destination",
    "0112_unattended_turn_provenance",
    "0113_microsoft_accounts",
)


@pytest.mark.parametrize("start", ["empty", "unversioned", MAIN, *LEGACY])
@pytest.mark.parametrize("read_only", [False, True])
async def test_diagnostics_never_write_even_without_a_version_table(
    migration_database: AsyncEngine,
    tmp_path: Path,
    start: str,
    read_only: bool,
) -> None:
    historical = _historical_scripts(tmp_path)
    canonical = tmp_path / "canonical"
    shutil.copytree(SCRIPTS, canonical, ignore=shutil.ignore_patterns("__pycache__"))
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(canonical, sync)
            if start == "unversioned":
                command.upgrade(config, FORK)
                sync.execute(text("DROP TABLE alembic_version"))
                sync.commit()
            elif start != "empty":
                command.upgrade(config if start == MAIN else _config(historical, sync), start)
            tables_before = set(inspect(sync).get_table_names())
            stamp_before = (
                sync.scalar(text("SELECT version_num FROM alembic_version"))
                if "alembic_version" in tables_before
                else None
            )
            sync.commit()
            if read_only:
                sync.execute(text("SET default_transaction_read_only = on"))
                sync.commit()
            statements: list[str] = []
            recorder = _record_sql(statements)
            event.listen(sync, "before_cursor_execute", recorder)
            try:
                # Graph-only commands must not enter env.py or contact the DB.
                command.history(config)
                command.heads(config)
                assert not statements
                if start in LEGACY:
                    diagnostics: list[Callable[[], object]] = [
                        lambda: command.current(config),
                        lambda: command.history(config, rev_range="current:head"),
                        lambda: command.history(config, indicate_current=True),
                        lambda: command.check(config),
                        lambda: command.stamp(config, MAIN),
                        lambda: command.stamp(config, MAIN, purge=True),
                        lambda: command.ensure_version(config),
                        lambda: command.revision(
                            config, message="must not write", autogenerate=True
                        ),
                        lambda: command.upgrade(config, f"{start}:head", sql=True),
                        lambda: command.downgrade(config, f"{start}:{MAIN}", sql=True),
                    ]
                    for action in diagnostics:
                        with pytest.raises(
                            util.CommandError, match=f"{start}.*alembic upgrade head"
                        ) as error:
                            action()
                        assert "Can't locate revision" not in str(error.value)
                else:
                    command.current(config)
                    with pytest.raises(util.CommandError):
                        command.check(config)
            finally:
                event.remove(sync, "before_cursor_execute", recorder)
            assert all(sql.lstrip().upper().startswith(("SELECT", "SHOW")) for sql in statements)
            assert set(inspect(sync).get_table_names()) == tables_before
            if stamp_before is not None:
                assert sync.scalar(text("SELECT version_num FROM alembic_version")) == stamp_before
            sync.commit()

        await connection.run_sync(exercise)


@pytest.mark.parametrize("revision", ["0092_routine_post_source", FORK, MAIN])
async def test_main_same_target_receives_no_repair_writes(
    migration_database: AsyncEngine,
    revision: str,
) -> None:
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            command.upgrade(config, revision)
            statements: list[str] = []
            recorder = _record_sql(statements)
            event.listen(sync, "before_cursor_execute", recorder)
            try:
                command.upgrade(config, revision)
            finally:
                event.remove(sync, "before_cursor_execute", recorder)
            assert statements
            assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
            assert not inspect(sync).has_table("microsoft_user_tokens") or revision == MAIN
            sync.commit()

        await connection.run_sync(exercise)


# Two independent processes intentionally bypass the deployer's single-migrator
# contract. PostgreSQL rejects one transaction; no custom lock/retry machinery.
_MIGRATOR = r"""
import asyncio
import sys
from alembic import command
from alembic.config import Config
from daimon.testing.db import build_test_engine, require_owned_sessionmaker
from sqlalchemy import text

async def main():
    engine = build_test_engine(sys.argv[1], "public")
    require_owned_sessionmaker(engine)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT set_config('application_name', :name, false)"), {"name": sys.argv[3]})
            await connection.commit()
            print("ready", flush=True)
            sys.stdin.readline()
            def migrate(sync):
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
) -> None:
    async with asyncio.timeout(3):
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
            await connection.commit()
            if pid is not None:
                return
            await asyncio.sleep(0.01)


async def test_concurrent_migrator_fails_cleanly_then_retry_succeeds(
    migration_database: AsyncEngine,
    tmp_path: Path,
) -> None:
    historical = _historical_scripts(tmp_path)
    async with migration_database.connect() as connection:

        def setup(sync: Connection) -> None:
            command.upgrade(_config(historical, sync), "0111_routine_dm_destination")
            sync.execute(
                text("INSERT INTO tenants (platform, external_id) VALUES ('discord', 'concurrent')")
            )
            sync.execute(
                text(
                    "INSERT INTO direct_message_policies (tenant_id, enabled, dm_agent_name) SELECT id, true, 'private' FROM tenants"
                )
            )
            sync.commit()

        await connection.run_sync(setup)
    processes: list[asyncio.subprocess.Process] = []
    url = migration_database.url.render_as_string(hide_password=False)
    try:
        # Preload both interpreters before taking a DDL barrier: the migration's
        # five-second lock_timeout must not include Python import/startup time.
        for label in ("migration-first", "migration-second"):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _MIGRATOR,
                url,
                str(ROOT / "alembic.ini"),
                label,
                cwd=ROOT,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            processes.append(process)
            assert process.stdout is not None
            async with asyncio.timeout(60):
                while True:
                    line = await process.stdout.readline()
                    assert line, "migrator exited before ready"
                    if line == b"ready\n":
                        break
        async with (
            migration_database.connect() as barrier,
            migration_database.connect() as observer,
        ):
            observer = await observer.execution_options(isolation_level="AUTOCOMMIT")
            await barrier.execute(text("LOCK TABLE tenants IN ACCESS EXCLUSIVE MODE"))
            first, second = processes
            assert first.stdin is not None and second.stdin is not None
            first.stdin.write(b"go\n")
            await first.stdin.drain()
            await _wait_for_backend(observer, "migration-first", "relation", first)
            second.stdin.write(b"go\n")
            await second.stdin.drain()
            await _wait_for_backend(observer, "migration-second", "transactionid", second)
            await barrier.rollback()
            outputs = await asyncio.wait_for(
                asyncio.gather(*(p.communicate() for p in processes)), 60
            )
            assert sorted(p.returncode for p in processes if p.returncode is not None) == [0, 1]
            loser = next(
                output
                for p, (output, _) in zip(processes, outputs, strict=True)
                if p.returncode == 1
            )
            assert b"duplicate key" in loser or b"already exists" in loser, loser.decode()
        async with migration_database.connect() as connection:

            def verify_and_retry(sync: Connection) -> None:
                _assert_head(sync)
                assert (
                    sync.scalar(text("SELECT dm_agent_name FROM direct_message_policies"))
                    == "private"
                )
                sync.commit()
                command.upgrade(_config(SCRIPTS, sync), "head")
                _assert_head(sync)

            await connection.run_sync(verify_and_retry)
    finally:
        for process in processes:
            if process.returncode is None:
                process.terminate()
                await asyncio.wait_for(process.communicate(), 10)


@pytest.mark.parametrize("start", ["empty", MAIN])
@pytest.mark.parametrize("operation", ["stamp", "ensure_version"])
async def test_bookkeeping_sql_matches_origin_main(
    migration_database: AsyncEngine,
    tmp_path: Path,
    start: str,
    operation: str,
) -> None:
    # Frozen origin/main env; only the supplied-connection test entrypoint is
    # adapted. Use the same graph and connection to compare every SQL statement
    # AND bound parameter, without differences from driver/bootstrap setup.
    baseline = tmp_path / "main"
    shutil.copytree(SCRIPTS, baseline, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(Path(__file__).parent / "fixtures/preview_alembic_env.py", baseline / "env.py")
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            if start == MAIN:
                command.upgrade(_config(SCRIPTS, sync), MAIN)
            tables_before = set(inspect(sync).get_table_names())
            sync.commit()
            traces: list[list[tuple[str, str]]] = []
            for scripts in (baseline, SCRIPTS):
                if start == "empty":
                    sync.execute(text("DROP TABLE IF EXISTS alembic_version"))
                else:
                    sync.execute(
                        text("UPDATE alembic_version SET version_num = :revision"),
                        {"revision": MAIN},
                    )
                sync.commit()
                statements: list[tuple[str, str]] = []

                def record(
                    _conn: Connection,
                    _cursor: object,
                    sql: str,
                    parameters: object,
                    _context: ExecutionContext,
                    _executemany: bool,
                    _statements: list[tuple[str, str]] = statements,
                ) -> None:
                    _statements.append((sql, repr(parameters)))

                event.listen(sync, "before_cursor_execute", record)
                try:
                    config = _config(scripts, sync)
                    if operation == "stamp":
                        # Preserve the documented initial-schema squash recipe.
                        command.stamp(config, "0001_initial_schema")
                    else:
                        command.ensure_version(config)
                finally:
                    event.remove(sync, "before_cursor_execute", record)
                traces.append(statements)
                expected = (
                    "0001_initial_schema"
                    if operation == "stamp"
                    else (MAIN if start == MAIN else None)
                )
                assert sync.scalar(text("SELECT version_num FROM alembic_version")) == expected
                assert set(inspect(sync).get_table_names()) == tables_before | {"alembic_version"}
                sync.commit()
            assert traces[0]
            assert traces[1] == traces[0]

        await connection.run_sync(exercise)


@pytest.mark.parametrize("start", ["empty", MAIN, "0111_cold_dm_agent"])
async def test_purge_stamp_preserves_normal_nonlegacy_bookkeeping(
    migration_database: AsyncEngine,
    start: str,
) -> None:
    async with migration_database.connect() as connection:

        def exercise(sync: Connection) -> None:
            config = _config(SCRIPTS, sync)
            if start != "empty":
                command.upgrade(config, start)
            tables_before = set(inspect(sync).get_table_names())
            sync.commit()
            command.stamp(config, "0001_initial_schema", purge=True)
            assert (
                sync.scalar(text("SELECT version_num FROM alembic_version"))
                == "0001_initial_schema"
            )
            assert set(inspect(sync).get_table_names()) == tables_before | {"alembic_version"}
            sync.commit()
            command.ensure_version(config)
            assert (
                sync.scalar(text("SELECT version_num FROM alembic_version"))
                == "0001_initial_schema"
            )
            sync.commit()

        await connection.run_sync(exercise)
