"""Deleting a skill and attaching it are serialized by the skill's reference lock.

Each test runs two real database connections (`db_nullpool_engine`) so one
side genuinely waits on the other's advisory lock, then checks the stranded
outcome the PR #857 review reproduced cannot happen: a reference committed to
a skill that is then deleted.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core.skills.attachments import delete_skill_unless_attached
from daimon.core.skills.reference_lock import (
    SkillDeletedWhileAttaching,
    guarded_agent_update,
    lock_skills_for_attach,
)
from daimon.core.stores.channel_skills import add_channel_skill, list_skill_channel_rules
from daimon.core.stores.domain import TenantRow
from daimon.testing.factories import make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
)
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

_SKILL = "skill_shared"


class _Workspace:
    """A Managed Agents fake whose skill delete really makes the skill gone."""

    def __init__(self) -> None:
        self.state = FakeMAState()
        self.deletes: list[str] = []

    def client(self) -> AsyncAnthropic:
        def skills(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and re.fullmatch(r"/v1/skills/[^/]+/versions", path):
                return list_response([])
            match = re.fullmatch(r"/v1/skills/([^/]+)", path)
            if request.method == "DELETE" and match is not None:
                self.deletes.append(match.group(1))
                self.state.deleted_skills.add(match.group(1))
                return httpx.Response(200, json={"id": match.group(1), "type": "skill_deleted"})
            raise NotHandled

        return build_fake_anthropic(combine_handlers(skills, make_fake_ma_handler(self.state)))

    def add_agent(self, agent_id: str, *, skill_ids: list[str]) -> None:
        agent = ma_agent(
            id=agent_id,
            name=agent_id,
            skills=[{"type": "custom", "skill_id": s, "version": "latest"} for s in skill_ids],
        )
        self.state.agents[agent_id] = agent.model_dump(mode="json")


@pytest.fixture
async def sm(
    db_nullpool_engine: AsyncEngine, db_clean: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(db_nullpool_engine, expire_on_commit=False)


async def _tenant(sm: async_sessionmaker[AsyncSession]) -> TenantRow:
    async with sm() as session, session.begin():
        return await make_tenant(session)


async def _add_rule(session: AsyncSession, tenant: TenantRow) -> None:
    await add_channel_skill(
        session,
        tenant_id=tenant.id,
        platform="discord",
        channel_id="new-channel",
        skill_id=_SKILL,
        version="1",
        name="shared",
        owner_agent_name=None,
        actor_account_id=None,
    )


async def _still_blocked(task: asyncio.Task[object]) -> bool:
    await asyncio.sleep(0.3)
    return not task.done()


@pytest.mark.parametrize("detach", [False, True])
async def test_a_delete_waits_for_a_channel_attach_and_then_sees_its_row(
    sm: async_sessionmaker[AsyncSession], detach: bool
) -> None:
    """The review's repro: a channel rule committed after the first look at references."""
    tenant = await _tenant(sm)
    workspace = _Workspace()
    client = workspace.client()

    async def delete() -> bool:
        async with sm() as session, session.begin():
            outcome = await delete_skill_unless_attached(
                client, session, skill_id=_SKILL, detach=detach
            )
        return outcome.deleted

    async with sm() as attach, attach.begin():
        await lock_skills_for_attach(attach, client, [_SKILL])
        await _add_rule(attach, tenant)
        deleting = asyncio.create_task(delete())
        assert await _still_blocked(deleting), "the delete must wait for the attach to commit"
    deleted = await deleting

    async with sm() as session:
        rules = await list_skill_channel_rules(session, skill_id=_SKILL)
    if detach:
        assert deleted and rules == [], "force removes the rule it waited for, then deletes"
    else:
        assert not deleted and len(rules) == 1, "a plain delete is refused by the new rule"
        assert workspace.deletes == []


async def test_a_channel_attach_waits_for_a_delete_and_then_refuses(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _tenant(sm)
    workspace = _Workspace()
    client = workspace.client()

    async def attach() -> None:
        async with sm() as session, session.begin():
            await lock_skills_for_attach(session, client, [_SKILL])
            await _add_rule(session, tenant)

    async with sm() as deleting, deleting.begin():
        outcome = await delete_skill_unless_attached(
            client, deleting, skill_id=_SKILL, detach=False
        )
        assert outcome.deleted
        attaching = asyncio.create_task(attach())
        assert await _still_blocked(attaching), "the attach must wait for the delete to commit"

    with pytest.raises(SkillDeletedWhileAttaching):
        await attaching
    async with sm() as session:
        assert await list_skill_channel_rules(session, skill_id=_SKILL) == []


async def test_a_delete_waits_for_an_agent_attach_and_then_sees_the_pin(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    workspace = _Workspace()
    workspace.add_agent("agent_a", skill_ids=[])
    client = workspace.client()
    fresh = await client.beta.agents.retrieve("agent_a")
    release = asyncio.Event()
    entered = asyncio.Event()

    async def slow_attach() -> None:
        # Holds the shared lock across the provider update, as every attach does.
        async with sm() as session, session.begin():
            await lock_skills_for_attach(session, client, [_SKILL])
            entered.set()
            await release.wait()
            await client.beta.agents.update(
                fresh.id,
                version=fresh.version,
                skills=[{"type": "custom", "skill_id": _SKILL}],
            )

    attaching = asyncio.create_task(slow_attach())
    await entered.wait()

    async def delete() -> bool:
        async with sm() as session, session.begin():
            outcome = await delete_skill_unless_attached(
                client, session, skill_id=_SKILL, detach=False
            )
        return outcome.deleted

    deleting = asyncio.create_task(delete())
    assert await _still_blocked(deleting)
    release.set()
    await attaching
    assert await deleting is False, "the delete re-reads agents under the lock and refuses"
    assert workspace.deletes == []


async def test_guarded_agent_update_refuses_a_skill_deleted_while_it_waited(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    workspace = _Workspace()
    workspace.add_agent("agent_a", skill_ids=[])
    client = workspace.client()
    fresh = await client.beta.agents.retrieve("agent_a")

    async def attach() -> object:
        return await guarded_agent_update(
            client, sm, fresh, skills=[{"type": "custom", "skill_id": _SKILL}]
        )

    async with sm() as deleting, deleting.begin():
        await delete_skill_unless_attached(client, deleting, skill_id=_SKILL, detach=False)
        attaching = asyncio.create_task(attach())
        assert await _still_blocked(attaching)

    with pytest.raises(SkillDeletedWhileAttaching):
        await attaching
    agent = await client.beta.agents.retrieve("agent_a")
    assert agent.skills == [], "the agent was never pinned to the deleted skill"


async def test_an_update_adding_no_skill_takes_no_lock(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    """Attaches only lock skills they add: a held delete lock doesn't block other edits."""
    workspace = _Workspace()
    workspace.add_agent("agent_a", skill_ids=[_SKILL])
    client = workspace.client()
    fresh = await client.beta.agents.retrieve("agent_a")
    async with sm() as holding, holding.begin():
        from daimon.core.skills.reference_lock import lock_skill_for_delete

        await lock_skill_for_delete(holding, _SKILL)
        updated = await asyncio.wait_for(
            guarded_agent_update(client, sm, fresh, description="new"), timeout=5
        )
    assert updated.description == "new"
