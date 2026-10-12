"""Declared profiles and extension addressing."""

from __future__ import annotations

import inspect
import pickle

import pytest
from mux import profiles
from mux.contracts import ports
from mux.contracts.extensions import ExtensionConfig, ExtensionRef
from mux.contracts.profile import CORE_CAPABILITIES, Profile
from mux.errors import ExtensionVersionError, InvalidConfig, UnsupportedCapability
from mux.profiles import (
    MANAGED_AGENTS,
    PERSISTENT_WORKSPACE,
    PROFILES,
    emits_legacy_events_for,
    get_profile,
)
from pydantic import ValidationError


def test_declared_profiles_and_their_core_status() -> None:
    assert {pid: p.core for pid, p in PROFILES.items()} == {
        "anthropic.managed_agents": True,
        "openai.persistent_workspace": True,
        "openai.conversation_only": False,
        "gemini.inline_reuse": False,
    }


@pytest.mark.parametrize("profile", tuple(PROFILES.values()), ids=lambda p: p.profile_id)
def test_legacy_rendering_declarations_round_trip(profile: Profile) -> None:
    assert profile.emits_legacy_events == (profile.provider == "anthropic")
    assert emits_legacy_events_for(profile.provider) is profile.emits_legacy_events
    assert Profile.model_validate_json(profile.model_dump_json()) == profile
    assert pickle.loads(pickle.dumps(profile)) == profile


def test_missing_rendering_declaration_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profiles, "PROFILES", {})
    with pytest.raises(InvalidConfig, match="unambiguous"):
        emits_legacy_events_for("anthropic")


def test_conflicting_rendering_declarations_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conflicting = PERSISTENT_WORKSPACE.model_copy(update={"emits_legacy_events": True})
    monkeypatch.setattr(
        profiles, "PROFILES", {"existing": PERSISTENT_WORKSPACE, "conflicting": conflicting}
    )
    with pytest.raises(InvalidConfig, match="unambiguous"):
        emits_legacy_events_for("openai")


def test_old_profile_defaults_to_normalized_rendering() -> None:
    old = PERSISTENT_WORKSPACE.model_dump(exclude={"emits_legacy_events"})
    assert Profile.model_validate(old).emits_legacy_events is False


def test_core_flag_is_checked_against_support() -> None:
    support = dict.fromkeys(CORE_CAPABILITIES, "native")
    support["cancel"] = "unknown"
    with pytest.raises(ValidationError, match="lacks cancel"):
        Profile(
            provider="openai",
            profile_id="openai.x",
            schema_version="1",
            sdk_pin="openai",
            core=True,
            support=support,  # pyright: ignore[reportArgumentType]
        )


def test_undeclared_capability_is_unknown() -> None:
    assert PERSISTENT_WORKSPACE.support_for("tool_confirmation") == "unknown"


def test_unknown_profile_id() -> None:
    with pytest.raises(InvalidConfig):
        get_profile("anthropic.nope")
    assert get_profile("anthropic.managed_agents") is MANAGED_AGENTS


def test_extension_lookup_distinguishes_never_from_wrong_version() -> None:
    assert MANAGED_AGENTS.offered_extension("anthropic.memory_stores", 1) == ExtensionRef(
        namespace="anthropic.memory_stores", version=1
    )
    with pytest.raises(ExtensionVersionError) as caught:
        MANAGED_AGENTS.offered_extension("anthropic.memory_stores", 2)
    assert caught.value.offered == (1,)
    with pytest.raises(UnsupportedCapability):
        PERSISTENT_WORKSPACE.offered_extension("anthropic.memory_stores", 1)


def test_extensions_are_namespaced_by_provider() -> None:
    with pytest.raises(ValidationError):
        ExtensionConfig(namespace="memory_stores", version=1)
    with pytest.raises(ValidationError, match="not a openai namespace"):
        Profile.model_validate(
            PERSISTENT_WORKSPACE.model_dump()
            | {"extensions": [{"namespace": "anthropic.vaults", "version": 1}]}
        )


def test_no_port_exposes_a_raw_client() -> None:
    for _, port in inspect.getmembers(ports, inspect.isclass):
        if port.__module__ != ports.__name__:
            continue
        names = {n for n in dir(port) if not n.startswith("__")}
        assert not {n for n in names if "client" in n or n.startswith("raw")}, port.__name__
