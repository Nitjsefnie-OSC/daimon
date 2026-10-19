"""Async Alembic env. Reads DSN from DAIMON_DATABASE_URL or DAIMON_DATABASE__URL env var."""

from __future__ import annotations

import asyncio
import os

from alembic import context
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from daimon.core._models import Base
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

db_url = os.environ.get("DAIMON_DATABASE_URL") or os.environ.get("DAIMON_DATABASE__URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)
elif not config.get_main_option("sqlalchemy.url") and "connection" not in config.attributes:
    # Flat DAIMON_DATABASE_URL is intentional — alembic CLI runs outside
    # pydantic-settings. The app uses nested DAIMON_DATABASE__URL.
    raise RuntimeError(
        "No database URL configured. Set DAIMON_DATABASE__URL "
        "(or DAIMON_DATABASE_URL for backwards compat) or sqlalchemy.url in alembic.ini."
    )

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        # Inspection commands such as `alembic current` must remain read-only.
        if "destination_rev" in context.get_context().opts:
            _normalize_preview_revision(connection)
        context.run_migrations()


def _normalize_preview_revision(connection: Connection) -> None:
    """Translate deployed preview stamps to the main-authoritative chain atomically.

    The old preview chain applied the DM/provenance changes before Microsoft
    accounts. Install that missing predecessor before translating its stamp;
    never replay the DM migrations, which would lose data or duplicate columns.
    S1's old head already includes Microsoft accounts and needs only an alias.
    This runs inside Alembic's transaction, including for downgrade commands.
    """
    migration_context = context.get_context()
    revisions = migration_context.get_current_heads()
    aliases = {
        "0110_cold_dm_agent": "0111_cold_dm_agent",
        "0111_routine_dm_destination": "0112_routine_dm_destination",
        "0112_unattended_turn_provenance": "0113_unattended_turn_provenance",
        "0113_microsoft_accounts": "0113_unattended_turn_provenance",
    }
    for old_revision in revisions:
        new_revision = aliases.get(old_revision)
        if new_revision is None:
            continue
        if old_revision != "0113_microsoft_accounts":
            microsoft = ScriptDirectory.from_config(config).get_revision("0110_microsoft_accounts")
            assert microsoft is not None
            with Operations.context(migration_context):
                microsoft.module.upgrade()
        connection.execute(
            text("UPDATE alembic_version SET version_num = :new WHERE version_num = :old"),
            {"new": new_revision, "old": old_revision},
        )


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Migrations re-encrypt agent keys; keep bound values out of SQL errors.
        hide_parameters=True,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
elif "connection" in config.attributes:
    do_run_migrations(config.attributes["connection"])
else:
    asyncio.run(run_migrations_online())
