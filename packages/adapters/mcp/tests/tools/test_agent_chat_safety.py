"""Agent-chat turns under the unattended tool gate, over a fake MA transport.

Every send is preceded by a re-gate of the session's own tool config; every
pause is answered by the sending process alone, from the current policy; a
read answers nothing, before or after a restart.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity, Role
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _agent_chat_safety as safety
from daimon.adapters.mcp.tools._session_access import hub_session_access
from daimon.adapters.mcp.tools.agent_chat import (
    _ask_impl,  # pyright: ignore[reportPrivateUsage]
    _continue_turn_impl,  # pyright: ignore[reportPrivateUsage]
    _get_session_impl,  # pyright: ignore[reportPrivateUsage]
    _start_turn_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.authz import Subject
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_CHANNEL
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.tool_safety import ToolAnnotations, ToolCall, ToolSafetyPolicy
from daimon.testing import ma_agent, ma_environment, ma_session
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, send_events_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TENANT_ID = uuid.uuid4()
_MA_AGENT_ID = "ag_safety"
_AGENT_UUID = derive_agent_uuid(tenant_id=_TENANT_ID, ma_agent_id=_MA_AGENT_ID)
_OWNER = uuid.uuid4()
_OTHER = uuid.uuid4()
_SESSION = "ses_safety"
_ENABLED = ToolSafetyPolicy(enabled=True)
_DISABLED = ToolSafetyPolicy(enabled=False)


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safety, "_POLL_S", 0.01)


def _linear_toolset(policy: str) -> dict[str, Any]:
    """A third-party toolset frozen as `policy`, with a per-tool override too."""
    return {
        "type": "mcp_toolset",
        "mcp_server_name": "linear",
        "configs": [
            {"name": "create_issue", "enabled": True, "permission_policy": {"type": policy}}
        ],
        "default_config": {"enabled": True, "permission_policy": {"type": policy}},
    }


class FakeSession:
    """One MA session: tool updates, the event log, and what was sent, in order.

    A `user.message` starts a turn that calls `tool` on `linear` and pauses;
    answering every pending call ends the turn. `calls` is the wire order of
    every mutating request.
    """

    def __init__(
        self,
        *,
        account: uuid.UUID = _OWNER,
        frozen: str = "always_allow",
        tool: str = "create_issue",
    ) -> None:
        self.metadata = {MA_METADATA_KEY_ACCOUNT: str(account)}
        self.tools = [_linear_toolset(frozen)]
        self.servers = [{"name": "linear", "type": "url", "url": "https://linear.example/mcp"}]
        self.tool = tool
        self.events: list[dict[str, Any]] = []
        self.status = "idle"
        self.calls: list[tuple[str, Any]] = []
        self._ids = 0

    def _id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}_{self._ids:03d}"

    def pause(self, *, boundary: str | None = None) -> str:
        """Append a turn paused on one call; returns its boundary id."""
        boundary = boundary or self._id("evt_user")
        tool_use = self._id("evt_tool")
        self.events += [
            {"id": boundary, "type": "user.message", "content": [{"type": "text", "text": "hi"}]},
            {
                "id": tool_use,
                "type": "agent.mcp_tool_use",
                "mcp_server_name": "linear",
                "name": self.tool,
                "input": {},
                "evaluated_permission": "ask",
            },
            {
                "id": self._id("evt_pause"),
                "type": "session.status_idle",
                "stop_reason": {"type": "requires_action", "event_ids": [tool_use]},
            },
        ]
        return boundary

    @property
    def confirmations(self) -> list[dict[str, Any]]:
        return [body for kind, body in self.calls if kind == "confirm"]

    @property
    def model(self) -> Any:
        return ma_session(
            id=_SESSION,
            agent=ma_agent(id=_MA_AGENT_ID, tools=self.tools, mcp_servers=self.servers),
            status=self.status,
            metadata=self.metadata,
        )

    def router(self) -> MARouter:
        router = MARouter()
        router.add("GET", rf"/v1/sessions/{_SESSION}$", self._retrieve)
        router.add("POST", rf"/v1/sessions/{_SESSION}$", self._update)
        router.add("GET", rf"/v1/sessions/{_SESSION}/events$", self._list)
        router.add("POST", rf"/v1/sessions/{_SESSION}/events$", self._send)
        return router

    def _retrieve(self, _r: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        return httpx.Response(200, json=self.model.model_dump(mode="json"))

    def _update(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        agent = json_body(request)["agent"]
        self.calls.append(("update", agent))
        self.tools = agent["tools"]
        self.servers = agent["mcp_servers"]
        return httpx.Response(200, json=self.model.model_dump(mode="json"))

    def _list(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        params = request.url.params
        events = list(self.events)
        types = params.get_list("types[]") or params.get_list("types")
        if types:
            events = [e for e in events if e["type"] in types]
        if params.get("order") == "desc":
            events.reverse()
        if (limit := params.get("limit")) is not None:
            events = events[: int(limit)]
        return httpx.Response(200, json={"data": events, "next_page": None})

    def _send(self, request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        echoed: list[dict[str, Any]] = []
        for event in json_body(request)["events"]:
            if event["type"] == "user.message":
                self.calls.append(("message", event))
                boundary = self.pause()
                echoed.append({**event, "id": boundary})
            elif event["type"] == "user.tool_confirmation":
                self.calls.append(("confirm", event))
                self.events.append({**event, "id": self._id("evt_confirm")})
        pending = self._pending()
        if self.confirmations and not pending and self.events[-1]["type"] != "session.status_idle":
            self.events += [
                {
                    "id": self._id("evt_msg"),
                    "type": "agent.message",
                    "content": [{"type": "text", "text": "done"}],
                },
                {
                    "id": self._id("evt_idle"),
                    "type": "session.status_idle",
                    "stop_reason": {"type": "end_turn"},
                },
            ]
        return send_events_response(data=echoed)

    def _pending(self) -> set[str]:
        asked: set[str] = set()
        for event in self.events:
            if event["type"] == "session.status_idle" and event["stop_reason"]["type"] == (
                "requires_action"
            ):
                asked |= set(event["stop_reason"]["event_ids"])
            if event["type"] == "user.tool_confirmation":
                asked.discard(event["tool_use_id"])
        return asked


def _runtime(
    fake: FakeSession,
    factory: async_sessionmaker[AsyncSession],
    policy: ToolSafetyPolicy,
) -> McpRuntime:
    settings = MagicMock()
    settings.tool_safety = policy
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.github.fallback_pat = None
    return McpRuntime(
        session_factory=factory,
        client=build_fake_anthropic(fake.router().dispatch),  # type: ignore[arg-type]
        settings=settings,  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(environment_name="production"),
    )


def _auth(account: uuid.UUID = _OWNER) -> AuthIdentity:
    return AuthIdentity(
        account_id=account, tenant_id=_TENANT_ID, role=Role.USER, agent_id=_AGENT_UUID
    )


async def _settle(runtime: McpRuntime) -> None:
    """Wait for this runtime's confirmation worker, if one is running."""
    worker = safety._workers.get((id(runtime), _SESSION))  # pyright: ignore[reportPrivateUsage]
    if worker is not None:
        await asyncio.wait_for(worker, timeout=5)


