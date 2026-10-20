"""What a session may still do when nobody is watching the turn.

Every unattended entry point builds its session through this one module, so
the capability surface of a turn nobody watches is decided in one place:

- `require_allowed_capabilities` is a pure check on the session snapshot.
  A coordinator (`multiagent`), skills an entry point does not allow, and any
  field this release does not know are refused before anything is sent.
  Managed Agents can neither override `multiagent` at session creation nor
  change it mid-session, so a coordinator session is never repaired, only
  refused.
- `constrain_capabilities` works out the tools and servers to send: built-in
  sandbox and custom tools disabled unless the entry point's
  `CapabilityPolicy` allows them, an optional explicit tool allowlist, and
  every MCP toolset gated for the unattended decider.
- `capability_posture` makes the decider agree: a built-in call is refused
  when built-ins are off, and a call outside the allowlist is refused.

`ENTRY_POINT_POLICY` is the per-entry-point switch. Only watches turn the
sandbox off today; the others keep the behaviour they had, so flipping one
of them is a one-line change here.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, NoReturn, cast, get_args

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_agent_toolset20260401 import (
    BetaManagedAgentsAgentToolset20260401,
)
from anthropic.types.beta.beta_managed_agents_anthropic_skill import (
    BetaManagedAgentsAnthropicSkill,
)
from anthropic.types.beta.beta_managed_agents_custom_skill import BetaManagedAgentsCustomSkill
from anthropic.types.beta.beta_managed_agents_mcp_toolset import BetaManagedAgentsMCPToolset
from anthropic.types.beta.beta_managed_agents_session_agent_update_param import (
    BetaManagedAgentsSessionAgentUpdateParam,
)
from daimon.core.context_prompt import TurnContext
from daimon.core.mcp_server_url import same_mcp_endpoint
from daimon.core.session_ports_compat import update_session_record
from daimon.core.stores.thread_sessions import invalidate_session_tools
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    ToolCall,
    ToolSafetyPolicy,
    backend_switch_tools,
    heal_reserved_server,
    microsoft_send_tools,
    session_tools_for_policy,
    trusted_servers_for,
)
from daimon.core.turn.approvals import daimon_rule_tools_for
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.posture import PolicyApproval, ToolConfirmationResult
from mux.contracts.ids import Revision, Scope
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

UnattendedEntryPoint = Literal[
    "watch", "routine", "wake", "mcp_chat", "fleet_worker", "cli_run", "checkpoint"
]

BUILTIN_TOOLSET: Final[str] = "agent_toolset_20260401"
MCP_TOOLSET: Final[str] = "mcp_toolset"
CUSTOM_TOOL: Final[str] = "custom"


@dataclass(frozen=True)
class CapabilityPolicy:
    """What one unattended entry point may keep beyond gated MCP tools."""

    #: The sandbox toolset (bash, read, write, edit, …) and custom tools.
    allow_builtins: bool
    #: Skills, which cannot be removed from a session once it exists.
    allow_skills: bool


#: The switch. A routine that writes files needs the sandbox, and so does a
#: checkpoint archive; the rest keep today's behaviour until decided.
ENTRY_POINT_POLICY: Final[Mapping[UnattendedEntryPoint, CapabilityPolicy]] = {
    "watch": CapabilityPolicy(allow_builtins=False, allow_skills=False),
    "routine": CapabilityPolicy(allow_builtins=True, allow_skills=True),
    "wake": CapabilityPolicy(allow_builtins=True, allow_skills=True),
    "mcp_chat": CapabilityPolicy(allow_builtins=True, allow_skills=True),
    "fleet_worker": CapabilityPolicy(allow_builtins=True, allow_skills=True),
    "cli_run": CapabilityPolicy(allow_builtins=True, allow_skills=True),
    "checkpoint": CapabilityPolicy(allow_builtins=True, allow_skills=True),
}

_CLOSED: Final[CapabilityPolicy] = CapabilityPolicy(allow_builtins=False, allow_skills=False)


def capability_policy(entry_point: UnattendedEntryPoint | None) -> CapabilityPolicy:
    """The entry point's policy; a caller that names none gets nothing extra."""
    return _CLOSED if entry_point is None else ENTRY_POINT_POLICY[entry_point]


