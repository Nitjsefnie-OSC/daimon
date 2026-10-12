"""Channel budgets: which debits a budget counts, and the admission gate that reads them.

A budget caps what one channel may spend. Spend is the channel's ledger
debits (markup included) inside the budget's window; the gate refuses a new
turn once spend has reached the limit, so a limit of 0 stops the channel.
It runs after the balance and per-person cap gates, and only where a turn has
a channel. No budget row means no gate. Like the other gates it is checked
once before a turn, so a running turn can finish past the limit.

The period and validation helpers are pure; `get_channel_budget_status` and
`is_over_channel_budget` are the thin DB shell around them.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from typing import get_args

import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Decision, Place, Subject, Surface, authorize
from daimon.core.errors import DaimonError
from daimon.core.fleet import FleetConflict, FleetRun, FleetWorker, worker_slot
from daimon.core.neutral_resources import (
    ResolvedWorkerSpec,
    WorkerPermissions,
    audit_worker_action,
)
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.stores import accounts, mux_state, tenant_ledger
from daimon.core.stores import fleet as fleet_store
from daimon.core.stores.channel_budgets import get_channel_budget, list_channel_budgets
from daimon.core.stores.domain import BudgetWindow, ChannelBudgetRow
from daimon.core.stores.tenant_ledger import get_channel_spend, get_prepaid_balance
from daimon.core.stores.turn_usage import read_accounting_revisions
from daimon.core.usage_delegation import HostUsageBinding, read_delegated_usage
from mux.contracts.ids import Scope
from mux.state.lease import Slot
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

BUDGET_WINDOWS: tuple[BudgetWindow, ...] = get_args(BudgetWindow)
_MAX_LIMIT_USD = Decimal("9999999999.99")  # Numeric(12, 2)


class ChannelBudgetError(DaimonError):
    """A budget request that cannot be stored; the message says what to fix."""


@dataclass(frozen=True)
class BudgetSpec:
    """A validated budget request: money as Decimal, bounds as aware UTC datetimes."""

    limit_usd: Decimal
    window: BudgetWindow
    starts_at: datetime | None
    ends_at: datetime | None


@dataclass(frozen=True)
class BudgetPeriod:
    """The half-open range `[since, until)` of debits a budget counts; None is unbounded."""

    since: datetime | None
    until: datetime | None


@dataclass(frozen=True)
class ChannelBudgetStatus:
    budget: ChannelBudgetRow
    spent_usd: Decimal
    is_active: bool
    """False before a `total` or `fixed` budget starts, or after a `fixed` one ends."""

    @property
    def remaining_usd(self) -> Decimal:
        return max(Decimal("0"), self.budget.limit_usd - self.spent_usd)

    @property
    def is_exceeded(self) -> bool:
        return self.is_active and self.spent_usd >= self.budget.limit_usd

    @property
    def percent_used(self) -> int:
        """Spend as a whole percentage of the limit; a limit of 0 reads as 100."""
        if self.budget.limit_usd <= 0:
            return 100
        return int(self.spent_usd / self.budget.limit_usd * 100)


def _parse_usd(value: str) -> Decimal:
    try:
        amount = Decimal(value.strip())
    except (InvalidOperation, ValueError) as exc:
        raise ChannelBudgetError(f"limit_usd must be a dollar amount, got {value!r}") from exc
    exponent = amount.as_tuple().exponent
    if not amount.is_finite() or amount < 0 or amount > _MAX_LIMIT_USD:
        raise ChannelBudgetError("limit_usd must be a nonnegative dollar amount")
    if isinstance(exponent, int) and exponent < -2:
        raise ChannelBudgetError("limit_usd must have at most two decimal places")
    return amount


def _parse_instant(value: str | None, *, name: str) -> datetime | None:
    """ISO 8601; a value without an offset is read as UTC."""
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ChannelBudgetError(f"{name} must be an ISO 8601 date or time, got {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_budget_spec(
    *, limit_usd: str, window: str, starts_at: str | None = None, ends_at: str | None = None
) -> BudgetSpec:
    """Validate a budget request from a tool or the CLI, before anything is written."""
    if window not in BUDGET_WINDOWS:
        raise ChannelBudgetError(f"window must be one of {', '.join(BUDGET_WINDOWS)}")
    amount = _parse_usd(limit_usd)
    start = _parse_instant(starts_at, name="starts_at")
    end = _parse_instant(ends_at, name="ends_at")
    if window == "fixed":
        if start is None or end is None:
            raise ChannelBudgetError("a fixed window needs both starts_at and ends_at")
        if start >= end:
            raise ChannelBudgetError("starts_at must be before ends_at")
    elif window == "total" and end is not None:
        raise ChannelBudgetError("ends_at is only for a fixed window")
    elif window == "monthly" and (start is not None or end is not None):
        raise ChannelBudgetError("a monthly window takes no starts_at or ends_at")
    return BudgetSpec(limit_usd=amount, window=window, starts_at=start, ends_at=end)


def budget_period(budget: ChannelBudgetRow, *, now: datetime) -> BudgetPeriod:
    """`monthly`: this UTC month. `total`: since `starts_at`, or ever. `fixed`: its range."""
    if budget.window == "monthly":
        return BudgetPeriod(since=datetime(now.year, now.month, 1, tzinfo=UTC), until=None)
    if budget.window == "total":
        return BudgetPeriod(since=budget.starts_at, until=None)
    return BudgetPeriod(since=budget.starts_at, until=budget.ends_at)


def is_budget_active(budget: ChannelBudgetRow, *, now: datetime) -> bool:
    """A budget gates from `starts_at`, if set, and a fixed one until `ends_at`, exclusive."""
    if budget.window == "monthly":
        return True
    if budget.starts_at is not None and now < budget.starts_at:
        return False
    return budget.ends_at is None or now < budget.ends_at


def _instant_label(value: datetime) -> str:
    if value.hour == value.minute == value.second == 0:
        return f"{value:%Y-%m-%d}"
    return f"{value:%Y-%m-%d %H:%M} UTC"


def window_label(budget: ChannelBudgetRow, *, started: bool = True) -> str:
    """The window in words, e.g. `monthly`, `since 2026-07-01`, `2026-07-01 until 2026-07-03`.

    A total window that has not `started` reads `from 2026-07-01`. A fixed
    window's end is exclusive, hence "until".
    """
    if budget.window == "monthly":
        return "monthly"
    if budget.starts_at is None:
        return "total"
    if budget.window == "total" or budget.ends_at is None:
        return f"{'since' if started else 'from'} {_instant_label(budget.starts_at)}"
    return f"{_instant_label(budget.starts_at)} until {_instant_label(budget.ends_at)}"


def describe_budget(status: ChannelBudgetStatus) -> str:
    """`$1.20 of $5.00 (monthly)`: the line every surface shows for a channel budget."""
    budget = status.budget
    label = window_label(budget, started=status.is_active)
    return f"${status.spent_usd:,.2f} of ${budget.limit_usd:,.2f} ({label})"


async def load_budget_status(
    session: AsyncSession, budget: ChannelBudgetRow, *, now: datetime
) -> ChannelBudgetStatus:
    period = budget_period(budget, now=now)
    spent = await get_channel_spend(
        session,
        tenant_id=budget.tenant_id,
        channel_id=budget.channel_id,
        since=period.since,
        until=period.until,
    )
    return ChannelBudgetStatus(
        budget=budget, spent_usd=spent, is_active=is_budget_active(budget, now=now)
    )


def rank_by_percent_used(
    statuses: list[ChannelBudgetStatus],
) -> list[ChannelBudgetStatus]:
    """Active budgets first, each group by the share of its limit spent, highest first. Pure."""
    return sorted(
        statuses,
        key=lambda s: (
            not s.is_active,
            -(s.spent_usd / s.budget.limit_usd if s.budget.limit_usd > 0 else Decimal("Infinity")),
            s.budget.channel_id,
        ),
    )


async def list_channel_budget_statuses(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str | None, now: datetime
) -> list[ChannelBudgetStatus]:
    """Every budget of the tenant (on `platform`, if given) with its spend, by
    `rank_by_percent_used`."""
    budgets = await list_channel_budgets(session, tenant_id=tenant_id)
    return rank_by_percent_used(
        [
            await load_budget_status(session, budget, now=now)
            for budget in budgets
            if platform is None or budget.platform == platform
        ]
    )


async def get_channel_budget_status(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    now: datetime,
) -> ChannelBudgetStatus | None:
    """The channel's budget with its spend so far, or None when it has no budget."""
    budget = await get_channel_budget(
        session, tenant_id=tenant_id, platform=platform, channel_id=channel_id
    )
    if budget is None:
        return None
    return await load_budget_status(session, budget, now=now)