def _restart() -> None:
    """A new process: no worker survives."""
    for worker in safety._workers.values():  # pyright: ignore[reportPrivateUsage]
        worker.cancel()
    safety._workers.clear()  # pyright: ignore[reportPrivateUsage]


def _asks(tools: list[dict[str, Any]]) -> bool:
    toolset = tools[0]
    return toolset["default_config"]["permission_policy"]["type"] == "always_ask" and all(
        c["permission_policy"]["type"] == "always_ask" for c in toolset["configs"]
    )


@pytest.fixture
def factory(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    """A pooled factory: the worker and the request each take a connection."""
    return committing_sessionmaker


async def _start(runtime: McpRuntime, fake: FakeSession, auth: AuthIdentity) -> dict[str, str]:
    agent = ma_agent(id=_MA_AGENT_ID, tools=fake.tools, mcp_servers=fake.servers)
    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._resolve_ma_agent", AsyncMock(return_value=agent)
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat.find_environment_by_daimon_tag",
            AsyncMock(return_value=ma_environment(name="production")),
        ),
        # Creation's own gating is covered in core (test_sessions*); this
        # hands back a session whose frozen config allows, as an old one would.
        patch(
            "daimon.adapters.mcp.tools.agent_chat.create_session",
            AsyncMock(return_value=fake.model),
        ),
    ):
        return await _start_turn_impl(runtime, auth, "hi")


Entry = Callable[[McpRuntime, FakeSession], Any]


async def _entry_start(runtime: McpRuntime, fake: FakeSession) -> None:
    await _start(runtime, fake, _auth())


async def _entry_continue(runtime: McpRuntime, fake: FakeSession) -> None:
    await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")


async def _entry_ask_existing(runtime: McpRuntime, fake: FakeSession) -> None:
    result = await _ask_impl(runtime, _auth(), "hi", handle=_SESSION, poll_interval_seconds=0.01)
    assert result.message == "done"


