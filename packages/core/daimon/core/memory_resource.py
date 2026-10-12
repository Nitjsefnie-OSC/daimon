"""Lazy provisioning + session mounting of MA memory stores.

Two kinds of store, both created lazily and bound in Postgres:

- **shared**: one per (tenant, agent), in `agent_memory_store`. Every session of
  the agent mounts it, whoever is talking and wherever. It holds team and agent
  knowledge: conventions, project facts, how tools and skills behave.
- **personal**: one per (tenant, agent, account), in
  `agent_personal_memory_store`. Only that person's own interactive sessions
  mount it (a chat session is already per caller, keyed on the account), so
  another person's session cannot read it — the isolation is the mount, not
  the prompt. It holds that person's preferences and facts about them.

`ensure_memory_store_and_mount` / `ensure_personal_memory_store_and_mount`
return the session `resources[]` entry; callers (sessions.create_session)
treat failures as degrade-not-block — a session without memory beats no
session.

Tenant isolation: all reads/writes are keyed (tenant_id, agent_id[, account_id]);
the MA store is stamped with daimon_tenant/daimon_agent(/daimon_account)
metadata for traceability (stores are workspace-scoped on Anthropic's side —
the DB binding is the isolation boundary).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import anthropic as anthropic_errors
import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta.beta_managed_agents_memory_store_resource_param import (
    BetaManagedAgentsMemoryStoreResourceParam,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_TENANT
from daimon.core.stores.agent_memory_stores import (
    clear_memory_store,
    clear_personal_memory_store,
    get_memory_store_id,
    get_personal_memory_store_id,
    insert_memory_store,
    insert_personal_memory_store,
    list_personal_memory_stores_for_account,
    list_personal_memory_stores_for_agent,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)

MA_METADATA_KEY_AGENT = "daimon_agent"

MEMORY_INSTRUCTIONS = (
    "This is the agent's shared memory: everyone who talks to this agent, in "
    "every channel, sees it. Check it before starting any task. Record durable "
    "team and agent knowledge here: project conventions, decisions, facts about "
    "the work, how tools and skills behave, and corrections to how you work. "
    "Never record anything personal here: a person's preferences or facts about "
    "a specific person go in that person's personal memory, and nothing from a "
    "personal memory is ever copied into this one. Keep files small and focused "
    "(one topic per file); update existing files instead of appending "
    "duplicates. Do not store secrets, credentials, or transient conversation "
    "details."
)

SHARED_ONLY_MEMORY_INSTRUCTIONS = (
    "This is the agent's shared memory: everyone who talks to this agent, in "
    "every channel, sees it. Check it before starting any task. Record durable "
    "team and agent knowledge here: project conventions, decisions, facts about "
    "the work, how tools and skills behave, and corrections to how you work. "
    "No personal memory is mounted in this conversation, so do not record a "
    "person's preferences or facts about a specific person anywhere. Keep files "
    "small and focused (one topic per file); update existing files instead of "
    "appending duplicates. Do not store secrets, credentials, or transient "
    "conversation details."
)

READ_ONLY_MEMORY_INSTRUCTIONS = (
    "This is shared persistent memory, mounted read-only for this conversation. "
    "Consult it when useful, but do not create, update, rename, or delete memory files. "
    "Do not attempt to save facts from this turn into persistent memory."
)

PERSONAL_MEMORY_INSTRUCTIONS = (
    "This is the personal memory of the person you are talking with in this "
    "conversation. Only their own conversations with this agent see it. Check it "
    "before starting a task for them. Record their durable preferences (format, "
    "tone, tools, working habits) and facts about them that they would expect "
    "you to remember. Team and project knowledge belongs in the shared memory, "
    "and facts about anyone else do not belong here. Never copy anything from "
    "this memory into the shared memory, and do not quote it in a reply other "
    "people will read unless this person asks. Keep files small and focused; "
    "update existing files instead of appending duplicates. Do not store "
    "secrets, credentials, or transient conversation details."
)

READ_ONLY_PERSONAL_MEMORY_INSTRUCTIONS = (
    "This is the personal memory of the person you are talking with, mounted "
    "read-only for this conversation. Consult it to follow their preferences, "
    "but do not create, update, rename, or delete files in it, and never copy "
    "anything from it into another memory."
)

_STORE_DESCRIPTION = (
    "Persistent memory for the daimon agent '{agent_name}'. Written by the "
    "agent itself across sessions; managed by daimon."
)

_PERSONAL_STORE_DESCRIPTION = (
    "Personal memory of one person for the daimon agent '{agent_name}': their "
    "preferences and facts about them. Mounted only in that person's own "
    "conversations; managed by daimon."
)


async def _ensure_store(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    read_binding: Callable[[AsyncSession], Awaitable[str | None]],
    insert_binding: Callable[[AsyncSession, str], Awaitable[str]],
    name: str,
    description: str,
    metadata: dict[str, str],
) -> str:
    """The bound store id, creating the store and its binding on first use.

    Warm path: one DB read, zero API calls. Cold path: one
    memory_stores.create + race-safe insert; on a lost race the just-created
    orphan store is deleted and the winner's id is used.
    """
    async with session_factory() as session:
        store_id = await read_binding(session)
    if store_id is not None:
        return store_id

    created = await anthropic.beta.memory_stores.create(
        name=name, description=description, metadata=metadata
    )
    try:
        async with session_factory() as session, session.begin():
            store_id = await insert_binding(session, created.id)
    except Exception:
        # The binding never landed, so nothing references the store we
        # just created — delete it, or a DB outage mints one unreferenced
        # active store per session attempt.
        try:
            await anthropic.beta.memory_stores.delete(created.id)
        except anthropic_errors.APIError:
            _log.warning("memory_store.orphan_delete_failed", orphan=created.id)
        raise
    if store_id != created.id:
        # Lost the provisioning race — discard the orphan store.
        _log.info("memory_store.race_lost", orphan=created.id, winner=store_id)
        try:
            await anthropic.beta.memory_stores.delete(created.id)
        except anthropic_errors.APIError:
            _log.warning("memory_store.orphan_delete_failed", orphan=created.id)
    else:
        _log.info("memory_store.provisioned", memory_store_id=store_id, name=name)
    return store_id


async def ensure_memory_store_and_mount(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    agent_name: str,
    read_only: bool = False,
    personal_mounted: bool = False,
) -> BetaManagedAgentsMemoryStoreResourceParam:
    """Return the shared memory-store session resource, provisioning on first use.

    `personal_mounted` says whether the same session also mounts the caller's
    personal store: the writable instructions then point personal facts there,
    and otherwise forbid recording them at all. Instructions are per attachment
    and frozen at session creation, so a wording change reaches only new
    sessions and never makes a live one read as drifted.
    """

    async def read(session: AsyncSession) -> str | None:
        return await get_memory_store_id(session, tenant_id=tenant_id, agent_id=agent_id)

    async def insert(session: AsyncSession, store_id: str) -> str:
        return await insert_memory_store(
            session, tenant_id=tenant_id, agent_id=agent_id, memory_store_id=store_id
        )

    store_id = await _ensure_store(
        anthropic,
        session_factory,
        read_binding=read,
        insert_binding=insert,
        name=f"daimon {agent_name} {tenant_id}",
        description=_STORE_DESCRIPTION.format(agent_name=agent_name),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_AGENT: str(agent_id),
        },
    )
    if read_only:
        instructions = READ_ONLY_MEMORY_INSTRUCTIONS
    elif personal_mounted:
        instructions = MEMORY_INSTRUCTIONS
    else:
        instructions = SHARED_ONLY_MEMORY_INSTRUCTIONS
    return {
        "type": "memory_store",
        "memory_store_id": store_id,
        "access": "read_only" if read_only else "read_write",
        "instructions": instructions,
    }


async def ensure_personal_memory_store_and_mount(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    agent_name: str,
    read_only: bool = False,
) -> BetaManagedAgentsMemoryStoreResourceParam:
    """Return the caller's personal memory-store session resource, provisioning on first use.

    The store is named `personal <account id>`, so it mounts at
    `/mnt/memory/personal-<account id>/` beside the shared store. Its access
    follows the shared mount's: a sealed channel or a read-only DM keeps both
    read-only, or a sealed conversation could be written into memory that the
    same person's other conversations read.
    """

    async def read(session: AsyncSession) -> str | None:
        return await get_personal_memory_store_id(
            session, tenant_id=tenant_id, agent_id=agent_id, account_id=account_id
        )

    async def insert(session: AsyncSession, store_id: str) -> str:
        return await insert_personal_memory_store(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            account_id=account_id,
            memory_store_id=store_id,
        )

    store_id = await _ensure_store(
        anthropic,
        session_factory,
        read_binding=read,
        insert_binding=insert,
        name=f"personal {account_id}",
        description=_PERSONAL_STORE_DESCRIPTION.format(agent_name=agent_name),
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_AGENT: str(agent_id),
            MA_METADATA_KEY_ACCOUNT: str(account_id),
        },
    )
    return {
        "type": "memory_store",
        "memory_store_id": store_id,
        "access": "read_only" if read_only else "read_write",
        "instructions": (
            READ_ONLY_PERSONAL_MEMORY_INSTRUCTIONS if read_only else PERSONAL_MEMORY_INSTRUCTIONS
        ),
    }


async def archive_memory_store_for_agent(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
) -> None:
    """Archive the agent's stores (audit trail preserved) and drop the bindings.

    The shared store and every person's personal store for the agent go
    together: a personal store belongs to its agent and is never mounted once
    the agent is gone.

    Idempotent: unbound agent → no-op; already-archived/missing store on the
    MA side → binding still cleared.
    """
    async with session_factory() as session:
        store_id = await get_memory_store_id(session, tenant_id=tenant_id, agent_id=agent_id)
        personal = await list_personal_memory_stores_for_agent(
            session, tenant_id=tenant_id, agent_id=agent_id
        )
    for account_id, personal_id in personal:
        try:
            await anthropic.beta.memory_stores.archive(personal_id)
        except anthropic_errors.NotFoundError:
            _log.info("memory_store.archive_missing", memory_store_id=personal_id)
        async with session_factory() as session, session.begin():
            await clear_personal_memory_store(
                session, tenant_id=tenant_id, agent_id=agent_id, account_id=account_id
            )
        _log.info("memory_store.personal_archived", memory_store_id=personal_id)
    if store_id is None:
        return
    try:
        await anthropic.beta.memory_stores.archive(store_id)
    except anthropic_errors.NotFoundError:
        _log.info("memory_store.archive_missing", memory_store_id=store_id)
    async with session_factory() as session, session.begin():
        await clear_memory_store(session, tenant_id=tenant_id, agent_id=agent_id)
    _log.info("memory_store.archived", memory_store_id=store_id)


async def delete_personal_memory_stores_for_account(
    anthropic: AsyncAnthropic,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: uuid.UUID,
) -> bool:
    """Delete every personal store one person has, for account erasure.

    Deleted, not archived: an archived store keeps its memories and versions,
    and erasure has to remove them. Each binding is dropped only after its
    store is gone (or already missing), so a failure leaves the binding as the
    record of what is still to delete and a retry picks it up. Returns True
    when nothing is left.
    """
    async with session_factory() as session:
        bound = await list_personal_memory_stores_for_account(session, account_id=account_id)
    complete = True
    for tenant_id, agent_id, store_id in bound:
        try:
            await anthropic.beta.memory_stores.delete(store_id)
        except anthropic_errors.NotFoundError:
            pass
        except anthropic_errors.APIError as error:
            _log.warning(
                "memory_store.personal_delete_failed",
                memory_store_id=store_id,
                error=type(error).__name__,
            )
            complete = False
            continue
        async with session_factory() as session, session.begin():
            await clear_personal_memory_store(
                session, tenant_id=tenant_id, agent_id=agent_id, account_id=account_id
            )
        _log.info("memory_store.personal_deleted", memory_store_id=store_id)
    return complete
