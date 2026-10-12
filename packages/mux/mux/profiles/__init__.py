"""The declared provider profiles and a lookup by id.

Support levels are what the provider documents today. Anything without
evidence is left undeclared, which reads as `unknown` and is refused at
admission; the conformance suite is what moves a capability out of
`unknown`.
"""

from __future__ import annotations

from collections.abc import Mapping

from mux.contracts.config import BackendConfig, ResolvedBackend, resolve_default
from mux.contracts.errors import InvalidConfig
from mux.contracts.ids import Provider
from mux.contracts.profile import Profile
from mux.profiles.anthropic import MANAGED_AGENTS
from mux.profiles.gemini import INLINE_REUSE
from mux.profiles.openai import CONVERSATION_ONLY, PERSISTENT_WORKSPACE

PROFILES: Mapping[str, Profile] = {
    p.profile_id: p for p in (MANAGED_AGENTS, PERSISTENT_WORKSPACE, CONVERSATION_ONLY, INLINE_REUSE)
}


def get_profile(profile_id: str) -> Profile:
    try:
        return PROFILES[profile_id]
    except KeyError:
        raise InvalidConfig(f"unknown profile {profile_id!r}") from None


__all__ = [
    "CONVERSATION_ONLY",
    "INLINE_REUSE",
    "MANAGED_AGENTS",
    "PERSISTENT_WORKSPACE",
    "PROFILES",
    "get_profile",
]


_BACKEND_SELECTIONS: Mapping[Provider, BackendConfig] = {
    "anthropic": BackendConfig(),
    "openai": BackendConfig(
        backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna"
    ),
    "gemini": BackendConfig(
        backend="gemini", profile="gemini.inline_reuse", model="gemini-3.8-flash"
    ),
}


def backend_selection(backend: Provider) -> ResolvedBackend:
    """The supported admin selection; provider choices stay in the profile seam."""
    try:
        config = _BACKEND_SELECTIONS[backend]
    except KeyError:
        raise InvalidConfig("unknown backend selection") from None
    return resolve_default(config)


def shared_threads_available(backend: Provider) -> bool:
    """Thread-mode limits of the admitted profile, without host provider branches."""
    return {"anthropic": True, "openai": False, "gemini": False}[backend]
