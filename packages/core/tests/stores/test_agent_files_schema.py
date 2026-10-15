"""Schema-drift guard for composite primary keys.

Cross-agent isolation for agent files depends on the composite primary key.
"""

from __future__ import annotations

from sqlalchemy import Connection, Inspector, inspect
from sqlalchemy.ext.asyncio import AsyncSession


def _pk_columns(sync_conn: Connection, table: str) -> list[str]:
    inspector: Inspector = inspect(sync_conn)
    pk = inspector.get_pk_constraint(table)
    return list(pk["constrained_columns"])


async def test_agent_files_has_composite_pk(db_session: AsyncSession) -> None:
    """SC-3 structural: PK on agent_files is (tenant_id, agent_id, key)."""
    pk_cols = await db_session.run_sync(lambda s: _pk_columns(s.connection(), "agent_files"))
    assert pk_cols == ["tenant_id", "agent_id", "key"], (
        f"agent_files composite PK drift: expected (tenant_id, agent_id, key), got {pk_cols}"
    )
