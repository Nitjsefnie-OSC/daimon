"""One-attempt Anthropic probe admission. No effect until explicit live selection.

The financial hold and streamed stop are controls, never provider token caps.
Native usage lacking billed runtime remains incomplete for N8 reconciliation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Literal, Never, cast

import httpx
from anthropic import AsyncAnthropic
from daimon.testing.isolated_sdk import TLS_PROXY_ENV_VARS, build_async_anthropic
from mux.conformance.budget import (
    BudgetConfig,
    BudgetGuard,
    ContainerAllowance,
    ModelPrice,
    ProbePlan,
    Reservation,
    SessionPrice,
    SpendReceipt,
    TokenLimits,
    TokenUsage,
)
from mux.conformance.recording import Audit, RecordingError
from mux.conformance.usage_settlement import AnthropicSessionSpend, actual_from_anthropic_sessions
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.usage import UsageObservation
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources._authorization import ResourceAuthorization
from mux.drivers.anthropic.usage import observation_from_event
from mux.errors import ScopeViolation
from pydantic import BaseModel, ConfigDict, JsonValue

MODEL = "claude-haiku-5-5"
HOST_CONFIG_SHA256 = "36c06fdfe606ba07f42f6f5866cbc68f13b25387fc9aceb62f256ffbabc247e9"
LEDGER_ID = "091ee642b92141f88a8f3026edaa1a5a"
Package = Literal["A7", "F2-Anthropic"]


class LiveRefused(ValueError):
    """Constant, credential-free diagnostics only."""


class LiveStopped(RuntimeError):
    """A stream stop control fired; no replay or new session is allowed."""


class LiveBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scope: Scope
    account_scope_id: str
    environment: ResourceRef
    agent: ResourceRef | None = None
    database_url: str | None = None
    mcp_url: str | None = None
    mcp_bearer_file: Path | None = None

    def verify(self, package: Package) -> None:
        if self.scope.is_platform or self.scope.is_legacy_host_authorized:
            raise LiveRefused("live probes require an authorized tenant/account scope")
        for ref, kind in ((self.environment, "environment"), (self.agent, "agent")):
            if ref is None:
                continue
            if (
                ref.provider != "anthropic"
                or ref.kind != kind
                or ref.account_scope_id != self.account_scope_id
                or ref.tenant_id != self.scope.tenant_id
                or ref.account_id != self.scope.account_id
            ):
                raise LiveRefused("live resource binding is outside the admitted scope")
        if package == "A7" and (self.agent is None or self.database_url is None):
            raise LiveRefused("A7 requires an owned DB and authorized disposable agent")
        if package == "F2-Anthropic":
            if not self.mcp_url or not self.mcp_bearer_file:
                raise LiveRefused("F2 requires a provider-reachable owned MCP binding")
            url = httpx.URL(self.mcp_url)
            if (
                url.scheme != "https"
                or not url.host
                or url.host.endswith(".invalid")
                or url.host in {"localhost", "127.0.0.1", "::1"}
                or url.userinfo
                or url.query
            ):
                raise LiveRefused("F2 requires an explicit public HTTPS MCP destination")


def digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def explicit_secret(path: Path, *, env_file: bool = False) -> str:
    """Read only after admission, without sourcing or consulting environment values."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 16384
        ):
            raise LiveRefused("credential file must be owned, regular and mode 0600")
        data = os.read(descriptor, 16385).decode()
    finally:
        os.close(descriptor)
    if env_file:
        lines = [
            line.strip()
            for line in data.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if len(lines) != 1 or not lines[0].startswith("ANTHROPIC_API_KEY="):
            raise LiveRefused(
                "credential file must contain only an explicit Anthropic key assignment"
            )
        data = lines[0].split("=", 1)[1].strip().strip("\"'")
    value = data.strip()
    if not value or any(c.isspace() for c in value):
        raise LiveRefused("explicit credential is missing or malformed")
    return value


def refuse_ambient() -> None:
    if any(
        name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
        or name.upper() in TLS_PROXY_ENV_VARS
        for name in os.environ
    ):
        raise LiveRefused("live entry refuses ambient provider, TLS and proxy configuration")


@dataclass(frozen=True)
class Admission:
    package: Package
    approval: Mapping[str, object]
    approval_digest: str
    config_path: Path
    ledger_path: Path
    binding: LiveBinding
    output: Path
    deadline_s: int
    source_head: str

    @property
    def hold(self) -> Decimal:
        return Decimal(".35" if self.package == "A7" else ".65")

    @property
    def plan(self) -> ProbePlan:
        return ProbePlan(
            provider="anthropic",
            model=MODEL,
            fixture_id="A7" if self.package == "A7" else "F1",
            limits=TokenLimits(
                input_tokens=200000 if self.package == "A7" else 500000,
                output_tokens=40000 if self.package == "A7" else 32000,
            ),
            container_allowance=ContainerAllowance(
                meter="agent_session",
                memory_gb=None,
                sessions=2 if self.package == "A7" else 1,
                seconds_per_session=self.deadline_s,
            ),
        )


def admit(
    *,
    package: Package,
    approval_path: Path,
    go_path: Path,
    go_digest: str,
    config_path: Path,
    ledger_path: Path,
    binding_path: Path,
    output: Path,
    source_head: str,
    source_clean: bool,
    now: datetime | None = None,
) -> Admission:
    """A separately lead-pinned GO binds exact approval/install/source/recipe bytes."""
    refuse_ambient()
    go = json.loads(go_path.read_text())
    document = json.loads(approval_path.read_text())
    approval = document.get("approval", document)
    signed = {
        "A7": "8de5c78c88a67a826a626516814a4e2575617d2796545f54da876f3a7b74e9f4",
        "F2-Anthropic": "8eea963b8f3541d8a36d79b87e6e983c40cb9cbe1875086202270e505585fab3",
    }
    if digest_json(approval) != signed[package]:
        raise LiveRefused("financial envelope is not the exact lead-signed replacement")
    if digest_json(go) != go_digest or go.get("live_go") is not True:
        raise LiveRefused("separate lead GO receipt is absent or changed")
    if go.get("approval_digest") != digest_json(approval):
        raise LiveRefused("financial authorization changed after signature")
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", source_head, "origin/integration/neutral-core"],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True,
        check=False,
    )
    if ancestry.returncode != 0:
        raise LiveRefused("live entry has not merged into integration")
    current = now or datetime.now(UTC)
    expires = datetime.fromisoformat(str(approval["expires_at"]).replace("Z", "+00:00"))
    if expires.tzinfo is None or current >= expires:
        raise LiveRefused("financial authorization expired")
    expected = {
        "package": package,
        "provider": "anthropic",
        "model": MODEL,
        "config_sha256": HOST_CONFIG_SHA256,
        "ledger_id": LEDGER_ID,
        "maximum_usd": ".35" if package == "A7" else ".65",
        "sessions": 2 if package == "A7" else 1,
        "native_budget_sent": False,
        "native_budget_must_be_acknowledged": False,
    }
    if any(approval.get(key) != value for key, value in expected.items()):
        raise LiveRefused("financial authorization does not match the admitted probe")
    if (
        not source_clean
        or go.get("merged_source_head") != source_head
        or go.get("package") != package
        or go.get("model") != MODEL
        or go.get("binding_sha256") != hashlib.sha256(binding_path.read_bytes()).hexdigest()
        or go.get("config_sha256") != HOST_CONFIG_SHA256
        or go.get("ledger_id") != LEDGER_ID
        or Path(str(approval.get("ledger_path"))).resolve() != ledger_path.resolve()
    ):
        raise LiveRefused("merged source, binding or installed ledger pin is missing or stale")
    install = Path(str(go.get("install_receipt_path")))
    recipe = Path(str(go.get("recipe_path")))
    if (
        hashlib.sha256(install.read_bytes()).hexdigest() != go.get("install_receipt_sha256")
        or hashlib.sha256(recipe.read_bytes()).hexdigest() != go.get("recipe_sha256")
        or hashlib.sha256(config_path.read_bytes()).hexdigest() != HOST_CONFIG_SHA256
    ):
        raise LiveRefused("installed configuration or reviewed recipe receipt changed")
    deadline = 300 if package == "A7" else 600
    if (
        approval.get("wall_clock_deadline_seconds_total") != deadline
        or approval.get("streamed_usage_monitor", {}).get("threshold_fraction_of_hold") != "0.8"
    ):
        raise LiveRefused("reviewed deadline and streamed-cost controls are missing")
    rates = json.loads(config_path.read_text())["providers"]["anthropic"]["models"][MODEL]
    if digest_json(rates) != "0403ed42c37c8df954e20756c5cc1cf9f5446a2fea90aa721ef2ae87431911b8":
        raise LiveRefused("dated Haiku5.5 pricing changed after signature")
    binding = LiveBinding.model_validate_json(binding_path.read_text())
    binding.verify(package)
    if output.exists():
        raise LiveRefused("preserve previous evidence; output must be fresh")
    return Admission(
        package,
        approval,
        digest_json(approval),
        config_path,
        ledger_path,
        binding,
        output,
        deadline,
        source_head,
    )


