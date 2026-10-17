"""Lane workspace ownership is checked before any destructive call."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from daimon.adapters.cli import lane_wipe


class FakeResources:
    def __init__(self, rows: list[object] | None = None) -> None:
        self.rows = rows or []

    def list(self, **_kwargs: object):  # type: ignore[no-untyped-def]
        async def rows():  # type: ignore[no-untyped-def]
            for row in self.rows:
                yield row

        return rows()


class FakeAgents(FakeResources):
    async def create(self, **kwargs: object) -> object:
        marker = SimpleNamespace(name=kwargs["name"], metadata=kwargs["metadata"], archived_at=None)
        self.rows.append(marker)
        return marker


class FakeClient:
    def __init__(
        self,
        *,
        agents: list[object] | None = None,
        environments: list[object] | None = None,
        vaults: list[object] | None = None,
        memory_stores: list[object] | None = None,
    ) -> None:
        self.beta = SimpleNamespace(
            agents=FakeAgents(agents),
            environments=FakeResources(environments),
            vaults=FakeResources(vaults),
            memory_stores=FakeResources(memory_stores),
        )


def marker(lane: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=f"daimon-lane-marker-{lane}",
        metadata={"daimon_workspace": "disposable"},
        archived_at=None,
    )


@pytest.fixture(autouse=True)
def empty_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_ENVIRONMENT__NAME", "lane-fleet")

    async def list_skills(_client: object) -> list[object]:
        return []

    monkeypatch.setattr(lane_wipe, "list_skills_strict", list_skills)


@pytest.mark.asyncio
async def test_non_lane_environment_rejected_before_api_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_ENVIRONMENT__NAME", "staging")
    client = FakeClient()
    with pytest.raises(RuntimeError, match="DAIMON_ENVIRONMENT__NAME"):
        await lane_wipe.check_workspace(client, "lane-fleet")  # type: ignore[arg-type]
    assert client.beta.agents.rows == []


@pytest.mark.asyncio
async def test_fresh_workspace_is_claimed() -> None:
    client = FakeClient()
    await lane_wipe.check_workspace(client, "lane-fleet")  # type: ignore[arg-type]
    assert len(client.beta.agents.rows) == 1
    assert client.beta.agents.rows[0].name == "daimon-lane-marker-fleet"
    assert client.beta.agents.rows[0].metadata == {"daimon_workspace": "disposable"}


@pytest.mark.asyncio
async def test_own_marker_is_accepted_without_creating_another() -> None:
    client = FakeClient(agents=[marker("fleet"), SimpleNamespace(name="tenant-agent")])
    await lane_wipe.check_workspace(client, "lane-fleet")  # type: ignore[arg-type]
    assert len(client.beta.agents.rows) == 2


@pytest.mark.asyncio
async def test_foreign_nonempty_workspace_is_refused() -> None:
    client = FakeClient(agents=[SimpleNamespace(name="staging-agent")])
    with pytest.raises(
        lane_wipe.WorkspaceNotOwned, match="^workspace not owned by lane-fleet; refusing$"
    ):
        await lane_wipe.check_workspace(client, "lane-fleet")  # type: ignore[arg-type]
    assert len(client.beta.agents.rows) == 1


@pytest.mark.asyncio
async def test_other_lane_marker_is_refused() -> None:
    client = FakeClient(agents=[marker("other")])
    with pytest.raises(lane_wipe.WorkspaceNotOwned):
        await lane_wipe.check_workspace(client, "lane-fleet")  # type: ignore[arg-type]
    assert len(client.beta.agents.rows) == 1


@pytest.mark.asyncio
async def test_archived_own_marker_is_refused() -> None:
    archived = marker("fleet")
    archived.archived_at = "2026-01-01T00:00:00Z"
    client = FakeClient(agents=[archived])
    with pytest.raises(lane_wipe.WorkspaceNotOwned):
        await lane_wipe.check_workspace(client, "lane-fleet")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_custom_skill_blocks_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_skills(_client: object) -> list[object]:
        return [SimpleNamespace(source="custom")]

    monkeypatch.setattr(lane_wipe, "list_skills_strict", list_skills)
    client = FakeClient()
    with pytest.raises(lane_wipe.WorkspaceNotOwned):
        await lane_wipe.check_workspace(client, "lane-fleet")  # type: ignore[arg-type]
    assert client.beta.agents.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["environments", "vaults", "memory_stores"])
async def test_other_resource_blocks_claim(kind: str) -> None:
    client = FakeClient(**{kind: [object()]})
    with pytest.raises(lane_wipe.WorkspaceNotOwned):
        await lane_wipe.check_workspace(client, "lane-fleet")  # type: ignore[arg-type]
    assert client.beta.agents.rows == []


@pytest.mark.asyncio
async def test_wipe_recreates_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(agents=[marker("fleet"), SimpleNamespace(name="tenant-agent")])
    calls = 0

    async def delete(fake: FakeClient, *, i_understand_this_destroys_all_tenants: bool) -> None:
        nonlocal calls
        assert i_understand_this_destroys_all_tenants
        calls += 1
        fake.beta.agents.rows.clear()

    monkeypatch.setattr(lane_wipe, "delete_entire_workspace_for_testing", delete)
    await lane_wipe.wipe_workspace(client, "lane-fleet")  # type: ignore[arg-type]
    assert calls == 1
    assert len(client.beta.agents.rows) == 1
    assert client.beta.agents.rows[0].name == "daimon-lane-marker-fleet"
    assert client.beta.agents.rows[0].metadata == {"daimon_workspace": "disposable"}


def test_cli_refusal_has_only_generic_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DAIMON_ENVIRONMENT__NAME", "lane-fleet")

    async def refuse(_command: str, _lane: str) -> None:
        raise lane_wipe.WorkspaceNotOwned("workspace not owned by lane-fleet; refusing")

    monkeypatch.setattr(lane_wipe, "_run", refuse)
    assert lane_wipe.main(["wipe"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "workspace not owned by lane-fleet; refusing\n"
