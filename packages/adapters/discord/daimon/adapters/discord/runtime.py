"""DiscordRuntime -- DI bundle for the Discord adapter process."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from secrets import randbits
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

import structlog
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from daimon.core.billing import BillingConfig, load_billing_config
from daimon.core.channel_budget_notice import drain_budget_notices
from daimon.core.config import Settings
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.db import build_engine, build_session_factory
from daimon.core.defaults.loader import parse_deployment_default
from daimon.core.ma_resolver import ResolverCache, new_resolver_cache
from daimon.core.mcp_oauth import McpTokenProbe, probe_bearer_token
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.core.skills.rate_limit import SkillsRateLimitedTransport
from daimon.core.stores.discord_gateway_lease import GatewayLease
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.worker_ownership import (
    hold_worker_owner as hold_process_owner,
)
from daimon.core.turn.admission import AdmissionGrant
from daimon.core.turn.deps import TurnDeps, build_turn_deps
from daimon.core.turn.outcomes import drain_outcomes
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.adapters.discord.lifecycle import EditFn, SendFn
    from daimon.adapters.discord.posted_controls.fleet import DiscordFleetCard, ParentResolver
    from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
    from daimon.core.neutral_resources import WorkerPermissions
    from daimon.core.pricing import ModelRates, ProviderPrice
    from daimon.core.stores.agent_posts import AgentPostRow

    import discord


class FleetCardFactory(Protocol):
    def __call__(
        self,
        thread: discord.abc.Messageable,
        requester_id: int,
        send: SendFn,
        edit: EditFn,
        *,
        run_id: UUID | None = None,
        turn_card_intent_id: UUID | None = None,
        recorded_post: AgentPostRow | None = None,
        on_replacement: Callable[[discord.Message], Awaitable[None]] | None = None,
    ) -> DiscordFleetCard | None: ...


log = structlog.get_logger()


# Keep the Discord gateway lease on the same guarded owner connection.
hold_worker_owner = hold_process_owner


@dataclass(frozen=True)
class DiscordRuntime:
    settings: Settings
    anthropic: AsyncAnthropic
    sessionmaker: async_sessionmaker[AsyncSession]
    notebook_rate_limiter: RateLimiter
    billing_config: BillingConfig | None
    deployment_default: DeploymentDefault
    resolver_cache: ResolverCache
    turn_deps: TurnDeps
    # Each production process holds this advisory key on a dedicated connection.
    owner_key: int = field(default_factory=lambda: randbits(63))
    ownership_lost: asyncio.Event = field(default_factory=asyncio.Event)
    # The MCP token form's live check. None means "do not probe" — the seam
    # tests use so a form submit never leaves the process; production wires
    # `daimon.core.mcp_oauth.probe_bearer_token`.
    mcp_token_probe: McpTokenProbe | None = None
    # Decides which Discord process may hold the gateway during a deploy overlap.
    # None means "no lease to take" (tests, tools that never connect).
    gateway_lease: GatewayLease | None = None
    # Host-built current-root service binding; ordinary construction discovers nothing.
    fleet_card_factory: FleetCardFactory | None = None


@asynccontextmanager
async def build_runtime(
    settings: Settings,
    *,
    fleet_service: FleetOrchestrator | None = None,
    fleet_parent_loader: ParentResolver | None = None,
    fleet_prices: Mapping[tuple[str, str], ModelRates | ProviderPrice] | None = None,
    fleet_root_grants: Callable[[AdmissionGrant, BackendAgent], Awaitable[WorkerPermissions]]
    | None = None,
) -> AsyncIterator[DiscordRuntime]:
    fleet_factory: FleetCardFactory | None = None
    owned_fleet_ports: HostFleetPorts | None = None
    engine = build_engine(
        str(settings.database.url),
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout,
    )
    sessionmaker = build_session_factory(
        engine,
        crypto_keys=tuple(k.get_secret_value() for k in settings.crypto.keys),
        allow_plaintext=settings.crypto.allow_plaintext,
    )
    deployment_default = parse_deployment_default(settings.defaults_root)
    resolver_cache = new_resolver_cache()
    billing_config = load_billing_config()
    async with AsyncAnthropic(
        api_key=settings.anthropic.resolved_api_key(),
        base_url=str(settings.anthropic.base_url),
        max_retries=MA_MAX_RETRIES,
        http_client=DefaultAsyncHttpxClient(
            transport=SkillsRateLimitedTransport(settings.anthropic.skills_requests_per_minute)
        ),
    ) as anthropic:
        notebook_rate_limiter = RateLimiter(
            max_requests=settings.notebook.publish_rate_per_hour,
        )
        turn_deps = build_turn_deps(
            settings,
            anthropic,
            sessionmaker,
            deployment_default=deployment_default,
            resolver_cache=resolver_cache,
            billing_config=billing_config,
            fleet_root_grants=fleet_root_grants if settings.fleet.enabled else None,
        )
        if settings.fleet.enabled:
            from daimon.adapters.discord.posted_controls.fleet import DiscordFleetCardFactory
            from daimon.core.fleet_orchestration import FleetOrchestrator, HostFleetPorts
            from daimon.core.stores.fleet import WorkerPool
            from daimon.core.stores.mux_state import PostgresStateStore

            if turn_deps.state_store is None:
                turn_deps = replace(turn_deps, state_store=PostgresStateStore(sessionmaker))
            if fleet_service is None:
                owned_fleet_ports = HostFleetPorts(
                    sessionmaker,
                    prices=fleet_prices,
                    markup=turn_deps.markup,
                    deps=turn_deps,
                    pool=WorkerPool(
                        max_concurrent_workers=settings.fleet.max_concurrent_workers,
                        max_concurrent_workers_per_tenant=(
                            settings.fleet.max_concurrent_workers_per_tenant
                        ),
                    ),
                )
                fleet_service = FleetOrchestrator(owned_fleet_ports)
            fleet_factory = (
                DiscordFleetCardFactory.from_runtime(fleet_service, turn_deps)
                if fleet_parent_loader is None
                else DiscordFleetCardFactory(fleet_service, fleet_parent_loader)
            )
        owner_key = randbits(63)
        ownership_lost = asyncio.Event()
        # Keep ownership outside the turn pool's preparation/mutation budget.
        # This pool opens one connection; its unused overflow satisfies the
        # shared builder's minimum capacity contract.
        owner_engine = build_engine(str(settings.database.url), pool_size=1, max_overflow=3)
        try:
            async with hold_worker_owner(
                owner_engine, owner_key=owner_key, ownership_lost=ownership_lost
            ) as gateway_lease:
                yield DiscordRuntime(
                    owner_key=owner_key,
                    ownership_lost=ownership_lost,
                    settings=settings,
                    anthropic=anthropic,
                    sessionmaker=sessionmaker,
                    notebook_rate_limiter=notebook_rate_limiter,
                    billing_config=billing_config,
                    deployment_default=deployment_default,
                    resolver_cache=resolver_cache,
                    turn_deps=turn_deps,
                    mcp_token_probe=probe_bearer_token,
                    gateway_lease=gateway_lease,
                    fleet_card_factory=fleet_factory,
                )
        finally:
            try:
                if owned_fleet_ports is not None:
                    turn_deps.running_turns.begin_shutdown()
                    await owned_fleet_ports.close()
            finally:
                await drain_outcomes()
                await drain_budget_notices()
                await owner_engine.dispose()
                await engine.dispose()