def unattended_entry_point(origin: TurnContext) -> UnattendedEntryPoint:
    """The entry point of a prepared turn nobody attends, by its origin.

    A fleet worker and a routine are their own; every other prepared
    unattended turn is a timer or a finished worker waking its thread.
    """
    if origin == "worker":
        return "fleet_worker"
    if origin == "routine":
        return "routine"
    return "wake"


def protected_tools(policy: ToolSafetyPolicy) -> frozenset[str]:
    """Daimon's tools that act only on a person's card, for an unattended turn.

    Every one a deployment could offer, whether or not this one does: a
    session that asks on a tool it lacks costs nothing. `microsoft_write`
    asks only with the policy on, as it does in chat.
    """
    return backend_switch_tools(enabled=True) | microsoft_send_tools(
        enabled=True, tool_safety_on=policy.enabled
    )


def _fields(*names: str) -> frozenset[str]:
    return frozenset(name for group in names for name in group.split())


# The reviewed schema of every capability-bearing node, frozen here rather
# than read from the installed SDK: a field the provider adds, or an SDK
# upgrade starts parsing, is refused until it is reviewed. Opaque data (a
# custom tool's `input_schema`, metadata values, usage) is not walked.
_SESSION = _fields(
    "id type agent archived_at created_at updated_at deployment_id environment_id",
    "metadata outcome_evaluations resources stats status title usage vault_ids",
)
_AGENT = _fields(
    "id type version name description model system tools mcp_servers skills multiagent"
)
# Inference settings only (Daimon's own driver writes `inference_geo` and
# `effort`, which the SDK does not parse yet); none of them grants a tool.
_MODEL = _fields("id speed inference_geo effort")
# The two skill kinds the SDK knows, by their own `type` literals.
_SKILL_TYPES = frozenset(
    kind
    for model in (BetaManagedAgentsAnthropicSkill, BetaManagedAgentsCustomSkill)
    for kind in get_args(model.model_fields["type"].annotation)
)
_SKILL = _fields("skill_id type version")
_SERVER = _fields("name type url")
_TOOLSETS: Final[Mapping[str, frozenset[str]]] = {
    BUILTIN_TOOLSET: _fields("type default_config configs"),
    MCP_TOOLSET: _fields("type mcp_server_name default_config configs"),
}
_CUSTOM = _fields("type name description input_schema")
_DEFAULT_CONFIG = _fields("enabled permission_policy")
_CONFIG = _fields("name enabled permission_policy")
_POLICY = _fields("type")
_POLICY_TYPES = frozenset({"always_allow", "always_ask"})
_RESOURCES: Final[Mapping[str, frozenset[str]]] = {
    "file": _fields("id type file_id mount_path created_at updated_at"),
    "github_repository": _fields("id type url mount_path checkout created_at updated_at"),
    "memory_store": _fields("type memory_store_id access description instructions mount_path name"),
}
_CHECKOUTS: Final[Mapping[str, frozenset[str]]] = {
    "branch": _fields("type name"),
    "commit": _fields("type sha"),
}


def _refuse() -> NoReturn:
    raise AdmissionDenied(reason="backend_unsupported")


def _only(value: object, names: frozenset[str]) -> None:
    """Refuse `value` unless it is a parsed model carrying only `names`.

    Counts the fields the SDK declares (set or not), the response's extras
    and anything else on the instance, so neither a new provider key nor a
    newly parsed SDK field passes unreviewed.
    """
    if not isinstance(value, BaseModel):
        _refuse()
    present = (
        set(type(value).model_fields)
        | set(value.model_extra or {})
        | {name for name in vars(value) if not name.startswith("_")}
    )
    if present - names:
        _refuse()


# Value shapes for the same nodes, checked on the snapshot's raw provider
# form: a reviewed scalar must be that scalar (or null), so no field can hide
# inside a value that became an object or a list. Objects and lists are
# checked all the way down; only telemetry, metadata and a custom tool's
# input schema are opaque data.
type _Shape = Callable[[object], bool]


def _opaque(_value: object) -> bool:
    return True


def _scalar(*kinds: type) -> _Shape:
    def check(value: object) -> bool:
        # `bool` is an `int`; only a field typed bool accepts one.
        return value is None or (
            isinstance(value, kinds) and (bool in kinds or not isinstance(value, bool))
        )

    return check


