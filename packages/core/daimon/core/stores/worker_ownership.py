"""Liveness of adapter processes holding a PostgreSQL session advisory lock."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import structlog
from asyncpg import (  # pyright: ignore[reportMissingTypeStubs]
    InterfaceError,
    InternalClientError,
    PostgresError,
)
from daimon.core.stores.discord_gateway_lease import GatewayLease, GatewayLeaseLost
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

# From the last committed heartbeat, stop occurs within probe interval + probe
# timeout + reconnect budget + hard exit. The margin covers event-loop scheduling
# and connection teardown; this timing bound is required for exclusion.
OWNER_PROBE_INTERVAL_S = 5
OWNER_PROBE_TIMEOUT_S = 5
OWNER_RETRY_BUDGET_S = 30
OWNER_HARD_EXIT_S = 100
OWNER_SCHEDULING_MARGIN_S = 40
OWNER_LOSS_GRACE_S = (
    OWNER_PROBE_INTERVAL_S
    + OWNER_PROBE_TIMEOUT_S
    + OWNER_RETRY_BUDGET_S
    + OWNER_HARD_EXIT_S
    + OWNER_SCHEDULING_MARGIN_S
)


async def owner_is_alive(session: AsyncSession | AsyncConnection, owner_key: int) -> bool:
    """Keep a disconnected owner protected until its fail-stop bound expires."""
    return bool(
        await session.scalar(
            text("""
        SELECT EXISTS (
            SELECT 1 FROM pg_locks
            WHERE locktype = 'advisory' AND granted AND objsubid = 1
              AND database = (SELECT oid FROM pg_database
                              WHERE datname = current_database())
              AND classid = CAST(:upper AS oid) AND objid = CAST(:lower AS oid)
        ) OR EXISTS (
            SELECT 1 FROM discord_worker_heartbeats
            WHERE owner_key = :key
              AND seen_at > clock_timestamp() - (:grace * interval '1 second')
        )
    """),
            {
                "upper": owner_key >> 32,
                "lower": owner_key & 0xFFFFFFFF,
                "key": owner_key,
                "grace": OWNER_LOSS_GRACE_S,
            },
        )
    )


log = structlog.get_logger(__name__)


class OwnershipExpired(Exception):
    """A replacement may already have reclaimed this owner's work."""


class GatewayLeaseExpired(OwnershipExpired, GatewayLeaseLost):
    """Another process may already have taken the Discord gateway."""


