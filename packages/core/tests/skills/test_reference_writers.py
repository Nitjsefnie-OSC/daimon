"""Every path that writes or retires a skill reference honours the reference lock.

Ported from the PR #857 re-review probes (reviews/evidence-857-97036d2), which
reproduced each path stranding an agent or a channel on a deleted skill. Here
the same interleavings assert the safe outcome. Real Postgres for the locks
(separate connections where one side must wait), fake Managed Agents for the
provider.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core import reader_agent
from daimon.core.channel_skills import add_skill_to_channel
from daimon.core.defaults import reconcile_agents
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.reconcile_skills import reconcile_skill
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.reader_agent import ensure_reader_variant
from daimon.core.scope import DeploymentDefault
from daimon.core.skill_sync.orchestrator import sync_agent_skills
from daimon.core.skill_zip import build_skill_zip
from daimon.core.skills import attachments
from daimon.core.skills.attachments import delete_skill_unless_attached
from daimon.core.skills.reference_lock import SkillDeletedWhileAttaching
from daimon.core.specs import AgentSpec, SkillRef, SkillRepo
from daimon.core.stores.channel_skills import add_channel_skill, list_skill_channel_rules
from daimon.core.stores.domain import TenantRow
from daimon.core.stores.seeded_skills import record_seeded_skill
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.user_skills import list_user_skills_for_agent, upsert_user_skill
from daimon.testing.archives import make_tarball
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_cli_principal, make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
    skill_response,
)
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

SKILL = "skill_existing"


class _Workspace:
    """Agents plus one skill titled `existing`; a delete really makes it gone."""

    def __init__(self, tenant: TenantRow, *, agent_name: str | None = None) -> None:
        self.tenant = tenant
        self.state = FakeMAState()
        self.deletes: list[str] = []
        self.title = tenant_scoped_display_title(
            tenant_id=tenant.id, name="existing", agent_name=agent_name
        )

    def add_agent(self, name: str, skills: list[str]) -> None:
        agent = ma_agent(
            id=f"agent_{name}",
            name=name,
            tenant_id=self.tenant.id,
            skills=[{"type": "custom", "skill_id": s, "version": "1"} for s in skills],
        )
        self.state.agents[agent.id] = agent.model_dump(mode="json")

    def client(self) -> AsyncAnthropic:
        def extra(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and path == "/v1/skills":
                gone = SKILL in self.state.deleted_skills
                return list_response([] if gone else [skill_response(SKILL, self.title)])
            if request.method == "GET" and re.fullmatch(r"/v1/skills/[^/]+/versions", path):
                return list_response([])
            if request.method == "DELETE" and re.fullmatch(r"/v1/skills/[^/]+", path):
                skill_id = path.rsplit("/", 1)[1]
                self.state.deleted_skills.add(skill_id)
                self.deletes.append(skill_id)
                return httpx.Response(200, json={"id": skill_id, "type": "skill_deleted"})
            raise NotHandled

        return build_fake_anthropic(combine_handlers(extra, make_fake_ma_handler(self.state)))

    def pins(self, agent_id: str) -> list[str]:
        agent = self.state.agents[agent_id]
        return [str(s["skill_id"]) for s in agent["skills"]]  # type: ignore[index, union-attr]


@pytest.fixture
async def sm(
    db_nullpool_engine: AsyncEngine, db_clean: None
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield async_sessionmaker(db_nullpool_engine, expire_on_commit=False)


async def _tenant(sm: async_sessionmaker[AsyncSession]) -> TenantRow:
    async with sm() as session, session.begin():
        return await make_tenant(session)


def _spec() -> AgentSpec:
    return AgentSpec(
        name="target",
        model="claude-sonnet-4-6",
        skills=[SkillRef(type="custom", skill_id="existing")],
    )


async def _still_blocked(task: asyncio.Task[Any]) -> bool:
    await asyncio.sleep(0.3)
    return not task.done()


@pytest.mark.parametrize("existing_agent", [False, True])
async def test_reconcile_waits_for_a_delete_holding_the_lock_and_then_refuses(
    sm: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    existing_agent: bool,
) -> None:
    """Delete first: reconcile resolved the skill, then a delete ran; no pin is written."""
    tenant = await _tenant(sm)
    workspace = _Workspace(tenant)
    if existing_agent:
        workspace.add_agent("target", [])
    client = workspace.client()
    resolved, resume = asyncio.Event(), asyncio.Event()
    real_resolve = reconcile_agents.resolve_refs

    async def pause_after_resolution(*args: Any, **kwargs: Any) -> Any:
        skills = await real_resolve(*args, **kwargs)
        resolved.set()
        await resume.wait()
        return skills

    monkeypatch.setattr(reconcile_agents, "resolve_refs", pause_after_resolution)
    attaching = asyncio.create_task(
        reconcile_agents.reconcile_agent(
            client, _spec(), tenant_id=tenant.id, dry_run=False, managed=False, references=sm
        )
    )
    await asyncio.wait_for(resolved.wait(), 5)
    async with sm() as deleting, deleting.begin():
        assert (
            await delete_skill_unless_attached(client, deleting, skill_id=SKILL, detach=False)
        ).deleted
        resume.set()
        assert await _still_blocked(attaching), "the attach waits for the delete's lock"

    with pytest.raises(SkillDeletedWhileAttaching):
        await attaching
    for agent_id in workspace.state.agents:
        assert SKILL not in workspace.pins(agent_id)


@pytest.mark.parametrize("existing_agent", [False, True])
async def test_a_delete_after_its_final_check_holds_off_reconcile(
    sm: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    existing_agent: bool,
) -> None:
    """The review's probe: reconcile pins the live skill after the delete's last look."""
    tenant = await _tenant(sm)
    workspace = _Workspace(tenant)
    if existing_agent:
        workspace.add_agent("target", [])
    client = workspace.client()
    checked, resume = asyncio.Event(), asyncio.Event()
    real_find = attachments.find_skill_attachments

    async def pause_after_final_snapshot(*args: Any, **kwargs: Any) -> Any:
        refs = await real_find(*args, **kwargs)
        assert not refs
        checked.set()
        await resume.wait()
        return refs

    monkeypatch.setattr(attachments, "find_skill_attachments", pause_after_final_snapshot)

    async def delete() -> bool:
        async with sm() as session, session.begin():
            outcome = await delete_skill_unless_attached(
                client, session, skill_id=SKILL, detach=False
            )
            return outcome.deleted

    deleting = asyncio.create_task(delete())
    await asyncio.wait_for(checked.wait(), 5)
    attaching = asyncio.create_task(
        reconcile_agents.reconcile_agent(
            client, _spec(), tenant_id=tenant.id, dry_run=False, managed=False, references=sm
        )
    )
    assert await _still_blocked(attaching)
    resume.set()
    assert await deleting
    with pytest.raises(SkillDeletedWhileAttaching):
        await attaching
    for agent_id in workspace.state.agents:
        assert SKILL not in workspace.pins(agent_id)


async def test_a_reader_copied_before_a_force_delete_is_rebuilt_without_the_skill(
    sm: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's probe: the reader's source snapshot names a skill a force delete removes."""
    tenant = await _tenant(sm)
    workspace = _Workspace(tenant)
    workspace.add_agent("source", [SKILL])
    client = workspace.client()
    snapshot, resume = asyncio.Event(), asyncio.Event()

    async def reader_ref(*args: Any, **kwargs: Any) -> list[Any]:
        snapshot.set()  # the source's skills are already copied
        await resume.wait()
        return []  # leave out the report-reader skill: only the copied pin matters

    monkeypatch.setattr(reader_agent, "resolve_refs", reader_ref)
    attaching = asyncio.create_task(
        ensure_reader_variant(
            client,
            tenant_id=tenant.id,
            account_id=uuid.uuid4(),
            source_name="source",
            references=sm,
        )
    )
    await asyncio.wait_for(snapshot.wait(), 5)
    before_provider_delete, finish_delete = asyncio.Event(), asyncio.Event()
    real_provider_delete = attachments.delete_skill_and_versions

    async def pause_before_provider_delete(*args: Any, **kwargs: Any) -> None:
        before_provider_delete.set()
        await finish_delete.wait()
        await real_provider_delete(*args, **kwargs)

    monkeypatch.setattr(attachments, "delete_skill_and_versions", pause_before_provider_delete)

    async def delete() -> bool:
        async with sm() as session, session.begin():
            outcome = await delete_skill_unless_attached(
                client, session, skill_id=SKILL, detach=True
            )
            return outcome.deleted

    deleting = asyncio.create_task(delete())
    await asyncio.wait_for(before_provider_delete.wait(), 5)
    resume.set()
    assert await _still_blocked(attaching), "the reader waits for the delete's lock"
    finish_delete.set()
    assert await deleting
    reader = await asyncio.wait_for(attaching, 5)

    assert SKILL in workspace.state.deleted_skills
    assert all(skill.skill_id != SKILL for skill in reader.skills), "no stale copied pin"
    assert workspace.pins("agent_source") == []