@dataclass(frozen=True)
class OwnedAuthorization(ResourceAuthorization):
    created: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])

    def check(self, scope: Scope, kind: str, resource_id: str | None = None) -> None:
        if scope != self.scope:
            raise ScopeViolation(kind, "live scope differs from admission")
        if resource_id is not None and (kind, resource_id) in self.created:
            return
        super().check(scope, kind, resource_id)


@dataclass
class UsageMonitor:
    price: ModelPrice
    session_price: SessionPrice
    threshold: Decimal
    started: float = field(default_factory=time.monotonic)
    observations: dict[str, dict[str, JsonValue]] = field(
        default_factory=dict[str, dict[str, JsonValue]]
    )
    observed_thresholds: list[str] = field(default_factory=list[str])
    stop_reason: str | None = None
    cost: Decimal | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    event_sessions: dict[str, str] = field(default_factory=dict[str, str])
    observed_times: dict[str, datetime] = field(default_factory=dict[str, datetime])
    verified_models: dict[str, str] = field(default_factory=dict[str, str])
    threads: dict[str, str] = field(default_factory=dict[str, str])

    def threshold_receipts(self) -> list[dict[str, JsonValue]]:
        facts: list[dict[str, JsonValue]] = []
        for id_, raw in self.observations.items():
            session = self.event_sessions[id_]
            try:
                obs = observation_from_event(
                    raw,
                    ResourceRef(
                        provider="anthropic", account_scope_id="monitor", kind="session", id=session
                    ),
                    observed_at=self.observed_times[id_],
                    model_id=MODEL,
                )
                input_, output_ = obs.input_tokens, obs.output_tokens
            except (ValueError, KeyError, TypeError):
                input_ = output_ = None
            facts.append(
                {
                    "id": id_,
                    "session_id": session,
                    "served_model_verified": self.verified_models.get(session) == MODEL,
                    "observed_at": self.observed_times[id_].isoformat(),
                    "inclusive_input_tokens": input_,
                    "output_tokens": output_,
                    "input_threshold_status": "not_observed"
                    if input_ is None
                    else "exceeded"
                    if input_ > 100000
                    else "observed_within_threshold",
                    "output_threshold_status": "not_observed"
                    if output_ is None
                    else "exceeded"
                    if output_ > 20000
                    else "observed_within_threshold",
                }
            )
        return facts

    def halt(self, reason: str) -> Never:
        self.stop_reason = reason
        self.stop.set()
        raise LiveStopped(reason)

    def observe(self, raw: Mapping[str, JsonValue], session_id: str = "monitor") -> None:
        with localcontext() as context:
            context.prec = 80
            try:
                self._observe(raw, session_id)
            except (ValueError, KeyError, TypeError):
                self.halt("native usage record is malformed")

    def _observe(self, raw: Mapping[str, JsonValue], session_id: str) -> None:
        thread = raw.get("thread_id")
        if isinstance(thread, str) and thread:
            previous = self.threads.setdefault(session_id, thread)
            if previous != thread:
                self.halt("unexpected second native thread")
        if raw.get("type") == "span.model_request_end":
            id_ = raw.get("id")
            if not isinstance(id_, str) or id_ in self.observations:
                self.halt("duplicate or unidentified usage")
            self.observations[id_] = dict(raw)
            self.event_sessions[id_] = session_id
            self.observed_times[id_] = datetime.now(UTC)
        total = Decimal(0)
        for event in self.observations.values():
            ref = ResourceRef(
                provider="anthropic", account_scope_id="monitor", kind="session", id="monitor"
            )
            obs = observation_from_event(event, ref, observed_at=datetime.now(UTC), model_id=MODEL)
            cache = obs.native_meter.get("cache_creation")
            duration = cache if isinstance(cache, dict) else {}
            tokens = TokenUsage(
                input_tokens=obs.input_tokens,
                output_tokens=obs.output_tokens,
                input_cached_tokens=obs.input_cached_tokens,
                input_cache_write_tokens=obs.input_cache_write_tokens,
                input_cache_write_5m_tokens=cast(
                    int | None, duration.get("ephemeral_5m_input_tokens")
                ),
                input_cache_write_1h_tokens=cast(
                    int | None, duration.get("ephemeral_1h_input_tokens")
                ),
            )
            if ((obs.input_tokens or 0) > 100000 or (obs.output_tokens or 0) > 20000) and str(
                event["id"]
            ) not in self.observed_thresholds:
                self.observed_thresholds.append(str(event["id"]))
            amount = self.price.actual(tokens, datetime.now(UTC))
            if amount is None:
                self.halt("usage cost is unknown")
            total += amount
        # Wall time is a conservative monitoring estimate, never billed-runtime evidence.
        total += self.session_price.cost(Decimal(str(time.monotonic() - self.started)))
        self.cost = total
        if total >= self.threshold:
            self.halt("streamed cost reached the stop threshold")


