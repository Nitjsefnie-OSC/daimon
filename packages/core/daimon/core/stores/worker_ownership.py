"""Liveness of adapter processes holding a PostgreSQL session advisory lock."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# The owner probes every 5 s and fails stop on its first failed probe. Its
# shutdown allows 60 s for turns, then 10 s for notices and 5 s each for turn
# cleanup and card edits. The 120 s grace also covers probe/query scheduling
# and connection teardown. This assumes the event loop runs within that bound.
OWNER_LOSS_GRACE_S = 120


async def owner_is_alive(session: AsyncSession, owner_key: int) -> bool:
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