async def test_sync_keeps_an_orphan_a_channel_still_adds(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The review's probe: a synced skill removed from its repo but added to a channel.

    No concurrency needed: the orphan cleanup must not delete it, and its
    registry row stays so a later sync can retire it once the rule is gone.
    """
    cli = await make_cli_principal(db_session, os_user="probe")
    await db_session.commit()
    async with db_session_factory() as session:
        tenant = await get_tenant(session, cli.tenant_id)
    assert tenant is not None
    workspace = _Workspace(tenant, agent_name="target")
    workspace.add_agent("target", [SKILL])
    client = workspace.client()
    principal = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_target")
    async with db_session_factory() as session, session.begin():
        await upsert_user_skill(
            session,
            tenant_id=tenant.id,
            principal_id=principal,
            agent_name="target",
            name="existing",
            source_repo_url="https://github.com/o/r1",
            source_repo_branch="main",
            source_path="",
            content_hash="hash",
            anthropic_id=SKILL,
            anthropic_latest_version="1",
        )
        rule = await add_skill_to_channel(
            session,
            client,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="chan",
            skill=SKILL,
            default=DeploymentDefault(agent_name="target"),
            actor_account_id=None,
        )
        assert not isinstance(rule, str), rule
    tarball = make_tarball({"r1-main/README.md": b"empty"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, content=tarball))
    )

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=tenant.id,
        agent_name="target",
        repos=[SkillRepo(url="https://github.com/o/r1", branch="main", split=True)],
        sessionmaker=db_session_factory,
        fernet=make_fernet(),
        http_client=http_client,
        anthropic_client=client,
    )

    assert report.deleted == 0 and workspace.deletes == []
    async with db_session_factory() as session:
        assert len(await list_skill_channel_rules(session, skill_id=SKILL)) == 1
        rows = await list_user_skills_for_agent(
            session, tenant_id=tenant.id, principal_id=principal, agent_name="target"
        )
    assert [row.anthropic_id for row in rows] == [SKILL], "the registry row is kept"


async def test_sync_still_retires_an_orphan_only_its_own_agent_pins(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The synced agent's own pin goes with the orphan; nothing else is holding it."""
    cli = await make_cli_principal(db_session, os_user="probe")
    await db_session.commit()
    async with db_session_factory() as session:
        tenant = await get_tenant(session, cli.tenant_id)
    assert tenant is not None
    workspace = _Workspace(tenant, agent_name="target")
    workspace.add_agent("target", [SKILL])
    client = workspace.client()
    principal = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_target")
    async with db_session_factory() as session, session.begin():
        await upsert_user_skill(
            session,
            tenant_id=tenant.id,
            principal_id=principal,
            agent_name="target",
            name="existing",
            source_repo_url="https://github.com/o/r1",
            source_repo_branch="main",
            source_path="",
            content_hash="hash",
            anthropic_id=SKILL,
            anthropic_latest_version="1",
        )
    tarball = make_tarball({"r1-main/README.md": b"empty"})
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, content=tarball))
    )

    report = await sync_agent_skills(
        principal_id=cli.id,
        tenant_id=tenant.id,
        agent_name="target",
        repos=[SkillRepo(url="https://github.com/o/r1", branch="main", split=True)],
        sessionmaker=db_session_factory,
        fernet=make_fernet(),
        http_client=http_client,
        anthropic_client=client,
    )

    assert report.deleted == 1 and workspace.deletes == [SKILL]
    assert workspace.pins("agent_target") == [], "its own pin is detached first"


