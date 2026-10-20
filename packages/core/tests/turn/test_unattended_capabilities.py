"""Unattended capability limits, exercised through a fake MA transport."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from copy import deepcopy
from typing import Any, cast, get_args
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from anthropic._models import construct_type  # pyright: ignore[reportPrivateImportUsage]
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from daimon.core.mux_backend import platform_scope
from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
from daimon.core.turn import unattended_capabilities as caps
from daimon.core.turn.approvals import chat_tool_confirmation
from daimon.core.turn.driver import run_turn, unattended_sends
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.posture import BillingExempt, PolicyApproval
from daimon.core.turn.unattended import gate_session_snapshot, session_gate, unattended_posture
from daimon.core.turn.unattended_capabilities import (
    ENTRY_POINT_POLICY,
    UnattendedEntryPoint,
    capability_posture,
    constrain_capabilities,
    require_allowed_capabilities,
    restore_capabilities,
    unattended_entry_point,
)
from daimon.testing import ma_agent, ma_session
from daimon.testing.ma import MARouter, build_fake_anthropic, json_body, send_events_response
from daimon.testing.ma_sessions import session_turn_sse
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.ids import Scope

from .conftest import make_status_idle

ENTRY_POINTS: tuple[UnattendedEntryPoint, ...] = get_args(UnattendedEntryPoint)
ALLOW = {"enabled": True, "permission_policy": {"type": "always_allow"}}
BUILTINS = ("bash", "write", "edit")


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "agent_toolset_20260401",
            "default_config": ALLOW,
            "configs": [{"name": name, **ALLOW} for name in BUILTINS],
        },
        {
            "type": "mcp_toolset",
            "mcp_server_name": "linear",
            "default_config": ALLOW,
            "configs": [{"name": "create_issue", **ALLOW}],
        },
    ]


SERVERS = [{"name": "linear", "type": "url", "url": "https://linear.example/mcp"}]


def _root_session(**agent: Any) -> BetaManagedAgentsSession:
    agent.setdefault("tools", _tools())
    agent.setdefault("mcp_servers", deepcopy(SERVERS))
    return ma_session(id="ses_coordinator", agent=ma_agent(id="agent_coordinator", **agent))


def _coordinator_session() -> BetaManagedAgentsSession:
    session = _root_session().model_dump(mode="json")
    child = ma_session(
        agent=ma_agent(id="agent_worker", tools=_tools(), mcp_servers=deepcopy(SERVERS))
    ).agent.model_dump(mode="json", exclude={"multiagent"})
    session["agent"]["multiagent"] = {"type": "coordinator", "agents": [child]}
    # Validate the full provider shape: the roster contains definitions, not IDs.
    return BetaManagedAgentsSession.model_validate(session)


def _with_agent_field(**fields: Any) -> BetaManagedAgentsSession:
    """A snapshot as the SDK parses a provider response: unvalidated, extras kept."""
    session = _root_session().model_dump(mode="json")
    session["agent"].update(fields)
    return cast(
        BetaManagedAgentsSession, construct_type(type_=BetaManagedAgentsSession, value=session)
    )


_FILE: dict[str, Any] = {
    "id": "sesrsc_1",
    "type": "file",
    "file_id": "file_1",
    "mount_path": "/mnt/session/uploads/a.txt",
    "created_at": "2026-10-01T00:00:00Z",
    "updated_at": "2026-10-01T00:00:00Z",
}


def _mutated(change: Callable[[dict[str, Any]], object]) -> BetaManagedAgentsSession:
    """A root snapshot changed in its raw provider form, then parsed as the SDK does."""
    session = _root_session().model_dump(mode="json")
    change(session)
    return cast(
        BetaManagedAgentsSession, construct_type(type_=BetaManagedAgentsSession, value=session)
    )


def _set(path: str, value: object) -> Callable[[dict[str, Any]], object]:
    """Set a dotted path (list indices as numbers) in a raw snapshot."""

    def change(session: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        node: Any = session
        for key in parents:
            node = node[int(key)] if key.isdigit() else node[key]
        node[int(leaf) if leaf.isdigit() else leaf] = value

    return change


_HIDDEN = {"future_capability": True}
_ASKING = {"enabled": True, "permission_policy": {"type": "always_ask"}}

# A reviewed scalar that became an object or a list, at each kind of node.
_HIDDEN_SHAPES: dict[str, Callable[[dict[str, Any]], object]] = {
    "effort_list": _set("agent.model.effort", [_HIDDEN]),
    "effort_type_object": _set("agent.model.effort", {"type": _HIDDEN}),
    "effort_unknown_level": _set("agent.model.effort", "unbounded"),
    "inference_geo_object": _set("agent.model.inference_geo", _HIDDEN),
    "speed_list": _set("agent.model.speed", ["fast", _HIDDEN]),
    "model_id_object": _set("agent.model.id", _HIDDEN),
    "agent_version_object": _set("agent.version", _HIDDEN),
    "system_list": _set("agent.system", [_HIDDEN]),
    "server_url_object": _set("agent.mcp_servers.0.url", _HIDDEN),
    "builtin_enabled_object": _set("agent.tools.0.default_config.enabled", _HIDDEN),
    "builtin_config_enabled_list": _set("agent.tools.0.configs.0.enabled", [_HIDDEN]),
    "mcp_config_name_object": _set("agent.tools.1.configs.0.name", _HIDDEN),
    "policy_type_object": _set("agent.tools.1.default_config.permission_policy.type", _HIDDEN),
    "policy_list": _set("agent.tools.1.default_config.permission_policy", [_HIDDEN]),
    "tool_null": _set("agent.tools.1", None),
    "tool_list": _set("agent.tools.1", [_HIDDEN]),
    "vault_id_object": _set("vault_ids", [_HIDDEN]),
    "resource_mount_object": lambda s: s["resources"].append({**_FILE, "mount_path": _HIDDEN}),
    "checkout_name_list": lambda s: s["resources"].append(
        {
            "id": "sesrsc_2",
            "type": "github_repository",
            "url": "https://github.com/o/r",
            "mount_path": "/workspace/r",
            "checkout": {"type": "branch", "name": [_HIDDEN]},
            "created_at": "2026-10-01T00:00:00Z",
            "updated_at": "2026-10-01T00:00:00Z",
        }
    ),
    # Nothing to rewrite (already asking, no built-ins): no update would
    # run the native schema, so admission alone must catch it.
    "no_change_enabled_object": lambda s: s["agent"].update(
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "linear",
                "default_config": {**_ASKING, "enabled": _HIDDEN},
                "configs": [{"name": "create_issue", **_ASKING}],
            }
        ]
    ),
}


class _FutureModelConfig(BetaManagedAgentsModelConfig):
    """An SDK upgrade that starts parsing a field this release never reviewed."""

    budget: dict[str, Any] | None = None


def _sdk_recognized_field() -> BetaManagedAgentsSession:
    session = _root_session()
    model = _FutureModelConfig.model_validate({"id": session.agent.model.id})
    return session.model_copy(update={"agent": session.agent.model_copy(update={"model": model})})


def _can_run_without_decider(toolset: dict[str, Any], name: str) -> bool:
    config = next((c for c in toolset["configs"] if c["name"] == name), None)
    effective: dict[str, Any] = {**toolset["default_config"], **(config or {})}
    return effective["enabled"] and effective["permission_policy"]["type"] != "always_ask"


def _builtins_runnable(tools: list[dict[str, Any]]) -> bool:
    return any(
        _can_run_without_decider(t, name)
        for t in tools
        if t["type"] == "agent_toolset_20260401"
        for name in BUILTINS
    )


def _scope() -> Scope:
    return Scope(
        tenant_id=str(uuid.uuid4()),
        account_id=str(uuid.uuid4()),
        principal_id="owner",
        authorization_id="grant",
    )


class _Provider:
    """A fake MA session that applies only supplied update fields, as MA does."""

    def __init__(self, snapshot: BetaManagedAgentsSession) -> None:
        self.state = snapshot.model_dump(mode="json")
        self.updates: list[dict[str, Any]] = []
        self.at_send: list[dict[str, Any]] = []
        router = MARouter()
        router.add(
            "GET",
            rf"/v1/sessions/{snapshot.id}$",
            lambda _r, _m: httpx.Response(200, json=self.state),
        )
        router.add("POST", rf"/v1/sessions/{snapshot.id}$", self._update)
        router.add("POST", rf"/v1/sessions/{snapshot.id}/events$", self._send)
        router.add(
            "GET",
            rf"/v1/sessions/{snapshot.id}/events/stream$",
            lambda _r, _m: session_turn_sse(make_status_idle(event_id="sevt_done")),
        )
        self.client = build_fake_anthropic(router.dispatch)

    def _update(self, request: httpx.Request, _match: Any) -> httpx.Response:
        agent = json_body(request)["agent"]
        self.updates.append(agent)
        self.state["agent"].update(agent)
        return httpx.Response(200, json=self.state)

    def _send(self, request: httpx.Request, _match: Any) -> httpx.Response:
        assert json_body(request)["events"][0]["type"] == "user.message"
        self.at_send.append(deepcopy(self.state["agent"]))
        return send_events_response([])


async def _drive(
    snapshot: BetaManagedAgentsSession,
    entry_point: UnattendedEntryPoint | None,
    *,
    policy: ToolSafetyPolicy | None = None,
    public_url: str | None = None,
    routine_sandbox_tools: bool = False,
) -> tuple[_Provider, Any]:
    provider = _Provider(snapshot)
    policy = policy or ToolSafetyPolicy(enabled=True)
    try:
        with unattended_sends(
            session_gate(
                provider.client,
                snapshot.id,
                policy,
                scope=platform_scope("unattended capability regression"),
                public_url=public_url,
                session_factory=None,
                tenant_id=None,
                entry_point=entry_point,
                routine_sandbox_tools=routine_sandbox_tools,
            )
        ):
            result = await run_turn(
                anthropic=provider.client,
                session_id=snapshot.id,
                user_message="nightly work",
                lifecycle=RecordingLifecycle(),
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="cli-operator-run"),
                path="legacy",
                tool_confirmation=unattended_posture(
                    policy,
                    session=snapshot,
                    public_url=public_url,
                    session_factory=None,
                    tenant_id=None,
                    entry_point=entry_point,
                    routine_sandbox_tools=routine_sandbox_tools,
                ),
            )
    except AdmissionDenied as err:
        result = err
    finally:
        await provider.client.close()
    return provider, result


@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
async def test_coordinator_session_is_refused_before_any_send(
    entry_point: UnattendedEntryPoint | None,
) -> None:
    """MA cannot drop `multiagent` from a live session, so the turn never starts.

    The roster's children carry always_allow built-ins and MCP writes; neither
    the root update nor the message is sent.
    """
    provider, result = await _drive(_coordinator_session(), entry_point)

    error = result if isinstance(result, AdmissionDenied) else result.error
    assert isinstance(error, AdmissionDenied)
    assert error.reason == "backend_unsupported"
    assert provider.updates == []
    assert provider.at_send == []


@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
async def test_unattended_send_always_disables_builtins(
    entry_point: UnattendedEntryPoint | None,
) -> None:
    """A real driver send observes the provider's post-gate effective snapshot."""
    provider, result = await _drive(_root_session(), entry_point)

    assert result.error is None
    [agent] = provider.at_send
    assert provider.updates, "the unattended gate must run before the message"
    root_mcp = next(t for t in agent["tools"] if t["type"] == "mcp_toolset")
    assert not _can_run_without_decider(root_mcp, "create_issue")
    assert not _builtins_runnable(agent["tools"])
    [builtin] = [t for t in agent["tools"] if t["type"] == "agent_toolset_20260401"]
    assert builtin["default_config"]["enabled"] is False
    assert all(config["enabled"] is False for config in builtin["configs"])


