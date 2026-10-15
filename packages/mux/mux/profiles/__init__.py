"""The declared provider profiles and a lookup by id.

Support levels are what the provider documents today. Anything without
evidence is left undeclared, which reads as `unknown` and is refused at
admission; the conformance suite is what moves a capability out of
`unknown`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from mux.contracts.config import BackendConfig, ResolvedBackend, ThreadMode, resolve_default
from mux.contracts.errors import InvalidConfig
from mux.contracts.ids import Provider
from mux.contracts.profile import SUPPORTED, AccountingMode, Profile
from mux.contracts.resources import AgentSpec, ToolSpec
from mux.profiles.anthropic import MANAGED_AGENTS
from mux.profiles.gemini import INLINE_REUSE, WORKER_MODEL
from mux.profiles.openai import CONVERSATION_ONLY, PERSISTENT_WORKSPACE

PROFILES: Mapping[str, Profile] = {
    p.profile_id: p for p in (MANAGED_AGENTS, PERSISTENT_WORKSPACE, CONVERSATION_ONLY, INLINE_REUSE)
}


def get_profile(profile_id: str) -> Profile:
    try:
        return PROFILES[profile_id]
    except KeyError:
        raise InvalidConfig(f"unknown profile {profile_id!r}") from None


def emits_legacy_events_for(provider: Provider) -> bool:
    """Read rendering policy for an event or resource without a profile id.

    All registered profiles for its provider must agree, so a missing or
    ambiguous declaration cannot silently select another rendering path.
    """
    declarations = {
        profile.emits_legacy_events for profile in PROFILES.values() if profile.provider == provider
    }
    if len(declarations) != 1:
        raise InvalidConfig("events require an unambiguous declared profile rendering policy")
    return declarations.pop()


def neutral_catalog_profile_for(provider: str, *, profile_id: str | None = None) -> Profile | None:
    """Select catalog support, requiring opt-in for explicitly named extra profiles.

    Provider-only callers keep their existing unambiguous resource profile;
    a host with a selected profile must retain that exact profile instead.
    """
    matches = tuple(
        profile
        for profile in PROFILES.values()
        if profile.provider == provider
        and (
            profile.profile_id == profile_id
            if profile_id is not None
            else profile.profile_id
            not in {
                explicit
                for selection in _BACKEND_SELECTIONS.values()
                for explicit in selection.profiles
            }
        )
        and profile.support_for("neutral_catalog") in SUPPORTED
    )
    if len(matches) > 1:
        raise InvalidConfig("provider has ambiguous neutral catalog profiles")
    return matches[0] if matches else None


def native_agent_tools(profile: Profile, spec: AgentSpec) -> tuple[ToolSpec, ...]:
    """Compare native readback while retaining the authored tool intent."""
    if profile.tool_readback == "authored":
        return tuple(spec.tools or ())
    return tuple(
        ToolSpec(
            name=tool.name,
            kind=tool.kind,
            description=(tool.description or None) if tool.kind == "custom" else None,
            input_schema=dict(tool.input_schema or {}) if tool.kind == "custom" else None,
            permission=tool.permission,
        )
        for tool in spec.tools or ()
        if tool.kind != "mcp_toolset"
        and not (tool.kind == "builtin" and tool.name == profile.execution_tool)
    )


def accounting_mode_for(provider: Provider) -> AccountingMode:
    """Read declared accounting policy for a DTO without a profile reference.

    All profiles of that provider must agree. An ambiguous declaration cannot
    choose an arbitrary default and silently change the observation's billing.
    No runtime/provider table, database read or SDK discovery is involved.
    """
    modes = {
        profile.accounting_mode for profile in PROFILES.values() if profile.provider == provider
    }
    if len(modes) != 1:
        raise InvalidConfig("usage requires an unambiguous declared profile accounting mode")
    return modes.pop()


__all__ = [
    "CONVERSATION_ONLY",
    "INLINE_REUSE",
    "MANAGED_AGENTS",
    "PERSISTENT_WORKSPACE",
    "PROFILES",
    "get_profile",
    "emits_legacy_events_for",
    "native_agent_tools",
    "neutral_catalog_profile_for",
    "accounting_mode_for",
]


@dataclass(frozen=True)
class BackendSelection:
    config: BackendConfig
    thread_modes: tuple[ThreadMode, ...]
    allowed_models: tuple[str, ...] | None = None
    profiles: tuple[str, ...] = ()


_BACKEND_SELECTIONS: Mapping[Provider, BackendSelection] = {
    "anthropic": BackendSelection(BackendConfig(), ("per_caller", "shared")),
    "openai": BackendSelection(
        BackendConfig(backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna"),
        ("per_caller",),
        profiles=(CONVERSATION_ONLY.profile_id,),
    ),
    "gemini": BackendSelection(
        BackendConfig(backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"),
        ("per_caller",),
        allowed_models=(WORKER_MODEL,),
    ),
}
RUNNABLE_PROFILES: frozenset[str] = frozenset(
    profile
    for selection in _BACKEND_SELECTIONS.values()
    for profile in (resolve_default(selection.config).profile, *selection.profiles)
)


def backend_selection(backend: Provider) -> ResolvedBackend:
    """The supported admin selection; provider choices stay in the profile seam."""
    try:
        selection = _BACKEND_SELECTIONS[backend]
    except KeyError:
        raise InvalidConfig("unknown backend selection") from None
    return resolve_default(selection.config)


def shared_threads_available(backend: Provider) -> bool:
    """Read the same admitted thread modes used by selection and runnable checks."""
    return "shared" in _BACKEND_SELECTIONS[backend].thread_modes


def runnable_selection(selection: ResolvedBackend) -> bool:
    """One release constraint table for profile, model and thread mode."""
    declared = _BACKEND_SELECTIONS[selection.backend]
    resolved = resolve_default(declared.config)
    return (
        selection.profile in (resolved.profile, *declared.profiles)
        and (
            selection.model == resolved.model or selection.model in (declared.allowed_models or ())
        )
        and selection.thread_mode in declared.thread_modes
    )
