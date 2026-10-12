"""Explicit Gemini deployment selection; no startup credential discovery."""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from daimon.core.mux_backend import TurnRuntime, register_turn_backend
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini import (
    PROFILE,
    GeminiDeployment,
    GeminiJournal,
    GeminiTransportRuntime,
    GeminiUsageRuntime,
    compose_gemini_backend,
    compose_gemini_codec,
)
from daimon.core.turn.gemini_prepare import prepare_gemini_turn
from daimon.core.turn.gemini_state import SQLiteFlashAccounting, SQLiteGeminiStorage
from daimon.core.turn.io import register_turn_codec
from daimon.core.turn.prepare import ProviderPreparationRequest, register_turn_preparation
from daimon.core.turn.runtime_manifest import (
    ApiKeyEnvironment,
    ConfigurationGrant,
    fixed_plan_origin_allowed,
    read_counterpart,
    select_runtime_manifest,
    validate_counterpart_pins,
)
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ToolSpec
from mux.drivers.gemini.catalog import GeminiCatalogAdapter
from mux.drivers.gemini.core import compile_agent, compile_environment
from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.errors import ScopeViolation
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

Nonblank = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]


class GeminiPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: Literal["gemini.inline_reuse"]
    tenant_id: Nonblank
    platform: Nonblank
    channel_id: Nonblank
    account_id: Nonblank
    config_digest: Nonblank | None = None
    configuration_grant: ConfigurationGrant | None = None
    project: Nonblank
    api_key_env: ApiKeyEnvironment
    records_path: Path
    holds_path: Path
    hold_usd: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    cap_usd: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    agent_name: Nonblank
    system: str | None = None
    # REQUIRED: [] means no tools. Never use the provider's implicit paid tools.
    tools: tuple[ToolSpec, ...]
    environment: EnvironmentSpec
    agent_id: Nonblank | None = None
    skill_ids: tuple[Nonblank, ...] = ()
    source_agent_id: UUID | None = None
    source_agent_version: Annotated[int, Field(gt=0, strict=True)] | None = None
    counterpart_catalog_id: Nonblank | None = None
    counterpart_catalog_revision: Annotated[int, Field(gt=0, strict=True)] | None = None

    @model_validator(mode="after")
    def _configuration_authorization(self) -> GeminiPlan:
        if self.config_digest is None and self.configuration_grant is None:
            raise ValueError("a deployment requires exact or bounded configuration authorization")
        validate_counterpart_pins(self)
        if (
            not self.records_path.is_absolute()
            or not self.holds_path.is_absolute()
            or self.records_path.resolve() == self.holds_path.resolve()
            or self.hold_usd > self.cap_usd
        ):
            raise ValueError(
                "a deployment requires distinct absolute accounting paths and a bounded hold"
            )
        return self

    async def read_counterpart(
        self,
        request: ProviderPreparationRequest,
        *,
        source: BackendAgent,
        resources: NeutralResourceHost,
    ) -> AgentRevision:
        """Read native named-agent intent; no full source or execution proof is implied."""

        async def verify_native(checked: AgentRevision) -> None:
            adapter = resources.backend.agents
            if (
                not isinstance(adapter, GeminiCatalogAdapter)
                or not adapter.enabled
                or adapter.account_scope_id != self.project
                or compile_environment(self.environment) != {"type": "remote", "sources": []}
                or checked.agent.spec.system != self.system
                or checked.agent.spec.tools != self.tools
            ):
                raise AdmissionDenied(reason="backend_unsupported")

        return await read_counterpart(
            self, request, source=source, resources=resources, verify_native=verify_native
        )


def load_gemini_deployment(request: ProviderPreparationRequest) -> GeminiPlan:
    revision = request.admission.backend_revision
    if not fixed_plan_origin_allowed(getattr(request.admission, "backend_revision_origin", None)):
        raise AdmissionDenied(reason="backend_unsupported")
    if revision is None or revision.profile != PROFILE:
        raise AdmissionDenied(reason="backend_unsupported")
    return select_runtime_manifest(request, GeminiPlan)


def build_gemini_runtime(request: ProviderPreparationRequest) -> TurnRuntime:
    revision, scope = request.admission.backend_revision, request.scope
    if revision is None or revision.model != FLASH_PRIMARY or request.deps.state_store is None:
        raise AdmissionDenied(reason="backend_unsupported")
    plan = load_gemini_deployment(request)
    agent = AgentSpec(
        name=plan.agent_name,
        model=ModelRef(provider="gemini", id=FLASH_PRIMARY),
        system=plan.system,
        tools=plan.tools,
    )
    # Validate native policy before reading the selected credential or opening
    # persistence. Unsupported MCP, confirmations and workspace sources refuse.
    compile_agent(agent)
    compile_environment(plan.environment)
    namespace = json.dumps([scope.tenant_id, scope.account_id, plan.project], separators=(",", ":"))

    def transport(config: ConfigRevision, authorized: Scope) -> GeminiTransportRuntime:
        if config != revision or authorized != scope:
            raise ScopeViolation(request.thread_id, "Gemini deployment differs from admitted turn")
        secret = os.environ.get(plan.api_key_env)
        if secret is None or not secret.strip():
            raise AdmissionDenied(reason="backend_unsupported")
        from mux.drivers.gemini.deployment import configured_transport

        return GeminiTransportRuntime(configured_transport(api_key=secret))

    return TurnRuntime(
        transport_factory=transport,
        journal=GeminiJournal(
            SQLiteGeminiStorage(plan.records_path, namespace=namespace),
            request.deps.state_store,
            GeminiDeployment(
                revision.digest, revision.local, plan.project, agent, plan.environment
            ),
        ),
        usage_store=GeminiUsageRuntime(
            SQLiteFlashAccounting(
                plan.holds_path, namespace=namespace, hold_usd=plan.hold_usd, cap_usd=plan.cap_usd
            ),
            {},
        ),
    )


def register_runtime() -> None:
    """Install the provider's host callbacks without transports or credential access."""
    from daimon.core.turn.runtimes import register_channel_runtime

    register_turn_backend(PROFILE, compose_gemini_backend, fallback=True)
    register_turn_codec(PROFILE, compose_gemini_codec, fallback=True)
    register_turn_preparation(PROFILE, prepare_gemini_turn, fallback=True)
    register_channel_runtime(PROFILE, build_gemini_runtime, fallback=True)