@pytest.mark.parametrize("referenced_by", ["channel", "agent"])
async def test_defaults_dedup_keeps_a_referenced_duplicate(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    referenced_by: str,
) -> None:
    """The review's probe: an older same-title duplicate a channel or agent still uses."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    skill_dir = tmp_path / "existing"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: existing\ndescription: d\n---\nbody\n")
    title = tenant_scoped_display_title(tenant_id=tenant.id, name="existing")
    older = skill_response(SKILL, title)
    older["created_at"] = "2026-09-01T00:00:00Z"
    newer = skill_response("skill_newer", title)
    newer["created_at"] = "2026-10-01T00:00:00Z"
    async with db_session_factory() as session, session.begin():
        await record_seeded_skill(
            session,
            tenant_id=tenant.id,
            name="existing",
            content_hash=build_skill_zip(skill_dir).content_hash,
            anthropic_id="skill_newer",
        )
        if referenced_by == "channel":
            await add_channel_skill(
                session,
                tenant_id=tenant.id,
                platform="discord",
                channel_id="chan",
                skill_id=SKILL,
                version="1",
                name="existing",
                owner_agent_name=None,
                actor_account_id=None,
            )
    agents = (
        [
            ma_agent(
                id="agent_old",
                name="old",
                tenant_id=tenant.id,
                skills=[{"type": "custom", "skill_id": SKILL, "version": "1"}],
            ).model_dump(mode="json")
        ]
        if referenced_by == "agent"
        else []
    )
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/skills":
            return list_response([older, newer])
        if request.method == "GET" and path == "/v1/agents":
            return list_response(agents)
        if request.method == "GET" and re.fullmatch(r"/v1/skills/[^/]+/versions", path):
            return list_response([])
        if request.method == "DELETE":
            deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"id": deleted[-1], "type": "skill_deleted"})
        raise AssertionError(f"unexpected {request.method} {path}")

    await reconcile_skill(
        build_fake_anthropic(handler),
        db_session_factory,
        skill_dir,
        tenant_id=tenant.id,
        dry_run=False,
    )

    assert deleted == [], "a duplicate in use is kept"
    if referenced_by == "channel":
        async with db_session_factory() as session:
            assert len(await list_skill_channel_rules(session, skill_id=SKILL)) == 1


async def test_defaults_dedup_waits_for_a_concurrent_channel_attach(
    sm: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A channel rule being added to the older duplicate as dedup runs keeps it."""
    from daimon.core.skills.reference_lock import lock_skills_for_attach

    tenant = await _tenant(sm)
    skill_dir = tmp_path / "existing"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: existing\ndescription: d\n---\nbody\n")
    title = tenant_scoped_display_title(tenant_id=tenant.id, name="existing")
    older = skill_response(SKILL, title)
    older["created_at"] = "2026-09-01T00:00:00Z"
    newer = skill_response("skill_newer", title)
    newer["created_at"] = "2026-10-01T00:00:00Z"
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/skills":
            return list_response([older, newer])
        if request.method == "GET" and path == "/v1/agents":
            return list_response([])
        if request.method == "GET" and re.fullmatch(r"/v1/skills/[^/]+", path):
            return httpx.Response(200, json=skill_response(path.rsplit("/", 1)[1]))
        if request.method == "GET" and re.fullmatch(r"/v1/skills/[^/]+/versions", path):
            return list_response([])
        if request.method == "DELETE":
            deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"id": deleted[-1], "type": "skill_deleted"})
        if request.method == "POST" and path.endswith("/versions"):
            return httpx.Response(500)
        raise AssertionError(f"unexpected {request.method} {path}")

    client = build_fake_anthropic(handler)
    async with sm() as attach, attach.begin():
        await lock_skills_for_attach(attach, client, [SKILL])
        await add_channel_skill(
            attach,
            tenant_id=tenant.id,
            platform="discord",
            channel_id="chan",
            skill_id=SKILL,
            version="1",
            name="existing",
            owner_agent_name=None,
            actor_account_id=None,
        )
        dedup = asyncio.create_task(
            reconcile_skill(client, sm, skill_dir, tenant_id=tenant.id, dry_run=False)
        )
        assert await _still_blocked(dedup), "dedup waits for the attach to commit"
    await asyncio.gather(dedup, return_exceptions=True)

    assert SKILL not in deleted
    async with sm() as session:
        assert len(await list_skill_channel_rules(session, skill_id=SKILL)) == 1