async def is_over_channel_budget(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str | None,
    now: datetime,
    session: AsyncSession | None = None,
) -> bool:
    """True iff the channel has an active budget whose spend has reached its limit.

    `channel_id=None` (an older DM, an MCP call, a routine with no channel) is never
    gated. Exceptions propagate: admission fails closed.
    """
    if channel_id is None:
        return False
    if session is None:
        async with sessionmaker() as opened:
            status = await get_channel_budget_status(
                opened, tenant_id=tenant_id, platform=platform, channel_id=channel_id, now=now
            )
    else:
        status = await get_channel_budget_status(
            session, tenant_id=tenant_id, platform=platform, channel_id=channel_id, now=now
        )
    return status is not None and status.is_exceeded


async def balance_footer(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    budget_channel_id: str | None,
    now: datetime,
) -> str | None:
    """A turn footer's money: what an active channel budget has left, else the prepaid
    balance, as ``$8.10 left``; None for an operator-funded tenant outside a budgeted
    channel."""
    if budget_channel_id is not None:
        status = await get_channel_budget_status(
            session, tenant_id=tenant_id, platform=platform, channel_id=budget_channel_id, now=now
        )
        if status is not None and status.is_active:
            return f"${status.remaining_usd:.2f} left"
    balance = await get_prepaid_balance(session, tenant_id=tenant_id)
    return None if balance is None else f"${balance:.2f} left"