class MonitoredStream(httpx.AsyncByteStream):
    def __init__(
        self, stream: httpx.AsyncByteStream, monitor: UsageMonitor, session_id: str
    ) -> None:
        self.stream, self.monitor, self.session_id = stream, monitor, session_id
        self.buffer = b""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.stream:
            self.buffer += chunk.replace(b"\r\n", b"\n")
            if len(self.buffer) > 2_000_000:
                self.monitor.halt("native SSE frame exceeded the evidence limit")
            while b"\n\n" in self.buffer:
                frame, self.buffer = self.buffer.split(b"\n\n", 1)
                payload = b"\n".join(
                    line[5:].lstrip() for line in frame.splitlines() if line.startswith(b"data:")
                )
                if payload:
                    event = json.loads(payload)
                    if isinstance(event, dict):
                        self.monitor.observe(cast(dict[str, JsonValue], event), self.session_id)
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


class LiveTransport(httpx.AsyncBaseTransport):
    """Observe accepted create IDs and enforce one native session/no replay POSTs."""

    def __init__(
        self,
        upstream: httpx.AsyncBaseTransport,
        authorization: OwnedAuthorization,
        monitor: UsageMonitor,
        *,
        max_user_sends: int,
    ) -> None:
        self.upstream, self.authorization, self.monitor = upstream, authorization, monitor
        self.max_user_sends = max_user_sends
        self.session_attempts = self.user_sends = 0
        self.session_id: str | None = None
        self.session_ids: list[str] = []
        self.archived: set[str] = set()
        self.skill_bodies: list[tuple[str, bytes]] = []
        self.agent_body: dict[str, JsonValue] | None = None
        self.requests: list[tuple[str, str]] = []
        self.stopping = False
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.closed:
            raise LiveStopped("live transport sealed after cleanup")
        path = request.url.path
        stopped = self.stopping or self.monitor.stop.is_set()
        if (
            stopped
            and request.method == "POST"
            and not (path.endswith("/events") or path.endswith("/archive"))
        ):
            raise LiveStopped("paid mutation refused after a stop control")
        if path.startswith("/v1/sessions/") and path.split("/")[3] not in self.session_ids:
            raise LiveStopped("native request escaped the accepted session inventory")
        self.requests.append((request.method, path))
        if request.method == "POST" and path == "/v1/sessions":
            if self.stopping or self.monitor.stop_reason is not None:
                raise LiveStopped("new session refused after a stop control")
            if self.session_ids and self.session_ids[-1] not in self.archived:
                raise LiveStopped("prior session cleanup has not been acknowledged")
            self.session_attempts += 1
            if self.session_attempts > self.max_user_sends:
                raise LiveStopped("fresh or retry session refused")
        if request.method == "POST" and path.endswith("/events"):
            body = json.loads(await request.aread())
            if stopped and any(
                item.get("type") != "user.interrupt" for item in body.get("events", [])
            ):
                raise LiveStopped("non-cleanup event refused after a stop control")
            if any(item.get("type") == "user.message" for item in body.get("events", [])):
                self.user_sends += 1
                if (
                    self.stopping
                    or self.monitor.stop_reason is not None
                    or self.user_sends > self.max_user_sends
                ):
                    raise LiveStopped("retry or extra user turn refused")
        if request.method == "POST" and path == "/v1/skills":
            self.skill_bodies.append((request.headers["content-type"], await request.aread()))
        if request.method == "POST" and path == "/v1/agents":
            self.agent_body = json.loads(await request.aread())
        response = await self.upstream.handle_async_request(request)
        if response.is_success and path.endswith("/archive"):
            self.archived.add(path.split("/")[-2])
        if "text/event-stream" in response.headers.get("content-type", ""):
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=MonitoredStream(
                    cast(httpx.AsyncByteStream, response.stream), self.monitor, path.split("/")[3]
                ),
                extensions=response.extensions,
            )
        if (
            response.is_success
            and request.method == "POST"
            and path in {"/v1/sessions", "/v1/agents", "/v1/skills", "/v1/vaults"}
        ):
            raw = cast(dict[str, JsonValue], json.loads(await response.aread()))
            id_ = raw.get("id")
            if not isinstance(id_, str) or not id_:
                raise LiveStopped("native create acknowledgement has no resource identity")
            kind = {
                "/v1/sessions": "session",
                "/v1/agents": "agent",
                "/v1/skills": "skill",
                "/v1/vaults": "vault",
            }[path]
            self.authorization.created.add((kind, id_))
            if kind == "session":
                self.session_id = id_
                self.session_ids.append(id_)
                agent = raw.get("agent")
                model = agent.get("model") if isinstance(agent, dict) else None
                if isinstance(model, dict):
                    model = model.get("id")
                if model == MODEL:
                    self.monitor.verified_models[id_] = MODEL
        return response

    async def aclose(self) -> None:
        self.closed = True
        await self.upstream.aclose()


