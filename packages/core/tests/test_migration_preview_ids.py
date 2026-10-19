"""Exercise deployed main/preview stamps through real Alembic and PostgreSQL.

The fixture scripts are frozen from 7df20d82c, before the ID repair. Building
those historical schemas proves compatibility without imitating them by merely
stamping a database that already has the new schema.
"""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from daimon.core._models import Base
from daimon.testing.db import DatabasePreflight, build_test_engine, require_owned_sessionmaker
from sqlalchemy import Connection, Text, inspect, text
from sqlalchemy.engine import make_url

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
    config.attributes["connection"] = connection
    return config


def _historical_scripts(tmp_path: Path) -> Path:
    scripts = tmp_path / "historical"
    versions = scripts / "versions"
    versions.mkdir(parents=True)
    shutil.copy(SCRIPTS / "env.py", scripts / "env.py")
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
    db_preflight: DatabasePreflight,
    tmp_path: Path,
    start: str,
) -> None:
    database = f"daimon_test_nc_migration_{os.getpid()}_{secrets.token_hex(4)}"
    admin = build_test_engine(db_preflight.url, db_preflight.schema)
    require_owned_sessionmaker(admin)
    async with admin.connect() as connection:
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text(f'CREATE DATABASE "{database}"'))
    url = make_url(db_preflight.url).set(database=database).render_as_string(hide_password=False)
    engine = build_test_engine(url, "public")
    require_owned_sessionmaker(engine)
    historical = _historical_scripts(tmp_path)
    try:
        async with engine.connect() as connection:

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
    finally:
        await engine.dispose()
        try:
            async with admin.connect() as connection:
                connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                await connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        finally:
            await admin.dispose()
