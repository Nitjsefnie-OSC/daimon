"""Async Alembic env; DSN from DAIMON_DATABASE_URL or DAIMON_DATABASE__URL.

Legacy preview stamps are a one-time boundary for disposable preview databases.
Only online ``upgrade``/``downgrade`` repair them, targeting canonical revisions
(or relative steps). Old IDs are not supported as command targets. Stamp and
DB-aware inspection/history/check/revision commands never repair: they reject
legacy stamps by name and direct operators to ``alembic upgrade head``. History
and revision enter this environment via ``revision_environment`` in alembic.ini.
Offline SQL never reads a database and accepts only canonical revision ranges;
legacy IDs in a supplied range get the same actionable diagnostic.

Online upgrade/downgrade hold a database-scoped session advisory lock from before
the first head read through the entire migration, including autocommit blocks.
Injected upgrade/downgrade connections must have no active transaction.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

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
# PostgreSQL advisory locks are database-scoped. A stable key serializes every
# migrator for this database, independently of other databases on the same host.
MIGRATION_LOCK = 0x4441494D4F4E  # DAIMON


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


@contextmanager
def _migration_lock(connection: Connection) -> Iterator[None]:
    if connection.in_transaction():
        raise util.CommandError(
            "Upgrade/downgrade requires a connection without an active transaction"
        )
    locked = False
    try:
        while not locked:
            locked = bool(
                connection.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK}
                )
            )
            # A blocking SELECT pg_advisory_lock holds a transaction snapshot:
            # CREATE INDEX CONCURRENTLY can wait for that snapshot while the
            # waiter waits for us. End each nonblocking attempt's transaction
            # before sleeping, so queued migrators cannot deadlock index builds.
            # A successfully acquired session lock survives this commit too.
            connection.commit()
            if not locked:
                time.sleep(0.1)
        yield
    finally:
        # An error may have aborted a transaction; release on the same session
        # without hiding the migration error behind InFailedSQLTransaction.
        connection.rollback()
        # A dead session already released its locks; do not reconnect just to
        # unlock, or mask the original disconnect with a new connection error.
        if locked and not connection.closed and not connection.invalidated:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK})
            connection.commit()


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
    if _is_migration_command():
        with _migration_lock(connection), context.begin_transaction():
            _normalize_preview_revision(connection)
            context.run_migrations()
    else:
        with context.begin_transaction():
            # Read the stored IDs before Alembic tries to resolve deleted nodes.
            # In particular, stamp (including --purge) must never run repair DDL.
            for revision in context.get_context().get_current_heads():
                if revision in LEGACY_REVISIONS:
                    raise _legacy_error(revision)
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