@dataclass
class LiveContext:
    admission: Admission
    client: AsyncAnthropic
    driver: AnthropicManagedAgents
    transport: LiveTransport
    reservation: Reservation
    evidence: list[AnthropicSessionSpend] = field(default_factory=list[AnthropicSessionSpend])
    retire: Callable[[], Awaitable[None]] | None = None
    redaction_secrets: tuple[str, ...] = field(default=(), repr=False)

    def ref(self, id_: str, kind: str) -> ResourceRef:
        b = self.admission.binding
        return ResourceRef(
            id=id_,
            kind=kind,
            provider="anthropic",
            account_scope_id=b.account_scope_id,
            tenant_id=b.scope.tenant_id,
            account_id=b.scope.account_id,
        )

    async def stop_session(self) -> None:
        self.transport.stopping = True
        for id_ in self.transport.session_ids:
            if id_ in self.transport.archived:
                continue
            ref = self.ref(id_, "session")
            try:
                async with asyncio.timeout(3):
                    await self.driver.events.cancel(
                        self.admission.binding.scope, ref, turn_id="live-root", key="live-stop"
                    )
            finally:
                async with asyncio.timeout(3):
                    await self.driver.session_admin.archive(
                        self.admission.binding.scope, ref, key="live-archive"
                    )