def may_set_channel_budget(subject: Subject, channel_id: str) -> Decision:
    """Whether ``subject`` may set, clear or raise ``channel_id``'s budget. Pure.

    No access policy fact counts, so the empty policy stands in for it.
    """
    return authorize(
        TenantAccessPolicy(),
        subject=subject,
        action=Action.SET_CHANNEL_BUDGET,
        place=Place(channel_id=channel_id),
    )


@dataclass(frozen=True)
class FleetBudgetUsage:
    """Persisted financial facts, not a bearer capability for native dispatch.

    Exposure includes all unknown final usage at its whole worker reservation.
    Original ledger debit is already contained in exposure, never added twice.
    A terminal status is not final-actual proof; this interface never refunds.
    """

    run: FleetRun
    worker: FleetWorker
    worker_floor_usd: Decimal
    prepaid_available_usd: Decimal | None
    channel_available_usd: Decimal | None
    admitted: bool
    reason: str | None

    @property
    def run_exposure_usd(self) -> Decimal:
        return self.run.reserved_usd

    @property
    def worker_exposure_usd(self) -> Decimal:
        return self.worker.reserved_usd


async def _fleet_worker_money(
    session: AsyncSession,
    run: FleetRun,
    worker: FleetWorker,
    *,
    scope: Scope,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
    price_floor: bool,
) -> tuple[Decimal, Decimal, bool]:
    binding = await mux_state.get_binding(session, worker_slot(run, worker))
    if binding is None:
        if worker.binding_id is not None:
            raise FleetConflict("accepted worker binding is unavailable")
        return Decimal(0), Decimal(0), False
    if (
        binding.provider != worker.backend
        or binding.profile != worker.profile
        or worker.binding_id not in (None, binding.id)
    ):
        raise FleetConflict("worker binding differs from its admitted identity")
    rows = await read_accounting_revisions(session, tenant_id=run.tenant_id, binding=binding)
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        debit = -sum((row.delta_usd for row in rows.ledger), Decimal(0))
        if debit < 0:
            raise FleetConflict("worker ledger cannot prove nonnegative spend")
        if not rows.revisions or not price_floor:
            return debit, debit, False
        native = rows.revisions[0].observation.session
        usage = await read_delegated_usage(
            session, scope=scope, bindings=(HostUsageBinding(binding, native),), prices=prices
        )
        floor = (usage.token_floor_usd * markup).quantize(
            Decimal("0.000001"), rounding=ROUND_CEILING
        )
        return max(floor, debit), debit, bool(usage.unpriced_observations)


