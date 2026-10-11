"""Public replay and real catalog admission use the shared isolated SDKs."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx
import pytest
from daimon.testing.isolated_sdk import SDK_ENV_VARS, AmbientSDKConfigurationError
from daimon.testing.ma_transport import ScriptedTransport
from daimon.testing.provider_fixtures import load_provider_fixtures, turn_tape
from daimon.testing.provider_replay import provider_replay

from .catalog_runner import load_catalog, plan_scenario
from .headless_executor import execute_gemini_cell, execute_openai_cell

if TYPE_CHECKING:
    from google.genai import Client
    from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "packages/testing/fixtures/target53"
CATALOG = DATA / "catalog"
PACK = load_provider_fixtures(DATA / "index.json", catalog_root=CATALOG)
SCENARIO_ID = "QA-NEW37-MARKDOWN-TABLE-READABLE"


class NamesOnly(Mapping[str, str]):
    """Match real Mapping membership semantics; fail on present-value reads."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.reads: list[str] = []

    def __iter__(self) -> Iterator[str]:
        return iter((self.name,))

    def __len__(self) -> int:
        return 1

    def __getitem__(self, key: str) -> str:
        if key == self.name:
            self.reads.append(key)
            raise AssertionError("present ambient value was read")
        raise KeyError(key)


def no_http_client(*args: object, **kwargs: object) -> None:
    raise AssertionError("ambient option reached HTTP client allocation")


@pytest.mark.parametrize("backend", ("openai", "gemini"))
@pytest.mark.parametrize("name", SDK_ENV_VARS)
@pytest.mark.parametrize("value", ("", "invented-test-option"))
async def test_public_provider_replay_refuses_before_http_allocation(
    monkeypatch: pytest.MonkeyPatch,
    backend: Literal["openai", "gemini"],
    name: str,
    value: str,
) -> None:
    scenario, fixture = PACK.select(SCENARIO_ID, backend)
    tape = turn_tape(scenario, fixture, fixture.turns[0], key="offline")
    ambient = {name: value}
    monkeypatch.setattr(os, "environ", ambient)
    monkeypatch.setattr(httpx, "AsyncClient", no_http_client)
    monkeypatch.setattr(httpx, "Client", no_http_client)
    try:
        async with provider_replay(
            tape, backend=backend, profile=fixture.profile, model=fixture.model, source_root=CATALOG
        ):
            raise AssertionError("ambient replay yielded a client")
    except AmbientSDKConfigurationError as error:
        if name not in str(error) or "invented-test-option" in str(error):
            raise AssertionError("refusal diagnostic did not use names only") from error
    else:
        raise AssertionError("ambient replay was admitted")
    if ambient != {name: value}:
        raise AssertionError("factory mutated the environment")


@pytest.mark.parametrize("backend", ("openai", "gemini"))
@pytest.mark.parametrize("name", SDK_ENV_VARS)
async def test_real_catalog_path_refuses_without_reading_present_values(
    monkeypatch: pytest.MonkeyPatch,
    backend: Literal["openai", "gemini"],
    name: str,
) -> None:
    scenario, fixture = PACK.select(SCENARIO_ID, backend)
    entry = next(e for e in load_catalog(CATALOG) if e.id == SCENARIO_ID).model_copy(
        update={"sha256": scenario.source.sha256}
    )
    plan = plan_scenario(entry, CATALOG, backend, run_id="isolated")
    ambient = NamesOnly(name)
    with monkeypatch.context() as guarded:
        guarded.setattr(os, "environ", ambient)
        guarded.setattr(httpx, "AsyncClient", no_http_client)
        guarded.setattr(httpx, "Client", no_http_client)
        try:
            await (execute_openai_cell if backend == "openai" else execute_gemini_cell)(
                plan, scenario, fixture, source_root=CATALOG, repository_root=ROOT
            )
        except ValueError:
            if ambient.reads:
                raise AssertionError("catalog refusal fetched an ambient value") from None
        else:
            raise AssertionError("ambient catalog path was admitted")


@pytest.mark.parametrize("name", SDK_ENV_VARS)
async def test_scripted_sentinel_uses_the_shared_name_only_guard(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    ambient = NamesOnly(name)
    wire = ScriptedTransport()
    with monkeypatch.context() as guarded:
        guarded.setattr(os, "environ", ambient)
        guarded.setattr(httpx, "AsyncClient", no_http_client)
        try:
            wire.client()
        except AmbientSDKConfigurationError:
            if ambient.reads or wire.requests or wire.violations:
                raise AssertionError("sentinel refusal read values or performed I/O") from None
        else:
            raise AssertionError("ambient sentinel was admitted")


@pytest.mark.parametrize("backend", ("openai", "gemini"))
async def test_clean_factory_preserves_exact_native_tape_and_closes_clients(
    monkeypatch: pytest.MonkeyPatch, backend: Literal["openai", "gemini"]
) -> None:
    from daimon.testing import provider_replay as factory

    monkeypatch.setattr(os, "environ", {})
    owned: list[httpx.Client | httpx.AsyncClient] = []
    # Track explicit clients without replacing SDK-visible client types.
    if backend == "openai":
        original = factory.build_async_openai

        def build_openai(**kwargs: Any) -> AsyncOpenAI:
            client = original(**kwargs)
            owned.append(client._client)  # pyright: ignore[reportPrivateUsage]
            return client

        monkeypatch.setattr(factory, "build_async_openai", build_openai)
    else:
        original_gemini = factory.build_gemini_client

        def build_gemini(**kwargs: Any) -> Client:
            client = original_gemini(**kwargs)
            sync_http = client._api_client._httpx_client  # pyright: ignore[reportPrivateUsage]
            async_http = client._api_client._async_httpx_client  # pyright: ignore[reportPrivateUsage]
            if not isinstance(sync_http, httpx.Client) or not isinstance(
                async_http, httpx.AsyncClient
            ):
                raise AssertionError("factory did not own both HTTP clients")
            owned.extend((sync_http, async_http))
            return client

        monkeypatch.setattr(factory, "build_gemini_client", build_gemini)
    scenario, fixture = PACK.select(SCENARIO_ID, backend)
    entry = next(e for e in load_catalog(CATALOG) if e.id == SCENARIO_ID).model_copy(
        update={"sha256": scenario.source.sha256}
    )
    plan = plan_scenario(entry, CATALOG, backend, run_id="clean-isolation")
    result = await (execute_openai_cell if backend == "openai" else execute_gemini_cell)(
        plan, scenario, fixture, source_root=CATALOG, repository_root=ROOT
    )
    if result.request_count != (6 if backend == "openai" else 3):
        raise AssertionError("isolation changed exact request consumption")
    if result.reported_status != "EXPECTED-FAIL" or result.host_outcome.status != "PASS":
        raise AssertionError("isolation changed the honest table baseline")
    if not owned or any(not c.is_closed for c in owned):
        raise AssertionError("replay leaked an owned HTTP client")