def test_entry_point_policy_table() -> None:
    """Every unattended entry point disables the sandbox; watches also deny skills."""
    assert set(ENTRY_POINT_POLICY) == set(ENTRY_POINTS)
    off = {name for name, policy in ENTRY_POINT_POLICY.items() if not policy.allow_builtins}
    assert off == set(ENTRY_POINTS)
    assert not ENTRY_POINT_POLICY["watch"].allow_skills
    assert caps.capability_policy(None) == caps.CapabilityPolicy(
        allow_builtins=False, allow_skills=False
    )


@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
def test_only_a_routine_takes_the_sandbox_opt_in(entry_point: UnattendedEntryPoint | None) -> None:
    """Watches, wakes, chat, workers, `daimon run` and checkpoints ignore the flag."""
    opted = caps.capability_policy(entry_point, routine_sandbox_tools=True)
    assert opted.allow_builtins is (entry_point == "routine")
    assert not caps.capability_policy(entry_point).allow_builtins
    if entry_point is not None:
        assert opted.allow_skills == ENTRY_POINT_POLICY[entry_point].allow_skills


def _with_custom_tool() -> BetaManagedAgentsSession:
    return _root_session(
        tools=[
            *_tools(),
            {
                "type": "custom",
                "name": "local_fn",
                "description": "d",
                "input_schema": {"type": "object"},
            },
        ]
    )


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
async def test_routine_opt_in_keeps_builtins_on_the_wire(entry_point: UnattendedEntryPoint) -> None:
    """An opted-in routine sends its built-ins as authored; every other entry point
    still disables them, and custom tools are removed either way."""
    provider, result = await _drive(_with_custom_tool(), entry_point, routine_sandbox_tools=True)

    assert result.error is None
    [agent] = provider.at_send
    assert _builtins_runnable(agent["tools"]) is (entry_point == "routine")
    assert all(t["type"] != "custom" for t in agent["tools"])
    root_mcp = next(t for t in agent["tools"] if t["type"] == "mcp_toolset")
    assert not _can_run_without_decider(root_mcp, "create_issue")