async def _produce_fleet_budget(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    worker_id: uuid.UUID,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
    reserve: bool,
) -> FleetBudgetUsage:
    """The sole product producer of held exposure and admission decisions.

    Use the store's tenant/actor/run/worker lock order. Read originals, derive
    conservative money once, and persist before returning an admission refusal.
    Terminal/crashed rows participate; changing status cannot free a bound.
    Caller owns commit, including refusals that retain new overrun evidence.
    """
    if not markup.is_finite() or markup <= 0:
        raise FleetConflict("fleet billing markup must be positive and finite")
    facts = parent.fleet
    if facts.scope.is_platform or facts.scope.is_legacy_host_authorized:
        raise FleetConflict("fleet budget requires tenant authority")
    tenant_id = uuid.UUID(facts.scope.tenant_id)
    graph = await fleet_store.lock_budget(session, tenant_id=tenant_id, actor_id=facts.account_id)
    pair = next((pair for pair in graph if pair[0].id == facts.run_id), None)
    if pair is None:
        raise FleetConflict("fleet run is unavailable")
    run, workers = pair
    worker = next((w for w in workers if w.id == worker_id), None)
    if worker is None or (
        run.orchestrator_account_id != facts.account_id
        or facts.scope.account_id != str(run.orchestrator_account_id)
        or (run.platform, run.channel_id, run.thread_id)
        != (facts.root.channel.platform, facts.root.channel.channel_id, facts.root.thread_id)
    ):
        raise FleetConflict("fleet budget belongs to another root")
    actor = await accounts.get_account(session, facts.account_id)
    if actor is None or actor.tenant_id != tenant_id:
        raise FleetConflict("financial admission requires a live tenant actor")
    if facts.root.channel.tenant_id != str(tenant_id):
        raise FleetConflict("root channel differs from tenant authority")
    root_binding = await mux_state.get_binding(
        session, Slot(thread=facts.root, account_id=str(facts.account_id))
    )
    if root_binding is None:
        root_binding = await mux_state.get_binding(
            session, Slot(thread=facts.root, account_id=None)
        )
    if root_binding is None or (
        root_binding.id != run.orchestrator_binding_id
        or root_binding.native_refs.get("session") != run.orchestrator_session_id
    ):
        raise FleetConflict("financial admission requires the current root binding")
    if run.total_budget_usd > parent.limits.budget_usd:
        raise FleetConflict("fleet cap exceeds the authenticated parent budget")

    # The same resolved tariff gate applies before reservation, dispatch and
    # every refresh, including workers with no native usage binding yet.
    price = prices.get((worker.backend, worker.model))
    pricing_unavailable = price is None or (
        isinstance(price, ProviderPrice)
        and (
            not price.verified
            or (price.provider, price.model) != (worker.backend, worker.model)
            or price.checked_on > datetime.now(UTC).date()
        )
    )
    if isinstance(price, ModelRates):
        for variant in (price, *((price.long_context,) if price.long_context else ())):
            for value in (variant.input, variant.output, variant.cache_read, variant.cache_write):
                rate = Decimal(str(value))
                pricing_unavailable = pricing_unavailable or not rate.is_finite() or rate < 0
    floors: dict[uuid.UUID, Decimal] = {}
    exposures: dict[uuid.UUID, Decimal] = {}
    debits: dict[uuid.UUID, Decimal] = {}
    with localcontext() as arithmetic:
        arithmetic.prec = 80
        for other_run, members in graph:
            debit_total = Decimal(0)
            for member in members:
                floor, debit, unpriced = await _fleet_worker_money(
                    session,
                    other_run,
                    member,
                    scope=facts.scope,
                    prices=prices,
                    markup=markup,
                    price_floor=other_run.id == run.id,
                )
                pricing_unavailable = pricing_unavailable or unpriced
                floors[member.id] = floor
                # Unfunded rows do not acquire a reservation by being read.
                exposures[member.id] = max(member.reserved_usd, floor)
                debit_total += debit
            debits[other_run.id] = debit_total
        graph = await fleet_store.save_budget(
            session, tenant_id=tenant_id, graph=graph, exposures=exposures, debits=debits
        )
        run, workers = next(pair for pair in graph if pair[0].id == run.id)
        worker = next(w for w in workers if w.id == worker_id)
        residual = sum((r.reserved_usd - r.observed_usd for r, _ in graph), Decimal(0))
        balance = await tenant_ledger.get_prepaid_balance(session, tenant_id=tenant_id)
        available = None if balance is None else balance - residual
        channel = await get_channel_budget_status(
            session,
            tenant_id=tenant_id,
            platform=run.platform,
            channel_id=run.channel_id,
            now=datetime.now(UTC),
        )
        channel_available: Decimal | None = None
        if channel is not None and channel.is_active:
            held = sum(
                (
                    r.reserved_usd - r.observed_usd
                    for r, _ in graph
                    if (r.platform, r.channel_id) == (run.platform, run.channel_id)
                ),
                Decimal(0),
            )
            channel_available = channel.remaining_usd - held
        reason = None
        if balance is None:
            reason = "funding_unavailable"
        elif available is not None and (
            available < 0 or (reserve and run.total_budget_usd > available + run.reserved_usd)
        ):
            reason = "tenant_budget_exceeded"
        elif channel_available is not None and (
            channel_available < 0
            or (reserve and run.total_budget_usd > channel_available + run.reserved_usd)
        ):
            reason = "channel_budget_exceeded"
        elif any(r.reserved_usd > r.total_budget_usd for r, _ in graph):
            reason = "run_budget_exceeded"
        elif (
            any(w.reserved_usd > w.budget_usd for _, members in graph for w in members)
            or floors[worker.id] >= worker.budget_usd
        ):
            reason = "worker_budget_exceeded"
        elif pricing_unavailable:
            reason = "pricing_unavailable"
        elif run.status != "running" or worker.status not in ("pending", "starting", "running"):
            reason = "terminal"
        elif worker.attempts or worker.turns_used:
            reason = "accepted_attempt_inventory_unavailable"
        if reserve and reason is None and worker.reserved_usd < worker.budget_usd:
            delta = worker.budget_usd - worker.reserved_usd
            if worker.budget_usd <= 0:
                reason = "worker_budget_exceeded"
            elif run.reserved_usd + delta > run.total_budget_usd:
                reason = "run_budget_exceeded"
            elif available is None or delta > available:
                reason = "tenant_budget_exceeded"
            elif channel_available is not None and delta > channel_available:
                reason = "channel_budget_exceeded"
            else:
                exposures[worker.id] = worker.budget_usd
                graph = await fleet_store.save_budget(
                    session, tenant_id=tenant_id, graph=graph, exposures=exposures, debits=debits
                )
                run, workers = next(pair for pair in graph if pair[0].id == run.id)
                worker = next(w for w in workers if w.id == worker_id)
                available -= delta
                if channel_available is not None:
                    channel_available -= delta
        admitted = reason is None and worker.reserved_usd >= worker.budget_usd > 0
    return FleetBudgetUsage(
        run, worker, floors[worker.id], available, channel_available, admitted, reason
    )