async def test_backfill_does_not_repin_to_a_skill_deleted_before_the_repin(
    sm: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's probe: the new skill is deleted between its creation and the re-pin."""
    from io import StringIO

    from daimon.adapters.cli.commands import skills_backfill as backfill
    from rich.console import Console

    tenant = await _tenant(sm)
    workspace = _Workspace(tenant)
    workspace.add_agent("target", ["legacy_skill"])
    client = workspace.client()

    async def create_then_operator_deletes(**_kwargs: Any) -> str:
        async with sm() as session, session.begin():
            outcome = await delete_skill_unless_attached(
                client, session, skill_id=SKILL, detach=False
            )
            assert outcome.deleted
        return SKILL

    monkeypatch.setattr(backfill, "_create_new_skill", create_then_operator_deletes)
    row = backfill._BackfillRow(  # pyright: ignore[reportPrivateUsage]
        tenant_id=str(tenant.id),
        agent_names="target",
        skill_id="legacy_skill",
        display_title="cli-auth",
        classification="RECREATE_SEEDED",
        new_title=workspace.title,
        new_skill_id="",
    )
    await backfill._apply_phase(  # pyright: ignore[reportPrivateUsage]
        client, sm, rows=[row], http_client=httpx.AsyncClient(), console=Console(file=StringIO())
    )

    assert SKILL not in workspace.pins("agent_target")
    assert workspace.pins("agent_target") == ["legacy_skill"], "the legacy pin is left in place"