@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
async def test_decider_lets_builtins_through_only_for_an_opted_in_routine(
    entry_point: UnattendedEntryPoint | None,
) -> None:
    inner = AsyncMock(return_value=caps.ToolConfirmationResult(allow=True))
    opted_in = entry_point == "routine"
    posture = caps.capability_posture(
        PolicyApproval(decide=inner),
        allow_builtins=caps.capability_policy(
            entry_point, routine_sandbox_tools=True
        ).allow_builtins,
    )
    decision = await posture.decide(
        ToolCall(tool_use_id="tu_builtin", server_name=None, tool_name="bash")
    )
    assert decision.allow is opted_in
    assert inner.await_count == (1 if opted_in else 0)


@pytest.mark.parametrize(
    ("origin", "entry_point"),
    [("worker", "fleet_worker"), ("routine", "routine"), ("chat", "wake"), ("relay", "wake")],
)
def test_prepared_origin_maps_to_entry_point(origin: Any, entry_point: str) -> None:
    assert unattended_entry_point(origin) == entry_point


@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
@pytest.mark.parametrize("name", BUILTINS)
async def test_unattended_decider_always_refuses_builtins(
    entry_point: UnattendedEntryPoint | None, name: str
) -> None:
    """Asking is not enough: with built-ins off the decider refuses them too."""
    posture = unattended_posture(
        ToolSafetyPolicy(enabled=True),
        session=_root_session(),
        public_url=None,
        session_factory=None,
        tenant_id=None,
        entry_point=entry_point,
    )
    decision = await posture.decide(
        ToolCall(tool_use_id="tu_builtin", server_name=None, tool_name=name)
    )
    assert decision.allow is False


