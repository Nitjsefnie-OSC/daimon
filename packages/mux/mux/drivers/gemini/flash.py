"""Explicit host Flash policy; no client creation, retry or implicit selection."""

from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import JsonValue

from mux.contracts.ids import ModelRef
from mux.contracts.resources import Agent
from mux.drivers.gemini.mcp_auth import model_request
from mux.drivers.gemini.transport import (
    Object,
    TokenCountingTransport,
    Transport,
    object_value,
    string,
)
from mux.errors import ProviderError
from mux.profiles.gemini import ARGON_MODEL, WORKER_MODEL

FLASH_PRIMARY = "gemini-3.8-flash"
FLASH_MODELS = (FLASH_PRIMARY, "gemini-flash-latest", "gemini-3.5-flash-lite")
# Every model an attempt may name. Argon is only ever its own single attempt.
ATTEMPT_MODELS = (*FLASH_MODELS, ARGON_MODEL)


@dataclass(frozen=True)
class FlashAttempt:
    model: ModelRef
    attempt: int
    outcome: Literal["accepted", "refused", "unknown"]
    http_status: int | None = None
    interaction_id: str | None = None


class FlashAccounting(Protocol):
    """Caller persists a hold before each dispatch, and preserves its outcome.

    A failed accounting operation stops the chain. This policy cannot waive a
    budget or infer a free call from a refusal. Actuals arrive via scoped Usage.
    """

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None: ...
    async def after_attempt(self, *, receipt: FlashAttempt) -> None: ...


class FlashTransport:
    def __init__(
        self,
        source: Transport,
        accounting: FlashAccounting,
        *,
        native_agent: Agent | None = None,
        bounded_worker: bool = False,
        primary: str = FLASH_PRIMARY,
    ) -> None:
        if primary not in (FLASH_PRIMARY, ARGON_MODEL):
            raise ValueError("an explicit Flash or Argon primary is required")
        self._primary = primary
        self._bounded_worker = bounded_worker
        self._native_agent = native_agent
        self.source, self.accounting = source, accounting
        self._models: dict[str, ModelRef] = {}

    def model_for_interaction(self, interaction: str) -> ModelRef | None:
        return self._models.get(interaction)

    async def count_input_tokens(self, request: Mapping[str, JsonValue]) -> int:
        if not isinstance(self.source, TokenCountingTransport):
            raise ProviderError(
                "invalid_request", retryable=False, native_code="unknown_input_tokens"
            )
        return await self.source.count_input_tokens(request)

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        request = deepcopy(dict(request))
        capped = model_request(dict(request))
        original = {} if capped else object_value(request["agent_config"])
        required = (
            self._native_agent.spec.model.id if self._native_agent is not None else self._primary
        )
        if (
            required not in (FLASH_PRIMARY, WORKER_MODEL, ARGON_MODEL)
            or (request.get("model") if capped else original.get("model")) != required
        ):
            raise ProviderError(
                "invalid_request", retryable=False, native_code="flash_primary_required"
            )
        models = (FLASH_PRIMARY,) if self._bounded_worker else FLASH_MODELS
        if required == ARGON_MODEL:
            # A 503 on Argon is the turn's answer, never a silent switch to Flash.
            models = (ARGON_MODEL,)
        if self._bounded_worker and (not capped or required != FLASH_PRIMARY):
            raise ProviderError(
                "invalid_request", retryable=False, native_code="worker_model_required"
            )
        if self._native_agent is not None:
            if capped:
                raise ProviderError(
                    "invalid_request", retryable=False, native_code="named_agent_model_required"
                )
            if request.get(
                "agent"
            ) != self._native_agent.ref.id or self._native_agent.spec.model != ModelRef(
                provider="gemini", id=required
            ):
                raise ProviderError(
                    "invalid_request", retryable=False, native_code="named_agent_model_required"
                )
            # A retained native definition cannot be replaced by the inline fallback chain.
            models = (self._native_agent.spec.model.id,)
        for index, name in enumerate(models):
            model = ModelRef(provider="gemini", id=name)
            await self.accounting.before_attempt(model=model, attempt=index + 1)
            bounded = dict(request)
            if capped:
                bounded["model"] = name
            else:
                bounded["agent_config"] = {**original, "model": name}
            try:
                reply = await self.source.create(bounded)
            except ProviderError as error:
                refused = error.category == "overloaded" and error.native_code == "503"
                await self.accounting.after_attempt(
                    receipt=FlashAttempt(
                        model=model,
                        attempt=index + 1,
                        outcome="refused" if refused else "unknown",
                        http_status=503 if refused else None,
                    )
                )
                if not refused or index + 1 == len(models):
                    raise
                continue
            # No fallback on malformed/lost acceptance or persistence failure.
            interaction = string(reply["id"])
            string(reply["status"])
            self._models[interaction] = model
            await self.accounting.after_attempt(
                receipt=FlashAttempt(
                    model=model,
                    attempt=index + 1,
                    outcome="accepted",
                    interaction_id=interaction,
                )
            )
            return reply
        raise ProviderError("upstream", retryable=False, native_code="flash_chain_exhausted")

    async def get(self, interaction_id: str) -> Object:
        return await self.source.get(interaction_id)

    async def cancel(self, interaction_id: str) -> Object:
        return await self.source.cancel(interaction_id)

    async def download_snapshot(self, environment_id: str) -> bytes:
        return await self.source.download_snapshot(environment_id)

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        return await self.source.open_stream(interaction_id, after=after)
