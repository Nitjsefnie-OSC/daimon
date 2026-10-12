"""Whose a skill is, read off its title: the full tenant id, or 8 hex chars one tenant alone has.

Titles are written with the whole tenant id. A title written before that
carries only the first 8 hex chars; the skills listing reads it as that
tenant's full-id title when exactly one tenant of the deployment has those
chars, and as nobody's otherwise. Mount checks read a shortened title's
folder off the skill's version, since shortening can cut the name.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
import pytest
from daimon.core.agent_fork import _split_scoped_skills  # pyright: ignore[reportPrivateUsage]
from daimon.core.defaults.ma_index import (
    find_attach_mount_collision,
    find_conflicting_skill_mount,
    list_skills_strict,
)
from daimon.core.defaults.metadata import strip_tenant_prefix, tenant_scoped_display_title
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_fake_anthropic, list_response
from daimon.testing.ma_models import ma_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT = uuid.UUID("5ca1ab1e-0000-4000-8000-00000000000a")
TWIN = uuid.UUID("5ca1ab1e-ffff-4fff-8fff-00000000000b")
OTHER = uuid.UUID("0ddba11e-0000-4000-8000-00000000000c")


def _skill(skill_id: str, title: str) -> dict[str, Any]:
    return {
        "id": skill_id,
        "display_title": title,
        "source": "custom",
        "type": "skill",
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
        "latest_version": "2",
    }


def _client(
    skills: list[dict[str, Any]],
    *,
    names: dict[str, str] | None = None,
    agents: list[dict[str, Any]] | None = None,
) -> Any:
    """The skills list, each skill's versions (named per `names`), and the agents list."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/skills":
            return list_response(skills)
        if path == "/v1/agents":
            return list_response(agents or [])
        if m := re.fullmatch(r"/v1/skills/([^/]+)/versions", path):
            name = (names or {})[m.group(1)]
            return list_response(
                [
                    {
                        "id": f"{m.group(1)}@{v}",
                        "created_at": "2026-10-01T00:00:00Z",
                        "description": "",
                        "directory": name,
                        "name": name,
                        "skill_id": m.group(1),
                        "type": "skill_version",
                        "version": v,
                    }
                    for v in ("1", "2")
                ]
            )
        raise AssertionError(f"unexpected {request.method} {path}")

    return build_fake_anthropic(handle)


async def _tenants(session: AsyncSession, *ids: uuid.UUID) -> None:
    for n, tenant in enumerate(ids):
        await make_tenant(session, id=tenant, workspace_id=f"owner-{n}")
    await session.commit()


def test_new_titles_carry_the_full_tenant_id() -> None:
    title = tenant_scoped_display_title(tenant_id=TENANT, name="notes", agent_name="bot")
    assert title == f"{TENANT.hex}-bot/notes"
    assert strip_tenant_prefix(tenant_id=TWIN, display_title=title) is None


async def test_a_short_prefix_title_is_its_one_tenants(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _tenants(db_session, TENANT, OTHER)
    client = _client([_skill("sk_old", "5ca1ab1e-bot/notes")])

    (row,) = await list_skills_strict(client, tenants=db_session_factory)

    assert row.display_title == tenant_scoped_display_title(
        tenant_id=TENANT, name="notes", agent_name="bot"
    ), "read under the title it would be written with now, so lookups by name find it"
    assert strip_tenant_prefix(tenant_id=TENANT, display_title=row.display_title) == "bot/notes"


async def test_a_short_prefix_two_tenants_share_is_nobodys(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _tenants(db_session, TENANT, TWIN)
    client = _client([_skill("sk_old", "5ca1ab1e-notes")])

    (row,) = await list_skills_strict(client, tenants=db_session_factory)

    assert row.display_title == "5ca1ab1e-notes"
    for tenant in (TENANT, TWIN):
        assert strip_tenant_prefix(tenant_id=tenant, display_title=row.display_title) is None
    assert "ma_index.skill_prefix_ambiguous" in capsys.readouterr().out


async def test_without_the_tenants_table_a_short_prefix_title_is_nobodys() -> None:
    (row,) = await list_skills_strict(_client([_skill("sk_old", "5ca1ab1e-notes")]))
    assert strip_tenant_prefix(tenant_id=TENANT, display_title=row.display_title or "") is None


async def test_a_fork_never_keeps_a_skill_its_tenant_does_not_own(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _tenants(db_session, TENANT, TWIN)
    pins = [{"type": "custom", "skill_id": "sk_old", "version": "latest"}]
    client = _client(
        [_skill("sk_old", "5ca1ab1e-old")],
        agents=[ma_agent(id="src", name="src", tenant_id=TENANT).model_dump(mode="json")],
    )

    split = await _split_scoped_skills(
        client, db_session_factory, tenant_id=TENANT, source_name="src", skills=pins
    )

    assert split.kept == [] and split.dropped == ["sk_old"], "left off the copy and named"


LONG_AGENT = "abcdefghijklmnopqrstuvwxyz"


async def test_a_shortened_title_still_clashes_on_the_name_it_mounts_under() -> None:
    scoped = tenant_scoped_display_title(tenant_id=TENANT, name="notes", agent_name=LONG_AGENT)
    assert "/notes" not in scoped, "the title cut the mount name off"
    library = tenant_scoped_display_title(tenant_id=TENANT, name="notes")
    client = _client(
        [_skill("sk_scoped", scoped), _skill("sk_library", library)],
        names={"sk_scoped": "notes"},
    )

    collision = await find_attach_mount_collision(
        client,
        tenant_id=TENANT,
        skills=[
            {"type": "custom", "skill_id": "sk_scoped"},
            {"type": "custom", "skill_id": "sk_library"},
        ],
    )
    assert collision is not None and "'notes'" in collision

    conflict = await find_conflicting_skill_mount(
        client, tenant_id=TENANT, name="notes", agent_name=None
    )
    assert conflict is not None and conflict.id == "sk_scoped", (
        "a library 'notes' would clash with the shortened agent-scoped 'notes'"
    )