def _one_of(*values: str) -> _Shape:
    return lambda value: value is None or (isinstance(value, str) and value in values)


def _list(item: _Shape) -> _Shape:
    def check(value: object) -> bool:
        return isinstance(value, list) and all(
            item(element) for element in cast("list[object]", value)
        )

    return check


def _object(**fields: _Shape) -> _Shape:
    def check(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        entries = cast("dict[str, object]", value)
        return set(entries) <= set(fields) and all(
            fields[key](entry) for key, entry in entries.items()
        )

    return check


def _nullable(shape: _Shape) -> _Shape:
    return lambda value: value is None or shape(value)


def _tagged(**variants: _Shape) -> _Shape:
    """An object whose `type` picks its shape."""

    def check(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        entries = cast("dict[str, object]", value)
        kind = entries.get("type")
        return isinstance(kind, str) and kind in variants and variants[kind](entries)

    return check


_TEXT = _scalar(str)
_FLAG = _scalar(bool)
_LEVELS = ("low", "medium", "high", "xhigh", "max")
_POLICY_SHAPE = _tagged(always_allow=_object(type=_TEXT), always_ask=_object(type=_TEXT))
_DEFAULT_SHAPE = _object(enabled=_FLAG, permission_policy=_POLICY_SHAPE)
_CONFIG_SHAPE = _object(name=_TEXT, enabled=_FLAG, permission_policy=_POLICY_SHAPE)


def _builtin_config(value: object) -> bool:
    # A built-in config may echo its own name as `type` (the driver's schema).
    if not isinstance(value, dict):
        return False
    config = dict(cast("dict[str, object]", value))
    if "type" in config and config.pop("type") != config.get("name"):
        return False
    return _CONFIG_SHAPE(config)


def _effort(value: object) -> bool:
    return _one_of(*_LEVELS)(value) or _object(type=_one_of(*_LEVELS))(value)


_SESSION_SHAPE = _object(
    id=_TEXT,
    type=_TEXT,
    archived_at=_TEXT,
    created_at=_TEXT,
    updated_at=_TEXT,
    deployment_id=_TEXT,
    environment_id=_TEXT,
    status=_TEXT,
    title=_TEXT,
    metadata=_opaque,
    outcome_evaluations=_opaque,
    stats=_opaque,
    usage=_opaque,
    vault_ids=_list(_TEXT),
    resources=_list(
        _tagged(
            file=_object(
                id=_TEXT,
                type=_TEXT,
                file_id=_TEXT,
                mount_path=_TEXT,
                created_at=_TEXT,
                updated_at=_TEXT,
            ),
            github_repository=_object(
                id=_TEXT,
                type=_TEXT,
                url=_TEXT,
                mount_path=_TEXT,
                created_at=_TEXT,
                updated_at=_TEXT,
                checkout=_nullable(
                    _tagged(
                        branch=_object(type=_TEXT, name=_TEXT),
                        commit=_object(type=_TEXT, sha=_TEXT),
                    )
                ),
            ),
            memory_store=_object(
                type=_TEXT,
                memory_store_id=_TEXT,
                access=_TEXT,
                description=_TEXT,
                instructions=_TEXT,
                mount_path=_TEXT,
                name=_TEXT,
            ),
        )
    ),
    agent=_object(
        id=_TEXT,
        type=_TEXT,
        version=_scalar(int),
        name=_TEXT,
        description=_TEXT,
        system=_TEXT,
        multiagent=_scalar(),
        model=_object(
            id=_TEXT,
            speed=_one_of("standard", "fast"),
            inference_geo=_one_of("us", "global"),
            effort=_effort,
        ),
        skills=_list(_object(skill_id=_TEXT, type=_TEXT, version=_scalar(str, int))),
        mcp_servers=_list(_object(name=_TEXT, type=_TEXT, url=_TEXT)),
        tools=_list(
            _tagged(
                agent_toolset_20260401=_object(
                    type=_TEXT, default_config=_DEFAULT_SHAPE, configs=_list(_builtin_config)
                ),
                mcp_toolset=_object(
                    type=_TEXT,
                    mcp_server_name=_TEXT,
                    default_config=_DEFAULT_SHAPE,
                    configs=_list(_CONFIG_SHAPE),
                ),
                custom=_object(type=_TEXT, name=_TEXT, description=_TEXT, input_schema=_opaque),
            )
        ),
    ),
)


def _require_toolset_configs(
    tool: BetaManagedAgentsAgentToolset20260401 | BetaManagedAgentsMCPToolset,
) -> None:
    _only(tool.default_config, _DEFAULT_CONFIG)
    configs = list(tool.configs)
    if len({config.name for config in configs}) != len(configs):
        _refuse()
    for config in configs:
        if tool.type == BUILTIN_TOOLSET and "type" in (config.model_extra or {}):
            # A built-in config may echo its own name as `type`, as the
            # driver's native schema allows; nothing else.
            if (config.model_extra or {})["type"] != config.name:
                _refuse()
            _only(config, _CONFIG | {"type"})
        else:
            _only(config, _CONFIG)
    for policy in (tool.default_config.permission_policy, *(c.permission_policy for c in configs)):
        _only(policy, _POLICY)
        if policy.type not in _POLICY_TYPES:
            _refuse()


def require_allowed_capabilities(
    snapshot: BetaManagedAgentsSession, *, allow_skills: bool = False
) -> None:
    """Refuse a session whose capabilities cannot be constrained, before any send.

    Pure: reads only `snapshot`. Raises `AdmissionDenied` for a coordinator
    roster, for skills unless `allow_skills`, and for any field this release
    has not reviewed, at every capability-bearing level: the session and its
    resources, the agent, its model, skills and servers, each tool, toolset
    default and per-tool config, and their permission policies. An unknown
    field is refused whatever its value, empty or not, and a known field must
    hold its reviewed shape: a scalar stays a scalar of its type (or null),
    and objects and lists are checked all the way down.
    """
    _only(snapshot, _SESSION)
    if not _SESSION_SHAPE(snapshot.model_dump(mode="json", warnings=False)):
        _refuse()
    agent = snapshot.agent
    _only(agent, _AGENT)
    if agent.multiagent is not None or (agent.skills and not allow_skills):
        _refuse()
    _only(agent.model, _MODEL)
    for skill in agent.skills:
        _only(skill, _SKILL)
        if skill.type not in _SKILL_TYPES:
            _refuse()
    names = [server.name for server in agent.mcp_servers]
    if len(set(names)) != len(names):
        _refuse()
    for server in agent.mcp_servers:
        _only(server, _SERVER)
        if server.type != "url":
            _refuse()
    for tool in agent.tools:
        if tool.type == CUSTOM_TOOL:
            _only(tool, _CUSTOM)
            continue
        if tool.type not in _TOOLSETS:
            _refuse()
        _only(tool, _TOOLSETS[tool.type])
        _require_toolset_configs(tool)
    for resource in snapshot.resources:
        kind = getattr(resource, "type", None)
        if not isinstance(kind, str) or kind not in _RESOURCES:
            _refuse()
        _only(resource, _RESOURCES[kind])
        checkout = getattr(resource, "checkout", None)
        if checkout is not None:
            if checkout.type not in _CHECKOUTS:
                _refuse()
            _only(checkout, _CHECKOUTS[checkout.type])


@dataclass(frozen=True)
class SessionToolConfig:
    """A session's root `tools` and `mcp_servers`, as SDK param dicts."""

    tools: list[dict[str, Any]]
    mcp_servers: list[dict[str, Any]]

    @classmethod
    def of(cls, snapshot: BetaManagedAgentsSession) -> SessionToolConfig:
        return cls(
            tools=[tool.model_dump(mode="json") for tool in snapshot.agent.tools],
            mcp_servers=[server.model_dump(mode="json") for server in snapshot.agent.mcp_servers],
        )

    def as_update(self) -> BetaManagedAgentsSessionAgentUpdateParam:
        agent: dict[str, Any] = {"tools": self.tools, "mcp_servers": self.mcp_servers}
        return cast(BetaManagedAgentsSessionAgentUpdateParam, agent)


@dataclass(frozen=True)
class ConstrainedSession:
    """The arrays to send, and the session's own arrays to restore afterwards."""

    send: SessionToolConfig
    original: SessionToolConfig
    #: False when `send` equals what the session already carries.
    changed: bool


def _configs(tool: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(config) for config in cast("list[Mapping[str, Any]]", tool.get("configs") or [])]


def _disabled_builtins(tool: dict[str, Any]) -> dict[str, Any]:
    off = {"enabled": False}
    return {
        **tool,
        "default_config": {**(tool.get("default_config") or {}), **off},
        "configs": [{**config, **off} for config in _configs(tool)],
    }


def _asking(tool: dict[str, Any]) -> dict[str, Any]:
    """An MCP toolset whose every call pauses for the unattended decider.

    Trusted, policy-off and per-tool `always_allow` settings all become
    `always_ask`; `enabled` is kept. What the decider then allows (Daimon's
    own ordinary tools, any call with tool safety off) is unchanged, but each
    call is decided, with the agent's rules, channel and trusted hints.
    """
    if tool.get("type") != MCP_TOOLSET:
        return tool
    ask = {"type": "always_ask"}
    return {
        **tool,
        "default_config": {**(tool.get("default_config") or {}), "permission_policy": ask},
        "configs": [{**config, "permission_policy": ask} for config in _configs(tool)],
    }


def _allowlisted(
    tools: list[dict[str, Any]],
    servers: list[dict[str, Any]],
    allowlist: frozenset[str],
    *,
    public_url: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Only the MCP tools named `server/tool` in `allowlist`, each enabled alone.

    Daimon's own management server is never kept, whatever the list says:
    `daimon-mcp`, a server named `sandbox`, and any server whose URL is the
    deployment's endpoint under a query, duplicate-parameter or path alias
    (`same_mcp_endpoint`).
    """
    own = {DAIMON_SERVER_NAME, "sandbox"} | {
        s["name"]
        for s in servers
        if public_url and isinstance(s.get("url"), str) and same_mcp_endpoint(s["url"], public_url)
    }
    wanted: dict[str, list[str]] = {}
    for key in sorted(allowlist):
        server, _, name = key.partition("/")
        if server and name and server not in own:
            wanted.setdefault(server, []).append(name)
    kept: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != MCP_TOOLSET:
            if tool.get("type") == BUILTIN_TOOLSET:
                kept.append(tool)
            continue
        names = wanted.get(tool.get("mcp_server_name") or "")
        if not names:
            continue
        default_config = dict(tool.get("default_config") or {})
        configs = {c.get("name"): c for c in _configs(tool)}
        kept.append(
            {
                **tool,
                "default_config": {**default_config, "enabled": False},
                "configs": [
                    {**default_config, **configs.get(name, {}), "name": name, "enabled": True}
                    for name in names
                ],
            }
        )
    named = {t.get("mcp_server_name") for t in kept if t.get("type") == MCP_TOOLSET}
    return kept, [s for s in servers if s["name"] in named]


async def constrain_capabilities(
    snapshot: BetaManagedAgentsSession,
    policy: ToolSafetyPolicy,
    *,
    public_url: str | None,
    session_factory: async_sessionmaker[AsyncSession] | None,
    tenant_id: uuid.UUID | None,
    allow_builtins: bool = False,
    allow_skills: bool = False,
    tool_allowlist: frozenset[str] | None = None,
) -> ConstrainedSession:
    """The tools and servers `snapshot` may run with unattended. No provider call.

    Refuses first (`require_allowed_capabilities`). Built-in and custom tools
    are disabled unless `allow_builtins`; with a `tool_allowlist` of
    `server/tool` keys only those MCP tools stay, and every server no kept
    toolset names is dropped. Every MCP toolset left, Daimon's own included and
    with tool safety on or off, asks on its default and on every per-tool
    config, so each call reaches the unattended decider; Daimon's
    `protected_tools` and rule tools are listed as `session_tools_for_policy`
    lists them, and a foreign `daimon-mcp` URL is healed.
    """
    require_allowed_capabilities(snapshot, allow_skills=allow_skills)
    original = SessionToolConfig.of(snapshot)
    tools = [dict(tool) for tool in original.tools]
    servers = [dict(server) for server in original.mcp_servers]
    if not allow_builtins:
        tools = [
            _disabled_builtins(tool) if tool.get("type") == BUILTIN_TOOLSET else tool
            for tool in tools
            if tool.get("type") != CUSTOM_TOOL
        ]
    if tool_allowlist is not None:
        tools, servers = _allowlisted(tools, servers, tool_allowlist, public_url=public_url)
    rules = (
        await daimon_rule_tools_for(
            session_factory, policy, tenant_id=tenant_id, agent_id=snapshot.agent.id
        )
        if session_factory is not None
        else frozenset[str]()
    )
    protected = protected_tools(policy)
    gated = session_tools_for_policy(
        policy,
        tools,
        trusted_servers=trusted_servers_for(public_url),
        confirmed_tools=protected,
        daimon_rule_tools=rules,
    )
    healed = heal_reserved_server(policy, servers, public_url=public_url, confirmed_tools=protected)
    send = SessionToolConfig(
        tools=[_asking(tool) for tool in (gated if gated is not None else tools)],
        mcp_servers=healed if healed is not None else servers,
    )
    return ConstrainedSession(send=send, original=original, changed=send != original)


async def apply_capabilities(
    client: AsyncAnthropic,
    snapshot: BetaManagedAgentsSession,
    constrained: ConstrainedSession,
    *,
    scope: Scope,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> None:
    """Send `constrained` to the session, when it changes anything. A failure raises.

    With a `session_factory`, the thread session's cached tool hashes are
    cleared and committed first (`invalidate_session_tools`, keeping its
    identity, workspace and credential metadata), so the next ordinary bind
    reapplies its own tools even if this turn never gets to restore them.
    """
    if not constrained.changed:
        return
    if session_factory is not None:
        async with session_factory.begin() as db:
            await invalidate_session_tools(db, ma_session_id=snapshot.id)
    await _update(client, snapshot, constrained.send, scope=scope)


async def restore_capabilities(
    client: AsyncAnthropic,
    snapshot: BetaManagedAgentsSession,
    original: SessionToolConfig,
    *,
    scope: Scope,
) -> None:
    """Put `original` back, finishing even when the turn is cancelled meanwhile.

    Shutdown and an owner stop can both cancel the same turn; the restore is
    shielded again on every request, and the cancellation propagates only
    after it.
    """
    restore = asyncio.create_task(_update(client, snapshot, original, scope=scope))
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(restore)
            break
        except asyncio.CancelledError as exc:
            if restore.cancelled():
                raise
            cancellation = exc
    if cancellation is not None:
        raise cancellation


async def _update(
    client: AsyncAnthropic,
    snapshot: BetaManagedAgentsSession,
    config: SessionToolConfig,
    *,
    scope: Scope,
) -> None:
    await update_session_record(
        client,
        snapshot.id,
        scope=scope,
        agent_id=snapshot.agent.id,
        revision=Revision(local=snapshot.agent.version, native=str(snapshot.agent.version)),
        agent=config.as_update(),
    )


def capability_posture(
    posture: PolicyApproval,
    *,
    allow_builtins: bool = False,
    tool_allowlist: frozenset[str] | None = None,
) -> PolicyApproval:
    """`posture`, refusing what `constrain_capabilities` took out of the session.

    A disabled tool should never pause, so this is the second lock: a
    built-in or custom call (`server_name=None`) is refused unless
    `allow_builtins`, and with a `tool_allowlist` so is every unlisted call.
    """
    if allow_builtins and tool_allowlist is None:
        return posture
    decide = posture.decide

    async def constrained(call: ToolCall) -> ToolConfirmationResult:
        if call.server_name is None and not allow_builtins:
            return ToolConfirmationResult(
                allow=False,
                deny_message=(
                    f"Refused {call.tool_name}: built-in sandbox tools are off for this "
                    "unattended turn. The tool call did not run."
                ),
            )
        if (
            call.server_name is not None
            and tool_allowlist is not None
            and call.key not in tool_allowlist
        ):
            return ToolConfirmationResult(
                allow=False,
                deny_message=(
                    f"Refused {call.key}: this unattended turn may only use its listed "
                    "tools. The tool call did not run."
                ),
            )
        return await decide(call)

    return PolicyApproval(decide=constrained)
