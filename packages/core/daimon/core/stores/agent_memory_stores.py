"""MA memory store bindings (agent memory feature).

The shared store is one per (tenant, agent); a personal store is one per
(tenant, agent, account).

`insert_memory_store` is the race-safe half of lazy provisioning: two
concurrent first sessions may both create an MA store; ON CONFLICT DO NOTHING
means exactly one row wins, and the return value tells the caller which store
id is canonical (the loser deletes its orphan MA store).
"""

from __future__ import annotations

import uuid

from daimon.core._models import AgentMemoryStore, AgentPersonalMemoryStore
from daimon.core.errors import StoreError
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_memory_store_id(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> str | None:
    """Return the bound memstore_... id, or None when unbound."""
    orm = await session.get(AgentMemoryStore, (tenant_id, agent_id))
    return None if orm is None else orm.memory_store_id


async def insert_memory_store(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    memory_store_id: str,
) -> str:
    """Race-safe insert. Returns the WINNING memory_store_id.

    ON CONFLICT DO NOTHING on the composite PK: when another writer got there
    first, the existing row's id is returned so the caller can discard its own
    just-created MA store.

    Bounded retry (3 attempts): the no-op insert and the follow-up SELECT are
    two separate statements, so a concurrent transaction can delete the
    (tenant_id, agent_id) row in between — the SELECT then finds nothing
    rather than the expected winner. Rather than let that surface as an
    unhandled NoResultFound, we retry the insert (with the row now gone, our
    insert should win) up to `max_attempts` times. If the row still can't be
    resolved after all attempts (pathological concurrent churn), raise
    StoreError.
    """
    stmt = (
        pg_insert(AgentMemoryStore)
        .values(tenant_id=tenant_id, agent_id=agent_id, memory_store_id=memory_store_id)
        .on_conflict_do_nothing(constraint="pk_agent_memory_store")
    )
    max_attempts = 3
    for _ in range(max_attempts):
        await session.execute(stmt)
        await session.flush()
        result = await session.execute(
            select(AgentMemoryStore.memory_store_id).where(
                AgentMemoryStore.tenant_id == tenant_id,
                AgentMemoryStore.agent_id == agent_id,
            )
        )
        winner = result.scalar_one_or_none()
        if winner is not None:
            return winner
    raise StoreError(
        f"insert_memory_store: no binding for tenant={tenant_id} agent={agent_id} "
        f"survived {max_attempts} attempts — concurrent clear_memory_store churn "
        "outpaced the retry budget"
    )


async def clear_memory_store(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> None:
    """Delete the binding. Idempotent — clearing an absent binding is a no-op
    (the archival path may run against agents that never had memory)."""
    await session.execute(
        delete(AgentMemoryStore).where(
            AgentMemoryStore.tenant_id == tenant_id,
            AgentMemoryStore.agent_id == agent_id,
        )
    )
    await session.flush()


# --- Personal stores: one per (tenant, agent, account) -----------------------


async def get_personal_memory_store_id(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
) -> str | None:
    """Return the person's bound memstore_... id for this agent, or None when unbound."""
    orm = await session.get(AgentPersonalMemoryStore, (tenant_id, agent_id, account_id))
    return None if orm is None else orm.memory_store_id


async def insert_personal_memory_store(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    memory_store_id: str,
) -> str:
    """Race-safe insert. Returns the WINNING memory_store_id.

    Same contract as `insert_memory_store`, one level finer: two first sessions
    for the same person may both create a store, and exactly one binding wins.
    """
    stmt = (
        pg_insert(AgentPersonalMemoryStore)
        .values(
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            memory_store_id=memory_store_id,
        )
        .on_conflict_do_nothing(constraint="pk_agent_personal_memory_store")
    )
    max_attempts = 3
    for _ in range(max_attempts):
        await session.execute(stmt)
        await session.flush()
        winner = await get_personal_memory_store_id(
            session, tenant_id=tenant_id, agent_id=agent_id, account_id=account_id
        )
        if winner is not None:
            return winner
    raise StoreError(
        f"insert_personal_memory_store: no binding for tenant={tenant_id} agent={agent_id} "
        f"account={account_id} survived {max_attempts} attempts"
    )


async def list_personal_memory_stores_for_agent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> list[tuple[uuid.UUID, str]]:
    """Every `(account_id, memory_store_id)` bound for one agent."""
    rows = await session.execute(
        select(AgentPersonalMemoryStore.account_id, AgentPersonalMemoryStore.memory_store_id).where(
            AgentPersonalMemoryStore.tenant_id == tenant_id,
            AgentPersonalMemoryStore.agent_id == agent_id,
        )
    )
    return [(account_id, store_id) for account_id, store_id in rows.tuples()]


async def list_personal_memory_stores_for_account(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
) -> list[tuple[uuid.UUID, uuid.UUID, str]]:
    """Every `(tenant_id, agent_id, memory_store_id)` one person has, across tenants."""
    rows = await session.execute(
        select(
            AgentPersonalMemoryStore.tenant_id,
            AgentPersonalMemoryStore.agent_id,
            AgentPersonalMemoryStore.memory_store_id,
        ).where(AgentPersonalMemoryStore.account_id == account_id)
    )
    return [(tenant_id, agent_id, store_id) for tenant_id, agent_id, store_id in rows.tuples()]


async def clear_personal_memory_store(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
) -> None:
    """Delete one person's binding. Idempotent."""
    await session.execute(
        delete(AgentPersonalMemoryStore).where(
            AgentPersonalMemoryStore.tenant_id == tenant_id,
            AgentPersonalMemoryStore.agent_id == agent_id,
            AgentPersonalMemoryStore.account_id == account_id,
        )
    )
    await session.flush()