async def produce_fleet_budget(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    worker_id: uuid.UUID,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
) -> FleetBudgetUsage:
    """Refresh durable money from original usage; never implicitly reserve."""
    return await _produce_fleet_budget(
        session, parent=parent, worker_id=worker_id, prices=prices, markup=markup, reserve=False
    )


async def reserve_fleet_worker(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    policy: TenantAccessPolicy,
    worker_id: uuid.UUID,
    expected_version: int,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
) -> FleetBudgetUsage:
    """Financial admission in the SAME caller transaction as canonical spawn.

    Call before any provider allocation; pool.claim alone is not admission.
    Consumer commits the returned refusal too, preserving overrun evidence, and
    dispatches only an admitted pending/starting first attempt. Unknown native
    attempts/replacement and operator-funded tenants remain unavailable here.
    """
    decision = authorize(
        policy,
        subject=parent.subject,
        action=Action.SPAWN_WORKER,
        surface=Surface.FLEET,
        agent=parent.agent,
        place=parent.fleet.place,
        fleet=parent.fleet,
    )
    if not decision:
        raise FleetConflict("reservation requires current root authorization")
    graph = await fleet_store.lock_budget(
        session, tenant_id=uuid.UUID(parent.fleet.scope.tenant_id), actor_id=parent.fleet.account_id
    )
    pair = next((p for p in graph if p[0].id == parent.fleet.run_id), None)
    if pair is None:
        raise FleetConflict("fleet run is unavailable")
    worker = next((w for w in pair[1] if w.id == worker_id), None)
    if worker is None:
        raise FleetConflict("worker is unavailable")
    if worker.reserved_usd == 0 and worker.version != expected_version:
        raise FleetConflict("worker changed before reservation")
    if worker.status not in ("pending", "starting") or worker.binding_id is not None:
        raise FleetConflict("a new reservation requires an unallocated worker")
    # Spec attenuation is the resolver/driver's responsibility. Financial
    # admission repeats the shared root authorization, not its spec compiler.
    if worker.budget_usd > parent.limits.budget_usd:
        raise FleetConflict("worker reservation exceeds parent authority")
    snapshot = await _produce_fleet_budget(
        session, parent=parent, worker_id=worker_id, prices=prices, markup=markup, reserve=True
    )
    if snapshot.admitted and worker.reserved_usd == 0:
        audit = await audit_worker_action(
            session,
            parent,
            worker_id=worker_id,
            operation="spawn",
            outcome="allowed",
            reason="authorized",
        )
        if audit is None:
            raise FleetConflict("reservation requires a retained actor audit")
    return snapshot