@asynccontextmanager
async def hold_worker_owner(
    engine: AsyncEngine,
    *,
    owner_key: int,
    ownership_lost: asyncio.Event,
    probe_interval_s: float = OWNER_PROBE_INTERVAL_S,
    gateway_lock_key: int | None = None,
) -> AsyncIterator[GatewayLease]:
    """Retry brief owner-connection loss; fail stop before reclaim becomes safe.

    Yields the gateway lease that shares this owner connection: a lease that
    cannot be re-confirmed after a reconnect fails stop like an expired owner.
    """
    # asyncpg failures can surface as raw socket errors, not SQLAlchemyError.
    failures = (
        SQLAlchemyError,
        InterfaceError,
        InternalClientError,
        PostgresError,
        OSError,
        TimeoutError,
    )

    connection: AsyncConnection | None = None
    # Serialises the heartbeat probe, reconnects and gateway lease queries on the one connection.
    connection_lock = asyncio.Lock()
    lease = GatewayLease(
        owner_key,
        connection=lambda: connection,
        lock=connection_lock,
        **({} if gateway_lock_key is None else {"lock_key": gateway_lock_key}),
    )

    async def acquire(*, reconnect: bool = False) -> AsyncConnection:
        candidate = await engine.connect()
        try:
            owned = await candidate.scalar(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": owner_key}
            )
            await candidate.commit()
            if not owned:
                raise SQLAlchemyError("worker owner key is still held")
            if reconnect and not await candidate.scalar(
                text("""SELECT EXISTS (
                         SELECT 1 FROM discord_worker_heartbeats
                         WHERE owner_key = :key AND seen_at >
                           clock_timestamp() - (:grace * interval '1 second'))"""),
                {"key": owner_key, "grace": OWNER_LOSS_GRACE_S},
            ):
                raise OwnershipExpired("owner heartbeat expired before reacquisition")
            if reconnect and lease.held and not await lease.confirm_after_reconnect(candidate):
                raise GatewayLeaseExpired("gateway lease moved on during owner reconnect")
            await candidate.execute(
                text("""INSERT INTO discord_worker_heartbeats (owner_key, seen_at, lease_aware)
                     VALUES (:key, clock_timestamp(), true)
                     ON CONFLICT (owner_key) DO UPDATE SET
                       seen_at = clock_timestamp(), lease_aware = true"""),
                {"key": owner_key},
            )
            await candidate.commit()
            if not reconnect:
                await candidate.execute(
                    text("""DELETE FROM discord_worker_heartbeats
                         WHERE owner_key != :key AND seen_at <
                           clock_timestamp() - (:age * interval '1 second')
                           AND NOT EXISTS (
                             SELECT 1 FROM pg_locks WHERE locktype = 'advisory'
                               AND granted AND objsubid = 1
                               AND database = (SELECT oid FROM pg_database
                                               WHERE datname = current_database())
                               AND classid = CAST(discord_worker_heartbeats.owner_key >> 32 AS oid)
                               AND objid = CAST(
                                 discord_worker_heartbeats.owner_key & 4294967295 AS oid)
                           )"""),
                    {"key": owner_key, "age": 3 * OWNER_LOSS_GRACE_S},
                )
                await candidate.commit()
            return candidate
        except BaseException:
            async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S):
                await candidate.invalidate()
                await candidate.close()
            raise

    async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S):
        connection = await acquire()

    async def discard() -> None:
        nonlocal connection
        stale, connection = connection, None
        if stale is not None:
            try:
                async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S):
                    await stale.invalidate()
                    await stale.close()
            except failures as err:
                log.warning("discord.worker_owner_release_failed", error_type=type(err).__name__)

    async def guard() -> None:
        nonlocal connection
        while True:
            await asyncio.sleep(probe_interval_s)
            try:
                async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S), connection_lock:
                    assert connection is not None
                    await connection.execute(
                        text("""UPDATE discord_worker_heartbeats
                             SET seen_at = clock_timestamp() WHERE owner_key = :key"""),
                        {"key": owner_key},
                    )
                    await connection.commit()
                continue
            except Exception as err:
                log.warning("discord.worker_owner_probe_failed", error_type=type(err).__name__)
            deadline = asyncio.get_running_loop().time() + OWNER_RETRY_BUDGET_S
            try:
                async with asyncio.timeout_at(deadline), connection_lock:
                    await discard()
                    while True:
                        try:
                            async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S):
                                connection = await acquire(reconnect=True)
                            log.info("discord.worker_owner_recovered")
                            break
                        except OwnershipExpired:
                            raise
                        except failures as err:
                            log.warning(
                                "discord.worker_owner_retry_failed",
                                error_type=type(err).__name__,
                            )
                            await asyncio.sleep(1)
            except (TimeoutError, OwnershipExpired) as err:
                log.error("discord.worker_owner_lost", error_type=type(err).__name__)
                ownership_lost.set()
                await discard()
                return

    guard_task = asyncio.create_task(guard(), name="discord.worker_owner_guard")
    try:
        yield lease
    finally:
        guard_task.cancel()
        with suppress(asyncio.CancelledError):
            await guard_task
        if connection is not None:  # pyright: ignore[reportUnnecessaryComparison]  # guard can discard it
            try:
                # The lock: a lease call still on this connection must finish first.
                async with asyncio.timeout(OWNER_PROBE_TIMEOUT_S), connection_lock:
                    await connection.execute(
                        text("DELETE FROM discord_worker_heartbeats WHERE owner_key = :key"),
                        {"key": owner_key},
                    )
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(:key)"), {"key": owner_key}
                    )
                    await connection.commit()
                    await connection.close()
                connection = None
            except failures:
                await discard()
