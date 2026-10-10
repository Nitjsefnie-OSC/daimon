"""Provider replay adopts the shared SDK preflight before any request."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from daimon.testing.isolated_sdk import SDK_ENV_VARS, AmbientSDKConfigurationError
from daimon.testing.provider_fixtures import load_provider_fixtures, turn_tape
from daimon.testing.provider_replay import Backend, ProviderTape, WireReplay, provider_replay

DATA = Path(__file__).resolve().parents[3] / "packages/testing/fixtures/target53"
PACK = load_provider_fixtures(DATA / "index.json", catalog_root=DATA / "catalog")


def tape_for(backend: Backend) -> ProviderTape:
    for scenario in PACK.scenarios:
        for fixture in scenario.providers:
            if fixture.backend == backend and fixture.turns:
                return turn_tape(scenario, fixture, fixture.turns[0], key="qa-send")
    raise RuntimeError("authored native provider fixture is required")


@pytest.mark.parametrize("backend", ["openai", "gemini"])
@pytest.mark.parametrize("name", SDK_ENV_VARS)
async def test_replay_refuses_ambient_settings_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, backend: Backend, name: str
) -> None:
    # Replace the mapping without reading any operator environment values.
    monkeypatch.setattr(os, "environ", {name: "offline-ambient-sentinel"})

    def refuse_dispatch(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ambient configuration reached replay I/O")

    monkeypatch.setattr(WireReplay, "dispatch", refuse_dispatch)
    tape = tape_for(backend)
    with pytest.raises(AmbientSDKConfigurationError):
        async with provider_replay(
            tape,
            backend=backend,
            profile=tape.profile,
            model=tape.model,
            source_root=DATA / "catalog",
        ):
            raise RuntimeError("ambient configuration reached an allocated replay")


@pytest.mark.parametrize("name", ["google_future_setting", "Gemini_Future_Setting"])
async def test_gemini_replay_refuses_future_provider_settings(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setattr(os, "environ", {name: "offline-ambient-sentinel"})
    tape = tape_for("gemini")
    with pytest.raises(AmbientSDKConfigurationError):
        async with provider_replay(
            tape,
            backend="gemini",
            profile=tape.profile,
            model=tape.model,
            source_root=DATA / "catalog",
        ):
            raise RuntimeError("future ambient configuration reached a replay")
