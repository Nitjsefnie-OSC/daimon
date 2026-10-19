"""Async Alembic env; DSN from DAIMON_DATABASE_URL or DAIMON_DATABASE__URL.

Invariant: main revisions receive no repair writes. Only online upgrade/downgrade
may atomically repair a stored legacy preview stamp in the migration transaction.
Every other command is non-mutating, even on an empty/unversioned database.
Online stamp/ensure_version bookkeeping is therefore rejected; use --sql to
inspect bookkeeping SQL. Plain history/heads remain graph-only. Commands that
enter this env diagnose legacy stamps without repairing; old literal targets and
offline legacy ranges are unsupported: run ``alembic upgrade head`` online first.

The DEPLOYER serializes migrations; this environment adds no advisory lock or
retry loop. Lane: .github/workflows/lane.yml:46, group lane-<lane>. Staging/prod:
.github/workflows/deploy.yml:66, group deploy-gcp-<environment>, with the blocking
Cloud Run migrate job at :206-210. These callers must run one migrator at a time.
A concurrent manual run can fail with ordinary PostgreSQL DDL conflicts; after
the other invocation finishes, retry normally, as on main.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context, util
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

LEGACY_REVISIONS = {
    "0110_cold_dm_agent": "0111_cold_dm_agent",
    "0111_routine_dm_destination": "0112_routine_dm_destination",
    "0112_unattended_turn_provenance": "0113_unattended_turn_provenance",
    "0113_microsoft_accounts": "0113_unattended_turn_provenance",
}


def _legacy_error(revision: str) -> util.CommandError:
    return util.CommandError(
        f"Legacy preview revision '{revision}' cannot be used for this command. "
        "Run `alembic upgrade head` online to reconcile the disposable preview database first. "
        "Only online upgrade/downgrade from a stored legacy stamp is supported; "
        "use canonical revision IDs for targets and offline SQL."
    )


def _reject_legacy_targets() -> None:
    opts = context.get_context().opts
    for option in ("starting_rev", "destination_rev"):
        value = str(opts.get(option, ""))
        for revision in LEGACY_REVISIONS:
            if revision in value:
                raise _legacy_error(revision)


def _is_migration_command() -> bool:
    # destination_rev is also set by stamp and autogenerate. Recognize the
    # actual Alembic command callbacks instead, for both CLI and command API.
    fn = context.get_context().opts.get("fn")
    return getattr(fn, "__module__", None) == "alembic.command" and getattr(
        fn, "__name__", None
    ) in ("upgrade", "downgrade")


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    _reject_legacy_targets()
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    _reject_legacy_targets()
    migrating = _is_migration_command()
    if not migrating:
        # Alembic otherwise creates alembic_version on an empty database even
        # when the inspection callback yields no migration steps.
        context.configure(connection=connection, target_metadata=target_metadata, dont_mutate=True)
    with context.begin_transaction():
        if migrating:
            _normalize_preview_revision(connection)
        else:
            for revision in context.get_context().get_current_heads():
                if revision in LEGACY_REVISIONS:
                    raise _legacy_error(revision)
            fn = context.get_context().opts.get("fn")
            if getattr(fn, "__name__", None) in ("do_stamp", "do_ensure_version"):
                raise util.CommandError(
                    "Online stamp/ensure_version is disabled: only alembic upgrade/downgrade "
                    "may write database state. Use --sql to inspect bookkeeping SQL."
                )
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
    for old_revision in revisions:
        new_revision = LEGACY_REVISIONS.get(old_revision)
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