async def _entry_hub_continue(runtime: McpRuntime, fake: FakeSession) -> None:
    with hub_session_access(Subject(platform_user_id="U1"), "continue"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")


@pytest.mark.parametrize(
    "entry",
    [_entry_start, _entry_continue, _entry_ask_existing, _entry_hub_continue],
    ids=["start_turn", "continue_turn", "ask_existing", "hub_continue_turn"],
)
@pytest.mark.parametrize(
    ("tool", "policy", "expected"),
    [
        ("create_issue", _ENABLED, "deny"),
        ("get_issue", _ENABLED, "allow"),
        ("opaque_query", _ENABLED, "deny"),
        (
            "create_issue",
            ToolSafetyPolicy(enabled=True, unattended_writes=("linear/create_issue",)),
            "allow",
        ),
    ],
)
async def test_every_send_is_regated_and_its_pause_decided_by_current_policy(
    entry: Entry,
    tool: str,
    policy: ToolSafetyPolicy,
    expected: str,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A frozen `always_allow` (default and per-tool) is rewritten before the
    message, so the call pauses and the unattended decider answers it."""
    fake = FakeSession(tool=tool)
    runtime = _runtime(fake, factory, policy)
    await entry(runtime, fake)
    await _settle(runtime)

    kinds = [kind for kind, _ in fake.calls]
    assert kinds.index("update") < kinds.index("message")
    assert _asks(fake.calls[kinds.index("update")][1]["tools"])
    assert [c["result"] for c in fake.confirmations] == [expected]


async def test_ask_new_session_is_gated_before_its_message(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    agent = ma_agent(id=_MA_AGENT_ID, tools=fake.tools, mcp_servers=fake.servers)
    creator = AsyncMock(return_value=fake.model)
    with (
        patch(
            "daimon.adapters.mcp.tools.agent_chat._resolve_ma_agent", AsyncMock(return_value=agent)
        ),
        patch(
            "daimon.adapters.mcp.tools.agent_chat.find_environment_by_daimon_tag",
            AsyncMock(return_value=ma_environment(name="production")),
        ),
        patch("daimon.adapters.mcp.tools.agent_chat.create_session", creator),
    ):
        result = await _ask_impl(runtime, _auth(), "hi", poll_interval_seconds=0.01)
    assert result.message == "done"
    assert creator.call_args.kwargs["tool_safety"] == _ENABLED
    kinds = [kind for kind, _ in fake.calls]
    assert kinds.index("update") < kinds.index("message")
    assert [c["result"] for c in fake.confirmations] == ["deny"]


async def test_disabled_policy_is_the_explicit_off_switch(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Off leaves the session as it is and allows what it pauses on."""
    fake = FakeSession(frozen="always_ask")
    runtime = _runtime(fake, factory, _DISABLED)
    await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    await _settle(runtime)
    assert [kind for kind, _ in fake.calls] == ["message", "confirm"]
    assert [c["result"] for c in fake.confirmations] == ["allow"]


async def test_restart_then_read_stays_paused_and_answers_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_ask")
    fake.pause()
    runtime = _runtime(fake, factory, _DISABLED)
    _restart()
    for _ in range(3):
        info = await _get_session_impl(runtime, _auth(), _SESSION)
        assert (info.status, info.hint) == ("paused", safety.PAUSED_HINT)
    await asyncio.sleep(0.05)
    assert fake.calls == []
    assert not safety._workers  # pyright: ignore[reportPrivateUsage]


async def test_hub_admin_read_of_another_accounts_paused_channel_session_answers_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Review 2, finding 2: a read grant never drives another account's turn."""
    fake = FakeSession(account=_OTHER, frozen="always_ask")
    fake.metadata[MA_METADATA_KEY_CHANNEL] = "C1"
    fake.pause()
    runtime = _runtime(fake, factory, _ENABLED)
    admin = Subject(is_admin=True, platform_user_id="UA")
    with hub_session_access(admin, "read"):
        info = await _get_session_impl(runtime, _auth(), _SESSION)
    assert info.status == "paused"
    with hub_session_access(admin, "continue"), pytest.raises(ToolError, match="not found"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    await asyncio.sleep(0.05)
    assert fake.calls == []


async def test_owner_read_of_a_paused_attended_session_answers_nothing(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_ask")
    fake.metadata[MA_METADATA_KEY_CHANNEL] = "C1"
    fake.pause()
    runtime = _runtime(fake, factory, _ENABLED)
    info = await _get_session_impl(runtime, _auth(), _SESSION)
    assert info.status == "paused"
    await asyncio.sleep(0.05)
    assert fake.calls == []


async def test_another_account_cannot_resume_a_paused_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(account=_OTHER, frozen="always_ask")
    fake.pause()
    runtime = _runtime(fake, factory, _DISABLED)
    with pytest.raises(ToolError, match="not found"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    assert fake.calls == []


@pytest.mark.parametrize(("policy", "expected"), [(_ENABLED, "deny"), (_DISABLED, "allow")])
async def test_owner_continue_after_restart_resumes_under_current_policy(
    policy: ToolSafetyPolicy, expected: str, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Review 1, finding 3: the policy now in force decides, enabled or not."""
    fake = FakeSession()
    boundary = fake.pause()
    runtime = _runtime(fake, factory, policy)
    _restart()
    started = await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    await _settle(runtime)
    assert started["turn_event_id"] == boundary
    assert [c["result"] for c in fake.confirmations] == [expected]
    assert "message" not in [kind for kind, _ in fake.calls]
    if policy.enabled:
        assert fake.calls[0][0] == "update" and _asks(fake.calls[0][1]["tools"])
    else:
        assert fake.calls[0][0] == "confirm"
    assert (await _get_session_impl(runtime, _auth(), _SESSION)).status == "idle"


async def test_continue_with_a_message_resumes_first_then_sends(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_ask")
    old = fake.pause()
    runtime = _runtime(fake, factory, _ENABLED)
    started = await _continue_turn_impl(runtime, _auth(), _SESSION, "next")
    await _settle(runtime)
    assert started["turn_event_id"] != old
    assert [kind for kind, _ in fake.calls] == ["confirm", "message", "confirm"]


async def test_continue_with_a_message_refuses_when_the_resumed_turn_runs_on(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSession(frozen="always_ask")
    fake.pause()
    runtime = _runtime(fake, factory, _ENABLED)
    # The resumed turn never ends here.
    monkeypatch.setattr(FakeSession, "_pending", lambda self: {"evt_still"})
    with pytest.raises(ToolError, match="not sent"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, "next", resume_wait_s=0.1)
    assert "message" not in [kind for kind, _ in fake.calls]


async def test_continue_without_a_message_needs_a_paused_session(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    with pytest.raises(ToolError, match="message is required"):
        await _continue_turn_impl(runtime, _auth(), _SESSION, None)
    assert "message" not in [kind for kind, _ in fake.calls]


async def test_worker_answers_only_its_own_turn(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pause under another boundary (an attended card, a newer turn) is not its own."""
    fake = FakeSession(frozen="always_ask")
    fake.pause()
    runtime = _runtime(fake, factory, _DISABLED)
    worker = safety.watch_turn(runtime, _auth(), _SESSION, turn_event_id="evt_someone_else")
    await asyncio.sleep(0.1)
    assert not worker.done()
    worker.cancel()
    assert fake.calls == []


async def test_running_worker_reads_as_running(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    fake = FakeSession(frozen="always_ask")
    fake.pause()
    runtime = _runtime(fake, factory, _ENABLED)
    worker = safety.watch_turn(runtime, _auth(), _SESSION, turn_event_id="evt_someone_else")
    try:
        assert (await _get_session_impl(runtime, _auth(), _SESSION)).status == "running"
    finally:
        worker.cancel()


@pytest.mark.parametrize(
    ("tool", "hints", "expected"),
    [
        ("memory_flag_list", None, "deny"),
        ("memory_flag_list", ToolAnnotations(read_only_hint=True), "allow"),
        ("memory_flag_list", ToolAnnotations(read_only_hint=True, destructive_hint=True), "deny"),
    ],
)
async def test_trusted_annotation_seam_feeds_the_unattended_verdict(
    tool: str,
    hints: ToolAnnotations | None,
    expected: str,
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A noun-first read needs a trusted hint; a destructive hint wins over read-only."""
    looked_up: list[str] = []

    async def lookup(call: ToolCall) -> ToolAnnotations | None:
        looked_up.append(call.key)
        return hints

    monkeypatch.setattr(safety, "_annotations_for", lambda *_: lookup)
    fake = FakeSession(tool=tool)
    runtime = _runtime(fake, factory, _ENABLED)
    await _continue_turn_impl(runtime, _auth(), _SESSION, "hi")
    await _settle(runtime)
    assert looked_up == [f"linear/{tool}"]
    assert [c["result"] for c in fake.confirmations] == [expected]


async def test_no_annotations_are_trusted_without_the_lookup(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Nothing on this build answers for a server: a claimed read is still a write."""
    fake = FakeSession()
    runtime = _runtime(fake, factory, _ENABLED)
    assert safety._annotations_for(runtime, _auth(), fake.model) is None  # pyright: ignore[reportPrivateUsage]