def merged_usage(context: LiveContext) -> tuple[AnthropicSessionSpend, ...]:
    """Keep verified streamed requests even when the terminal fetch fails."""
    by_session: dict[str, dict[str, UsageObservation]] = {}
    for evidence in context.evidence:
        if evidence.session.id not in context.transport.session_ids:
            raise LiveRefused("walk evidence is outside the accepted inventory")
        if context.transport.monitor.verified_models.get(evidence.session.id) != MODEL:
            raise LiveRefused("walk evidence has no served-model provenance")
        by_session[evidence.session.id] = {obs.id: obs for obs in evidence.observations}
    monitor = context.transport.monitor
    for id_, raw in monitor.observations.items():
        session_id = monitor.event_sessions[id_]
        if (
            session_id not in context.transport.session_ids
            or monitor.verified_models.get(session_id) != MODEL
        ):
            continue  # Raw unverified facts remain in the receipt, never promoted to the ledger.
        try:
            obs = observation_from_event(
                raw,
                context.ref(session_id, "session"),
                observed_at=monitor.observed_times[id_],
                model_id=MODEL,
            )
        except (ValueError, KeyError, TypeError):
            continue
        bucket = by_session.setdefault(session_id, {})
        prior = bucket.get(id_)
        if prior is not None and (
            prior.input_tokens != obs.input_tokens
            or prior.output_tokens != obs.output_tokens
            or prior.input_cached_tokens != obs.input_cached_tokens
            or prior.input_cache_write_tokens != obs.input_cache_write_tokens
            or prior.native_meter.get("cache_creation") != obs.native_meter.get("cache_creation")
        ):
            raise LiveRefused("stream and walk usage disagree")
        bucket.setdefault(id_, obs)
    return tuple(
        AnthropicSessionSpend(
            context.ref(id_, "session"), tuple(bucket.values()), runtime=None, usage_complete=False
        )
        for id_, bucket in by_session.items()
    )