async def watch_fleet_budget(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    parent: WorkerPermissions,
    worker_id: uuid.UUID,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
    cancelled: asyncio.Event,
    finished: asyncio.Event,
    poll_interval: float = 0.25,
    read_timeout: float | None = None,
) -> str:
    """Watch the REAL producer; run_prepared_turn owns the only worker deadline.

    Equal run exposure from complete reservations is admissible. A worker's
    spent floor at its bound cancels to prevent further work; run overrun is
    strictly greater because unknown reservations alone are not spent usage.
    """
    if parent.fleet.enabled is not True or parent.fleet.depth != 0:
        raise FleetConflict("fleet watch requires explicit root opt-in")
    timeout = read_timeout if read_timeout is not None else poll_interval * 8
    if not all(math.isfinite(v) and v > 0 for v in (poll_interval, timeout)):
        raise ValueError("budget poll and read timeout must be positive finite durations")
    try:
        while not finished.is_set() and not cancelled.is_set():
            try:
                async with asyncio.timeout(timeout):
                    async with sessionmaker.begin() as session:
                        usage = await produce_fleet_budget(
                            session,
                            parent=parent,
                            worker_id=worker_id,
                            prices=prices,
                            markup=markup,
                        )
            except Exception as error:
                log.warning(
                    "fleet.budget_read_unavailable",
                    reason="read_unavailable",
                    error_class=type(error).__name__,
                )
                cancelled.set()
                return "usage_unavailable"
            if usage.reason not in (None, "terminal") or not usage.admitted:
                log.warning("fleet.budget_cancel", reason=usage.reason or "not_admitted")
                cancelled.set()
                return "budget_exceeded"
            stop = asyncio.create_task(cancelled.wait())
            done = asyncio.create_task(finished.wait())
            try:
                await asyncio.wait(
                    (stop, done), timeout=poll_interval, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                stop.cancel()
                done.cancel()
                await asyncio.gather(stop, done, return_exceptions=True)
        return "finished" if finished.is_set() else "cancelled"
    except asyncio.CancelledError:
        cancelled.set()
        raise


async def require_fleet_worker_admission(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    policy: TenantAccessPolicy,
    worker_id: uuid.UUID,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
) -> FleetBudgetUsage:
    """Pre-allocation readback; a pool claim or a cached snapshot is insufficient.

    Never implicitly funds a claimed worker. Return refusal rather than raise
    after producing money, so callers commit overrun evidence, then stop before
    native allocation. The resolver/driver still proves fresh spec/resources.
    """
    decision = authorize(
        policy,
        subject=parent.subject,
        action=Action.SPAWN_WORKER,
        surface=Surface.FLEET,
        agent=parent.agent,
        place=parent.fleet.place,
        fleet=parent.fleet,
    )
    if not decision:
        raise FleetConflict("native dispatch requires current root authority")
    usage = await produce_fleet_budget(
        session, parent=parent, worker_id=worker_id, prices=prices, markup=markup
    )
    if usage.worker.status != "starting" or usage.worker.binding_id is not None:
        return replace(usage, admitted=False, reason="not_an_unallocated_pool_claim")
    return usage


async def admit_fleet_worker(
    session: AsyncSession,
    *,
    parent: WorkerPermissions,
    policy: TenantAccessPolicy,
    resolved: ResolvedWorkerSpec,
    ordinal: int,
    role: str,
    prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
    markup: Decimal,
    reviewer_of: uuid.UUID | None = None,
    result_contract: Mapping[str, JsonValue] | None = None,
) -> FleetBudgetUsage:
    """Canonical spawn plus reservation in the financial lock order.

    The host supplies a freshly resolved spec from the sole owner resolver.
    Lock before add_worker; taking run locks first across two runs could invert
    the tenant money lock. Native creation starts only after this commits.
    """
    decision = authorize(
        policy,
        subject=parent.subject,
        action=Action.SPAWN_WORKER,
        surface=Surface.FLEET,
        agent=parent.agent,
        place=parent.fleet.place,
        fleet=parent.fleet,
    )
    if not decision:
        raise FleetConflict("spawn requires current root authorization")
    if (
        resolved.permissions.fleet.run_id != parent.fleet.run_id
        or resolved.permissions.fleet.origin_id != parent.fleet.origin_id
        or resolved.permissions.fleet.scope.account_id != parent.fleet.scope.account_id
        or resolved.permissions.fleet.scope.tenant_id != parent.fleet.scope.tenant_id
        or resolved.permissions.fleet.depth != 1
    ):
        raise FleetConflict("resolved worker belongs to another root")
    tenant_id = uuid.UUID(parent.fleet.scope.tenant_id)
    await fleet_store.lock_budget(session, tenant_id=tenant_id, actor_id=parent.fleet.account_id)
    worker = await fleet_store.add_worker(
        session,
        tenant_id=tenant_id,
        run_id=parent.fleet.run_id,
        ordinal=ordinal,
        role=role,
        spec=resolved.spec.model_dump(mode="json"),
        spec_digest=resolved.digest,
        backend=resolved.backend.backend,
        profile=resolved.backend.profile,
        model=resolved.spec.model.id,
        budget_usd=resolved.limits.budget_usd,
        deadline_at=resolved.limits.deadline,
        max_turns=resolved.limits.max_turns,
        reviewer_of=reviewer_of,
        result_contract=result_contract,
    )
    return await reserve_fleet_worker(
        session,
        parent=parent,
        policy=policy,
        worker_id=worker.id,
        expected_version=worker.version,
        prices=prices,
        markup=markup,
    )