async def test_attended_chat_posture_is_unchanged() -> None:
    """Only unattended turns go through the capability limits."""
    posture = chat_tool_confirmation(
        ToolSafetyPolicy(enabled=True),
        requester_platform_user_id="U1",
        confirm=None,
    )
    assert isinstance(posture, PolicyApproval)
    decision = await posture.decide(ToolCall(tool_use_id="tu", server_name=None, tool_name="bash"))
    assert decision.allow


@pytest.mark.parametrize(
    "snapshot",
    [
        pytest.param(lambda: _with_agent_field(sandbox_policy={"network": "open"}), id="extra"),
        pytest.param(
            lambda: _with_agent_field(
                tools=[{"type": "computer_use_20270101", "default_config": ALLOW}]
            ),
            id="tool_type",
        ),
        pytest.param(
            lambda: _with_agent_field(
                tools=[{**_tools()[1], "grants": ["everything"]}],
            ),
            id="tool_field",
        ),
        pytest.param(
            lambda: _with_agent_field(
                mcp_servers=[{**SERVERS[0], "headers": {"x": "y"}}],
            ),
            id="server_field",
        ),
        pytest.param(_coordinator_session, id="multiagent"),
        pytest.param(lambda: _with_agent_field(sandbox_policy={}), id="empty_agent_field"),
        pytest.param(lambda: _with_agent_field(sandbox_policy=None), id="null_agent_field"),
        pytest.param(
            lambda: _mutated(lambda s: s["agent"]["tools"][0]["default_config"].update(net=1)),
            id="builtin_default_config_field",
        ),
        pytest.param(
            lambda: _mutated(lambda s: s["agent"]["tools"][0]["configs"][0].update(net={})),
            id="builtin_config_field",
        ),
        pytest.param(
            lambda: _mutated(lambda s: s["agent"]["tools"][0]["configs"][0].update(type="write")),
            id="builtin_config_foreign_type",
        ),
        pytest.param(
            lambda: _mutated(lambda s: s["agent"]["tools"][1]["configs"][0].update(scope="*")),
            id="mcp_config_field",
        ),
        pytest.param(
            lambda: _mutated(
                lambda s: s["agent"]["tools"][1]["default_config"]["permission_policy"].update(
                    budget=5
                )
            ),
            id="permission_policy_field",
        ),
        pytest.param(
            lambda: _mutated(
                lambda s: s["agent"]["tools"][1]["configs"][0].update(
                    permission_policy={"type": "allow_within_budget"}
                )
            ),
            id="permission_policy_type",
        ),
        pytest.param(
            lambda: _mutated(
                lambda s: s["agent"]["tools"][1]["configs"].append(
                    {"name": "create_issue", **ALLOW}
                )
            ),
            id="duplicate_config",
        ),
        pytest.param(
            lambda: _mutated(lambda s: s["agent"]["mcp_servers"].append(dict(SERVERS[0]))),
            id="duplicate_server",
        ),
        pytest.param(lambda: _mutated(lambda s: s["agent"]["model"].update(tier={})), id="model"),
        pytest.param(
            lambda: _mutated(
                lambda s: s["agent"]["model"].update(effort={"type": "max", "tools": True})
            ),
            id="model_effort_field",
        ),
        pytest.param(lambda: _mutated(lambda s: s.update(sandbox={})), id="session_field"),
        pytest.param(
            lambda: _mutated(lambda s: s["resources"].append({**_FILE, "exec": True})),
            id="resource_field",
        ),
        pytest.param(
            lambda: _mutated(lambda s: s["resources"].append({**_FILE, "type": "shell"})),
            id="resource_type",
        ),
        pytest.param(_sdk_recognized_field, id="sdk_recognized_field"),
        *(
            pytest.param(lambda change=change: _mutated(change), id=f"shape_{name}")
            for name, change in _HIDDEN_SHAPES.items()
        ),
    ],
)
@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
async def test_unknown_capability_is_refused_before_mutation(
    snapshot: Any, entry_point: UnattendedEntryPoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    update = AsyncMock()
    monkeypatch.setattr(caps, "update_session_record", update)
    session = snapshot()
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        require_allowed_capabilities(session, allow_skills=True)
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await gate_session_snapshot(
            MagicMock(),
            session,
            ToolSafetyPolicy(enabled=True),
            scope=_scope(),
            public_url=None,
            session_factory=None,
            tenant_id=None,
            entry_point=entry_point,
        )
    update.assert_not_awaited()


@pytest.mark.parametrize("entry_point", ENTRY_POINTS)
async def test_skills_follow_the_entry_point_policy(
    entry_point: UnattendedEntryPoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    update = AsyncMock()
    monkeypatch.setattr(caps, "update_session_record", update)
    session = _root_session(skills=[{"type": "custom", "skill_id": "skill_x", "version": "1"}])
    gate = gate_session_snapshot(
        MagicMock(),
        session,
        ToolSafetyPolicy(enabled=True),
        scope=_scope(),
        public_url=None,
        session_factory=None,
        tenant_id=None,
        entry_point=entry_point,
    )
    if ENTRY_POINT_POLICY[entry_point].allow_skills:
        await gate
        update.assert_awaited_once()
    else:
        with pytest.raises(AdmissionDenied, match="backend_unsupported"):
            await gate
        update.assert_not_awaited()


async def test_allowlist_keeps_only_named_tools_and_asks() -> None:
    session = _root_session(
        tools=[
            *_tools(),
            {
                "type": "custom",
                "name": "local",
                "description": "d",
                "input_schema": {"type": "object"},
            },
            {
                "type": "mcp_toolset",
                "mcp_server_name": "github",
                "default_config": ALLOW,
                "configs": [],
            },
        ],
        mcp_servers=[*SERVERS, {"name": "github", "type": "url", "url": "https://gh.example"}],
    )
    constrained = await constrain_capabilities(
        session,
        ToolSafetyPolicy(enabled=True),
        public_url=None,
        session_factory=None,
        tenant_id=None,
        tool_allowlist=frozenset({"linear/get_issue"}),
    )
    tools = constrained.send.tools
    assert [t["type"] for t in tools] == ["agent_toolset_20260401", "mcp_toolset"]
    assert not _builtins_runnable(tools)
    [linear] = [t for t in tools if t["type"] == "mcp_toolset"]
    assert linear["default_config"]["enabled"] is False
    assert [(c["name"], c["enabled"], c["permission_policy"]) for c in linear["configs"]] == [
        ("get_issue", True, {"type": "always_ask"})
    ]
    assert [s["name"] for s in constrained.send.mcp_servers] == ["linear"]
    assert constrained.original.tools == [t.model_dump(mode="json") for t in session.agent.tools]

    posture = capability_posture(
        PolicyApproval(decide=AsyncMock()), tool_allowlist=frozenset({"linear/get_issue"})
    )
    refused = await posture.decide(
        ToolCall(tool_use_id="x", server_name="linear", tool_name="create_issue")
    )
    assert not refused.allow


@pytest.mark.parametrize(
    "query",
    [
        "?extra=1",
        "?daimon_session=one&daimon_session=two",
        "?daimon_session=invalid",
        "?daimon_session=00000000-0000-0000-0000-000000000001&extra=1",
        "/",
        "/../mcp",
        "/%2e/",
    ],
)
async def test_allowlist_drops_management_endpoint_aliases(query: str) -> None:
    """Ported from the watches review: a renamed Daimon endpoint never survives."""
    session = ma_session(
        agent=ma_agent(
            tools=[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": name,
                    "configs": [],
                    "default_config": ALLOW,
                }
                for name in ("renamed", "daimon-mcp")
            ],
            mcp_servers=[
                {"name": "renamed", "type": "url", "url": "https://daimon.invalid/mcp" + query},
                {"name": "daimon-mcp", "type": "url", "url": "https://daimon.invalid/mcp"},
            ],
        )
    )
    constrained = await constrain_capabilities(
        session,
        ToolSafetyPolicy(),
        public_url="https://daimon.invalid/mcp",
        session_factory=None,
        tenant_id=None,
        tool_allowlist=frozenset({"renamed/get_agent", "daimon-mcp/get_agent"}),
    )
    assert constrained.send.tools == []
    assert constrained.send.mcp_servers == []


async def test_apply_invalidates_cached_tools_before_mutating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def invalidate(_db: Any, *, ma_session_id: str) -> None:
        order.append(f"invalidate:{ma_session_id}")

    async def update(*_args: Any, **_kwargs: Any) -> None:
        order.append("update")

    class _Begin:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc: object) -> None:
            order.append("commit")

    factory = MagicMock()
    factory.begin = _Begin
    monkeypatch.setattr(caps, "invalidate_session_tools", invalidate)
    monkeypatch.setattr(caps, "update_session_record", update)
    await gate_session_snapshot(
        MagicMock(),
        _root_session(),
        ToolSafetyPolicy(enabled=True),
        scope=_scope(),
        public_url=None,
        session_factory=None,
        tenant_id=None,
        entry_point="watch",
    )
    assert order == ["update"]
    order.clear()
    session = _root_session()
    constrained = await constrain_capabilities(
        session,
        ToolSafetyPolicy(enabled=True),
        public_url=None,
        session_factory=None,
        tenant_id=None,
    )
    await caps.apply_capabilities(
        MagicMock(), session, constrained, scope=_scope(), session_factory=factory
    )
    assert order == [f"invalidate:{session.id}", "commit", "update"]