async def run_live(
    admission: Admission,
    callback: Callable[[LiveContext], Awaitable[Mapping[str, object]]],
    *,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] = lambda: httpx.AsyncHTTPTransport(
        trust_env=False
    ),
    secret_loader: Callable[[Path], str] | None = None,
) -> Mapping[str, object]:
    """Exactly one admission/settlement, cleanup bounded inside the whole-run deadline."""
    refuse_ambient()
    marker = admission.ledger_path.parent / (".n4-live-attempt-" + admission.approval_digest)
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        raise LiveRefused("the signed envelope already has an attempt") from None
    guard = BudgetGuard(admission.config_path, admission.ledger_path)
    reservation = guard.reserve_with_hold(
        admission.plan,
        minimum_hold_usd=admission.hold,
        maximum_hold_usd=admission.hold,
        expected_config_sha256=HOST_CONFIG_SHA256,
        expected_ledger_id=LEDGER_ID,
    )
    started = time.monotonic()
    context: LiveContext | None = None
    status: Literal["completed", "failed", "cancelled"] = "failed"
    result: Mapping[str, object] = {"status": "failed"}
    receipt: SpendReceipt | None = None
    task: asyncio.Future[Mapping[str, object]] | None = None
    stop_task: asyncio.Task[bool] | None = None
    try:
        admission.output.mkdir(parents=True, exist_ok=False)
        path = Path(str(admission.approval["credential_file"]))
        key = (
            secret_loader(path)
            if secret_loader is not None
            else explicit_secret(path, env_file=True)
        )
        config = BudgetConfig.load(admission.config_path)
        provider = config.providers["anthropic"]
        price = provider.models[MODEL]
        runtime_price = next(p for p in provider.session_prices if p.meter == "agent_session")
        authorization = OwnedAuthorization(
            admission.binding.scope,
            frozenset(
                (ref.kind, ref.id)
                for ref in (admission.binding.environment, admission.binding.agent)
                if ref is not None
            ),
        )
        monitor = UsageMonitor(
            price, runtime_price, admission.hold * Decimal(".8"), started=started
        )
        transport = LiveTransport(
            transport_factory(),
            authorization,
            monitor,
            max_user_sends=2 if admission.package == "A7" else 1,
        )
        client = build_async_anthropic(
            transport=transport, api_key=key, max_retries=0, timeout=httpx.Timeout(10, read=30)
        )
        native = AnthropicManagedAgents(
            client, account_scope_id=admission.binding.account_scope_id, authorization=authorization
        )
        context = LiveContext(
            admission, client, native, transport, reservation, redaction_secrets=(key,)
        )
        try:
            # Reserve 15 seconds of the SAME deadline for interrupt/archive/close.
            task = asyncio.ensure_future(callback(context))
            remaining = max(
                0,
                admission.deadline_s
                - min(15, admission.deadline_s / 2)
                - (time.monotonic() - started),
            )
            stop_task = asyncio.create_task(monitor.stop.wait())
            done, _ = await asyncio.wait(
                (task, stop_task), timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if monitor.stop.is_set() or task not in done:
                context.transport.stopping = True
                task.cancel()
                await asyncio.wait(
                    (task,),
                    timeout=min(1, max(0, admission.deadline_s - (time.monotonic() - started)) / 4),
                )
                if monitor.stop_reason is None:
                    monitor.stop_reason = "whole-run deadline reached; cleanup margin reserved"
                    monitor.stop.set()
                raise LiveStopped(monitor.stop_reason)
            result = task.result()
            status = "completed" if result.get("status") == "pass" else "failed"
        except asyncio.CancelledError:
            context.transport.stopping = True
            if task is not None:
                task.cancel()
            status = "cancelled"
            raise
        except Exception as error:
            result = {
                "status": "failed",
                "reason": "native run stopped or failed; no retry",
                "failure_type": type(error).__name__,
            }
    finally:
        if stop_task is not None:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        if task is not None:

            def consume_done(future: asyncio.Future[Mapping[str, object]]) -> None:
                if not future.cancelled():
                    future.exception()

            task.add_done_callback(consume_done)
        try:
            if context is not None:
                remaining = max(0, admission.deadline_s - (time.monotonic() - started))
                async with asyncio.timeout(remaining):
                    try:
                        await context.stop_session()
                    finally:
                        try:
                            if context.retire is not None:
                                await context.retire()
                        finally:
                            await context.client.close()
        except Exception:
            status = "failed"
            result = {
                "status": "failed",
                "reason": "cleanup acknowledgement unavailable; hold retained",
            }
        finally:
            actual = None
            if context is not None:
                context.transport.closed = True
                try:
                    actual = actual_from_anthropic_sessions(
                        reservation, merged_usage(context), pricing_basis="standard-global"
                    )
                except Exception:
                    actual = None
            receipt = guard.settle(
                reservation, status=status, limits=admission.plan.limits, actual=actual
            )
            if admission.output.is_dir():
                facts: dict[str, object] = {
                    "source_head": admission.source_head,
                    "package": admission.package,
                    "model": MODEL,
                    "native_control_mode": "guard-only",
                    "native_budget_sent": False,
                    "provider_token_caps_enforced": False,
                    "observed_per_request_thresholds": [100000, 20000],
                    "wall_clock_deadline_seconds": admission.deadline_s,
                    "stop_threshold_usd": str(admission.hold * Decimal(".8")),
                    "result": result,
                    "spend": receipt.model_dump(mode="json"),
                }
                if context is not None:
                    facts["requests"] = context.transport.requests
                    facts["created_resources"] = sorted(context.transport.authorization.created)
                    facts["archived_sessions"] = sorted(context.transport.archived)
                    facts["observed_token_threshold_crossings"] = (
                        context.transport.monitor.observed_thresholds
                    )
                    facts["stream_stop_reason"] = context.transport.monitor.stop_reason
                    facts["stream_cost_estimate_usd"] = (
                        str(context.transport.monitor.cost)
                        if context.transport.monitor.cost is not None
                        else None
                    )
                    facts["monitor_cost_complete"] = False
                    facts["monitor_cost_basis"] = "known token lower bound plus wall-time estimate"
                    facts["observed_request_threshold_status"] = (
                        context.transport.monitor.threshold_receipts()
                    )
                    facts["runtime_measurement"] = (
                        "unknown; wall-clock monitoring is not billed-runtime proof"
                    )
                    facts["observed_request_meters"] = [
                        raw.get("model_usage")
                        for raw in context.transport.monitor.observations.values()
                    ]
                try:
                    Audit(context.redaction_secrets if context else ()).audit(
                        json.loads(json.dumps(facts))
                    )
                except RecordingError:
                    facts = {
                        "package": admission.package,
                        "model": MODEL,
                        "status": "failed",
                        "reason": "evidence export refused by credential audit",
                        "guard_settlement_persisted": True,
                        "native_budget_sent": False,
                    }
                (admission.output / "receipt.json").write_text(json.dumps(facts, indent=2) + "\n")
    return {"result": result, "spend": receipt.model_dump(mode="json")}


def add_live_arguments(parser: object) -> None:
    import argparse

    if not isinstance(parser, argparse.ArgumentParser):
        raise TypeError("expected an argument parser")
    parser.add_argument("--live-binding", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--go-receipt", type=Path)
    parser.add_argument("--go-sha256")
    parser.add_argument("--budget-config", type=Path)
    parser.add_argument("--spend-ledger", type=Path)


def live_command(package: Package, args: object) -> None:
    import argparse

    from daimon.testing.a7_anthropic import source_pin
    from daimon.testing.anthropic_live_cases import a7_pair, f2_default

    if not isinstance(args, argparse.Namespace):
        raise TypeError("expected command arguments")
    try:
        paths = (
            args.live_binding,
            args.authorization,
            args.go_receipt,
            args.go_sha256,
            args.budget_config,
            args.spend_ledger,
        )
        if any(item is None for item in paths):
            raise LiveRefused("signed installation, bindings and separate lead GO are required")
        source = source_pin()
        admission = admit(
            package=package,
            approval_path=args.authorization,
            go_path=args.go_receipt,
            go_digest=args.go_sha256,
            config_path=args.budget_config,
            ledger_path=args.spend_ledger,
            binding_path=args.live_binding,
            output=args.output,
            source_head=str(source["head"]),
            source_clean=source["clean"] is True,
        )
    except Exception:
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "receipt.json").write_text(
            json.dumps(
                {
                    "status": "PENDING",
                    "reason": "live admission prerequisites missing or stale",
                    "package": package,
                    "model": MODEL,
                    "native_control_mode": "guard-only",
                    "native_budget_sent": False,
                    "external_provider_calls": 0,
                    "credential_reads": 0,
                    "reserved_usd": "0",
                },
                indent=2,
            )
            + "\n"
        )
        return
    asyncio.run(run_live(admission, a7_pair if package == "A7" else f2_default))
