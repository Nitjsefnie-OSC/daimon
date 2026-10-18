"""The scheduler and turn path read only live GitHub tokens; those reads must use an index.

The real store functions run against a table that is mostly revoked rows, as in
production, and each SQL statement they send is explained.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from daimon.core._models import Tenant, ThreadSession
from daimon.core.stores import github_issued_tokens
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

INDEX = "ix_github_issued_tokens_live_session"
SESSIONS = 200
REVOKED = 20_000


def _scans(node: Any, table: str) -> list[str]:
    """Node types that read `table` in an EXPLAIN JSON plan: scans of the heap, and the
    `Index Name` of each index scan or bitmap index scan."""
    found: list[str] = []
    if node.get("Relation Name") == table:
        found.append(node["Node Type"])
    if "Index Name" in node:
        found.append(f"{node['Node Type']} on {node['Index Name']}")
    for child in node.get("Plans", []):
        found.extend(_scans(child, table))
    return found


async def _seed(db_session: AsyncSession) -> None:
    tenant_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
    await db_session.flush()
    await db_session.execute(
        text("""
        INSERT INTO github_issued_tokens
            (tenant_id, agent_id, session_id, installation_id, repo_ids, permissions,
             grant_versions, expires_at, status, revoked_at)
        SELECT :tenant, gen_random_uuid(), 'sesn_' || (n % :sessions), 77, ARRAY[1]::bigint[],
               '{}'::jsonb, '{}'::jsonb, now() - interval '2 days', 'revoked', now()
        FROM generate_series(1, :revoked) AS n
        """),
        {"tenant": tenant_id, "sessions": SESSIONS, "revoked": REVOKED},
    )
    await db_session.execute(
        text("""
        INSERT INTO github_issued_tokens
            (tenant_id, agent_id, session_id, installation_id, repo_ids, permissions,
             grant_versions, expires_at, status, revoke_after)
        SELECT :tenant, gen_random_uuid(), 'sesn_' || n, 77, ARRAY[1]::bigint[],
               '{}'::jsonb, '{}'::jsonb, now() + interval '30 minutes',
               CASE WHEN n % 5 = 0 THEN 'stored' ELSE 'delivered' END,
               CASE WHEN n % 3 = 0 THEN now() - interval '1 minute' END
        FROM generate_series(1, 150) AS n
        """),
        {"tenant": tenant_id},
    )
    db_session.add(
        ThreadSession(
            tenant_id=tenant_id,
            platform="discord",
            thread_id="thread-1",
            ma_session_id="sesn_1",
            status="live",
        )
    )
    await db_session.flush()
    await db_session.execute(text("ANALYZE github_issued_tokens"))
    await db_session.execute(text("ANALYZE thread_sessions"))


async def _explain_store_reads(db_session: AsyncSession) -> dict[str, list[str]]:
    """Run each store read, then EXPLAIN the statement it sent, keyed by read name."""
    connection = await db_session.connection()
    sent: list[tuple[str, Any]] = []

    def record(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if "github_issued_tokens" in statement and not statement.startswith("EXPLAIN"):
            sent.append((statement, parameters))

    now = datetime.now(UTC)
    reads = {
        "list_session_tokens": lambda: github_issued_tokens.list_session_tokens(
            db_session, session_id="sesn_5"
        ),
        "select_due_superseded_tokens": lambda: github_issued_tokens.select_due_superseded_tokens(
            db_session, now=now
        ),
        "select_abandoned_pending_tokens": lambda: (
            github_issued_tokens.select_abandoned_pending_tokens(db_session, now=now)
        ),
        "list_live_app_sessions": lambda: github_issued_tokens.list_live_app_sessions(db_session),
        "select_stale_tokens": lambda: github_issued_tokens.select_stale_tokens(
            db_session, now=now, include_live_version_bumps=True
        ),
    }
    plans: dict[str, list[str]] = {}
    event.listen(connection.sync_connection, "before_cursor_execute", record)
    try:
        for name, read in reads.items():
            sent.clear()
            await read()
            assert len(sent) == 1, name
            statement, parameters = sent[0]
            result = await connection.exec_driver_sql(
                f"EXPLAIN (FORMAT JSON) {statement}", parameters
            )
            raw = result.scalar_one()
            plan = (json.loads(raw) if isinstance(raw, str) else raw)[0]
            plans[name] = _scans(plan["Plan"], "github_issued_tokens")
    finally:
        event.remove(connection.sync_connection, "before_cursor_execute", record)
    return plans


async def test_live_token_reads_use_the_partial_index(db_session: AsyncSession) -> None:
    await _seed(db_session)

    with_index = await _explain_store_reads(db_session)
    for name, scans in with_index.items():
        assert "Seq Scan" not in scans, (name, scans)
        assert any(scan.endswith(f" on {INDEX}") for scan in scans), (name, scans)

    # Control: without the index the same reads scan the whole table.
    await db_session.execute(text(f"DROP INDEX {INDEX}"))
    without_index = await _explain_store_reads(db_session)
    for name, scans in without_index.items():
        assert "Seq Scan" in scans, (name, scans)