async def test_restore_finishes_through_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ported from the watches turn: the original tools are back before release."""
    started = asyncio.Event()
    finish = asyncio.Event()
    restored = asyncio.Event()
    snapshot = _root_session()
    original = caps.SessionToolConfig.of(snapshot)

    async def update(*_args: Any, **kwargs: Any) -> None:
        started.set()
        await finish.wait()
        assert kwargs["agent"]["tools"] == original.tools
        restored.set()

    monkeypatch.setattr(caps, "update_session_record", update)
    task = asyncio.create_task(
        restore_capabilities(MagicMock(), snapshot, original, scope=_scope())
    )
    await started.wait()
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert restored.is_set()


def test_a_reviewed_session_with_every_known_capability_passes() -> None:
    """No false refusal: skills, resources of each kind and a custom tool are known."""
    session = _mutated(
        lambda s: (
            s["agent"]["skills"].append({"type": "custom", "skill_id": "skill_1", "version": "1"}),
            s["agent"]["tools"].append(
                {
                    "type": "custom",
                    "name": "local",
                    "description": "d",
                    "input_schema": {"type": "object", "anything": {"goes": True}},
                }
            ),
            s["resources"].extend(
                [
                    _FILE,
                    {
                        "id": "sesrsc_2",
                        "type": "github_repository",
                        "url": "https://github.com/o/r",
                        "mount_path": "/workspace/r",
                        "checkout": {"type": "branch", "name": "main"},
                        "created_at": "2026-10-01T00:00:00Z",
                        "updated_at": "2026-10-01T00:00:00Z",
                    },
                    {
                        "type": "memory_store",
                        "memory_store_id": "memstore_1",
                        "access": "read_write",
                        "description": None,
                        "instructions": None,
                        "mount_path": "/mnt/memory",
                        "name": "notes",
                    },
                ]
            ),
        )
    )
    require_allowed_capabilities(session, allow_skills=True)
    with pytest.raises(AdmissionDenied):
        require_allowed_capabilities(session, allow_skills=False)


def _daimon_and_foreign(public_url: str) -> BetaManagedAgentsSession:
    return _root_session(
        tools=[
            *_tools(),
            {
                "type": "mcp_toolset",
                "mcp_server_name": "daimon-mcp",
                "default_config": ALLOW,
                "configs": [{"name": "list_agents", **ALLOW}],
            },
        ],
        mcp_servers=[*deepcopy(SERVERS), {"name": "daimon-mcp", "type": "url", "url": public_url}],
    )


@pytest.mark.parametrize("enabled", [True, False], ids=["safety_on", "safety_off"])
@pytest.mark.parametrize("entry_point", [None, *ENTRY_POINTS])
async def test_every_retained_mcp_call_pauses_for_the_decider(
    entry_point: UnattendedEntryPoint | None, enabled: bool
) -> None:
    """Trusted Daimon tools and, with safety off, foreign writes ask too.

    Nothing an MCP server offers runs on its own in an unattended session; the
    decider then answers as before (Daimon's ordinary tools and, with safety
    off, foreign calls are allowed).
    """
    public_url = "https://daimon.example/mcp"
    policy = ToolSafetyPolicy(enabled=enabled)
    snapshot = _daimon_and_foreign(public_url)
    provider, result = await _drive(snapshot, entry_point, policy=policy, public_url=public_url)

    assert result.error is None
    [agent] = provider.at_send
    toolsets = [t for t in agent["tools"] if t["type"] == "mcp_toolset"]
    assert {t["mcp_server_name"] for t in toolsets} == {"linear", "daimon-mcp"}
    for toolset in toolsets:
        assert toolset["default_config"]["permission_policy"] == {"type": "always_ask"}
        assert all(c["permission_policy"] == {"type": "always_ask"} for c in toolset["configs"]), (
            toolset
        )
    posture = unattended_posture(
        policy,
        session=snapshot,
        public_url=public_url,
        session_factory=None,
        tenant_id=None,
        entry_point=entry_point,
    )
    daimon = await posture.decide(
        ToolCall(tool_use_id="d", server_name="daimon-mcp", tool_name="list_agents")
    )
    foreign = await posture.decide(
        ToolCall(tool_use_id="f", server_name="linear", tool_name="create_issue")
    )
    assert daimon.allow
    assert foreign.allow is not enabled


@pytest.mark.parametrize("effort", ["medium", {"type": "medium"}])
def test_model_inference_settings_daimon_writes_pass(effort: object) -> None:
    """`inference_geo` and `effort`, which Daimon's driver sets and the SDK does not parse."""
    session = _mutated(
        lambda s: s["agent"]["model"].update(inference_geo="us", effort=effort, speed="fast")
    )
    require_allowed_capabilities(session)


def test_a_builtin_config_may_echo_its_own_name_as_type() -> None:
    session = _mutated(lambda s: s["agent"]["tools"][0]["configs"][0].update(type="bash"))
    require_allowed_capabilities(session)


def test_a_hidden_field_in_a_session_needing_no_update_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-change fast path never reaches the native update schema."""
    clean = _mutated(
        lambda s: s["agent"].update(
            tools=[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "linear",
                    "default_config": _ASKING,
                    "configs": [{"name": "create_issue", **_ASKING}],
                }
            ]
        )
    )
    require_allowed_capabilities(clean)
    with pytest.raises(AdmissionDenied):
        require_allowed_capabilities(_mutated(_HIDDEN_SHAPES["no_change_enabled_object"]))
