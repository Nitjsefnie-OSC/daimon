"""The agent files table survives later migrations."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_agent_files_table_exists_at_head(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name = 'agent_files' "
                "ORDER BY table_name"
            )
        )
        names = [row[0] for row in result.fetchall()]
    assert names == ["agent_files"]


@pytest.mark.asyncio
async def test_agent_files_pk_constraint_name(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                "SELECT conname FROM pg_constraint "
                "WHERE conname = 'pk_agent_files' "
                "AND connamespace = 'public'::regnamespace "
                "ORDER BY conname"
            )
        )
        names = [row[0] for row in result.fetchall()]
    assert names == ["pk_agent_files"]
