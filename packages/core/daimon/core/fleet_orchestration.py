"""Orchestrator operations over the fleet owners' admission and journal ports.

No tool handle is authority. Hosts supply freshly loaded worker permissions;
the resolver authorizes writes and the store atomically retains operations,
reservations and audit. Missing owner ports leave the service unavailable.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Protocol

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.security_audit import record_authz_denial
from mux.contracts.config import BackendConfig
from mux.contracts.events import AgentMessagePayload, Event, TextPart
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import Agent, AgentPatch, AgentSpec, SessionSpec
from pydantic import JsonValue

if TYPE_CHECKING:
    from daimon.core.fleet import FleetRun, FleetWorker
    from daimon.core.neutral_resources import ResolvedWorkerSpec, WorkerLimits, WorkerPermissions
    from daimon.core.pricing import ModelRates, ProviderPrice
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


type Control = Literal["kill", "resume"]


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate review field")
        result[key] = value
    return result


class FleetRefused(ValueError):
    """Closed refusal code, with no request, result or credential material."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"fleet: {reason}")


def _review_required(contract: Mapping[str, JsonValue] | None) -> bool:
    required = (contract or {}).get("review_required", False)
    if type(required) is not bool:
        raise FleetRefused("review_policy_invalid")
    return required


class FleetPorts(Protocol):
    """Store/budget/driver integration supplied by the owning host.

    Admission and control must commit operation identity, original ceilings,
    audit and reservation together. They repeat fresh authorization under the
    transaction before any native effect. Wait freezes the exact worker set and
    generation with the wake in that transaction. Result reads validate the
    worker's real binding slot before reading its journal. A missing integration
    never falls back to a provider client or a second state machine here.
    """

    async def read_run(
        self, scope: Scope, run_id: uuid.UUID
    ) -> tuple[FleetRun, tuple[FleetWorker, ...]]: ...

    async def read_clone(
        self, parent: WorkerPermissions, ref: ResourceRef, revision: Revision
    ) -> Agent: ...

    async def admit(
        self,
        parent: WorkerPermissions,
        resolved: ResolvedWorkerSpec,
        *,
        run_id: uuid.UUID,
        operation_key: str,
        role: str,
        session: SessionSpec | None,
        result_contract: Mapping[str, JsonValue] | None,
        reviewer_of: uuid.UUID | None,
        reviewed_result: Event | None,
    ) -> tuple[FleetRun, FleetWorker]: ...

    async def control(
        self,
        parent: WorkerPermissions,
        run: FleetRun,
        worker: FleetWorker | None,
        *,
        action: Control,
        operation_key: str,
        expected_version: int,
        resolved: tuple[ResolvedWorkerSpec, ...],
    ) -> Mapping[str, JsonValue]:
        """Return a scoped durable acknowledgement, not native termination.

        Explicit accepted=True binds run_id, worker_id, action, operation_key,
        expected_version and the committed next version. A pending/refused
        operation uses accepted=False. Arbitrary JSON is not acknowledgement.
        """
        ...

    async def retained_control(
        self,
        parent: WorkerPermissions,
        run: FleetRun,
        worker: FleetWorker | None,
        *,
        action: Control,
        operation_key: str,
        expected_version: int,
    ) -> Mapping[str, JsonValue] | None:
        """Read an existing scoped operation only when its original request matches."""
        ...

    async def wait(
        self, parent: WorkerPermissions, run: FleetRun, workers: tuple[FleetWorker, ...]
    ) -> None: ...

    async def read_result(
        self,
        parent: WorkerPermissions,
        policy: TenantAccessPolicy,
        run: FleetRun,
        worker: FleetWorker,
    ) -> Event: ...

    def validate_contract(
        self, contract: Mapping[str, JsonValue] | None, result: Event
    ) -> JsonValue:
        """Validate the stored contract, refusing unsupported or invalid formats.

        Implementations must not fetch remote schemas or trust the excerpt.
        Return the validated output, rather than the model's summary of it.
        """
        ...


def _authorize(parent: WorkerPermissions, policy: TenantAccessPolicy) -> None:
    from daimon.core.authz import Action, Surface, authorize

    facts = parent.fleet
    decision = authorize(
        policy,
        subject=parent.subject,
        action=Action.SPAWN_WORKER,
        surface=Surface.FLEET,
        agent=parent.agent,
        place=facts.place,
        fleet=facts,
    )
    if not decision:
        raise FleetRefused(decision.reason or "permission_refused")
    if parent.agent_id is None:
        raise FleetRefused("root_identity_unbound")


def _scope(parent: WorkerPermissions, run: FleetRun) -> None:
    facts = parent.fleet
    if (
        run.id != facts.run_id
        or str(run.tenant_id) != facts.scope.tenant_id
        or run.orchestrator_account_id != facts.account_id
        or run.platform != facts.root.channel.platform
        or run.channel_id != facts.root.channel.channel_id
        or run.thread_id != facts.root.thread_id
    ):
        raise FleetRefused("foreign_run")


def _session_authorize(
    parent: WorkerPermissions, policy: TenantAccessPolicy, *, continuing: bool = False
) -> None:
    from daimon.core.authz import Action, SessionFacts, Surface, authorize

    root = parent.fleet.root
    decision = authorize(
        policy,
        subject=parent.subject,
        action=Action.CONTINUE_SESSION if continuing else Action.READ_SESSION,
        surface=Surface.FLEET,
        agent=parent.agent,
        origin=parent.fleet.place,
        origin_channel_ids=frozenset({root.channel.channel_id, root.thread_id}),
        session=SessionFacts(
            channel=root.channel.channel_id,
            thread=root.thread_id,
            seal_ids=parent.seal_ids,
            owned=parent.fleet.scope.account_id == str(parent.fleet.account_id),
        ),
    )
    if not decision:
        raise FleetRefused(decision.reason or "permission_refused")


class FleetOrchestrator:
    """Shared operations for authenticated MCP and platform control adapters."""

    def __init__(self, ports: FleetPorts | None = None) -> None:
        self._ports = ports

    def _owner(self) -> FleetPorts:
        if self._ports is None:
            raise FleetRefused("prerequisites_unavailable")
        return self._ports

    @staticmethod
    def _controller_authorize(parent: WorkerPermissions) -> None:
        if parent.subject.platform_user_id != parent.fleet.scope.principal_id and (
            not parent.subject.is_admin or parent.subject.via_agent_key
        ):
            record_authz_denial("fleet_control", "controller_required")
            raise FleetRefused("controller_required")

    async def read_snapshot(
        self, parent: WorkerPermissions, policy: TenantAccessPolicy, run_id: uuid.UUID
    ) -> tuple[FleetRun, tuple[FleetWorker, ...]]:
        """Read canonical rows through the shared current-authority/membership fence."""
        _authorize(parent, policy)
        _session_authorize(parent, policy)
        if run_id != parent.fleet.run_id:
            raise FleetRefused("foreign_run")
        run, workers = await self._owner().read_run(parent.fleet.scope, run_id)
        _scope(parent, run)
        if any(worker.run_id != run.id or worker.tenant_id != run.tenant_id for worker in workers):
            raise FleetRefused("foreign_worker")
        if len({worker.id for worker in workers}) != len(workers):
            raise FleetRefused("ambiguous_workers")
        return run, workers

    async def spawn(
        self,
        parent: WorkerPermissions,
        policy: TenantAccessPolicy,
        *,
        now: datetime,
        operation_key: str,
        role: str,
        backend: BackendConfig,
        limits: WorkerLimits,
        run_id: uuid.UUID | None = None,
        agent: AgentSpec | None = None,
        clone: ResourceRef | None = None,
        clone_revision: Revision | None = None,
        overrides: AgentPatch | None = None,
        session: SessionSpec | None = None,
        result_contract: Mapping[str, JsonValue] | None = None,
        reviewer_of: uuid.UUID | None = None,
    ) -> Mapping[str, JsonValue]:
        """Resolve once, then hand admission to the atomic owner operation.

        Cross-review follows exactly this path with the target's durable result
        attached as input by the host. The model cannot supply review evidence.
        """
        _authorize(parent, policy)
        owner = self._owner()
        from daimon.core.neutral_resources import resolve_worker_spec

        selected_run = parent.fleet.run_id if run_id is None else run_id
        if selected_run != parent.fleet.run_id:
            raise FleetRefused("foreign_run")
        if (agent is None) == (clone is None):
            raise FleetRefused("spec_or_clone_required")
        if clone is None and (clone_revision is not None or overrides is not None):
            raise FleetRefused("clone_required")
        if not operation_key.strip() or not role.strip():
            raise FleetRefused("operation_required")
        if session is not None:
            # The host must construct worker-slot/session resources from the
            # resolved grant; arbitrary native refs are never request authority.
            raise FleetRefused("session_resources_unavailable")
        if reviewer_of is None:
            _review_required(result_contract)
        source: AgentSpec | Agent
        if clone is not None:
            if clone.kind != "agent" or clone_revision is None:
                raise FleetRefused("pinned_clone_required")
            if (clone.tenant_id, clone.account_id) != (
                parent.fleet.scope.tenant_id,
                parent.fleet.scope.account_id,
            ):
                raise FleetRefused("foreign_clone")
            source = await owner.read_clone(parent, clone, clone_revision)
            if source.ref != clone or source.revision != clone_revision:
                raise FleetRefused("stale_clone")
        else:
            assert agent is not None
            source = agent
        resolved = resolve_worker_spec(
            parent,
            source,
            policy=policy,
            now=now,
            backend=backend,
            overrides=overrides,
            limits=limits,
        )
        reviewed: Event | None = None
        if reviewer_of is not None:
            run, workers = await self.read_snapshot(parent, policy, selected_run)
            target = next((worker for worker in workers if worker.id == reviewer_of), None)
            if target is None or target.reviewer_of is not None:
                raise FleetRefused("review_target_required")
            reviewed = await self._result(parent, policy, run, target)
            result_contract = {
                "format": "review",
                "reviewed_digest": target.result.digest if target.result is not None else "",
            }
        run, worker = await owner.admit(
            parent,
            resolved,
            run_id=selected_run,
            operation_key=operation_key,
            role=role,
            session=None,
            result_contract=result_contract,
            reviewer_of=reviewer_of,
            reviewed_result=reviewed,
        )
        _scope(parent, run)
        if worker.run_id != run.id or worker.tenant_id != run.tenant_id:
            raise FleetRefused("foreign_worker")
        if (
            worker.spec_digest != resolved.digest
            or worker.spec != resolved.spec.model_dump(mode="json")
            or worker.backend != resolved.backend.backend
            or worker.profile != resolved.backend.profile
            or worker.model != resolved.spec.model.id
            or worker.budget_usd != resolved.limits.budget_usd
            or worker.deadline_at != resolved.limits.deadline
            or worker.max_turns != resolved.limits.max_turns
            or worker.reviewer_of != reviewer_of
            or worker.result_contract != result_contract
            or worker.id == reviewer_of
        ):
            raise FleetRefused("admitted_worker_mismatch")
        return {"run_id": str(run.id), "worker_id": str(worker.id), "status": worker.status}

    async def status(
        self, parent: WorkerPermissions, policy: TenantAccessPolicy, *, run_id: uuid.UUID
    ) -> Mapping[str, JsonValue]:
        run, workers = await self.read_snapshot(parent, policy, run_id)
        return {
            "run_id": str(run.id),
            "status": run.status,
            "version": run.version,
            "reserved_usd": str(run.reserved_usd),
            "observed_usd": str(run.observed_usd),
            "workers": [
                {
                    "worker_id": str(worker.id),
                    "role": worker.role,
                    "status": worker.status,
                    "version": worker.version,
                    "reviewer_of": str(worker.reviewer_of)
                    if worker.reviewer_of is not None
                    else None,
                }
                for worker in workers
            ],
        }

    async def control(
        self,
        parent: WorkerPermissions,
        policy: TenantAccessPolicy,
        *,
        run_id: uuid.UUID,
        worker_id: uuid.UUID | None,
        action: Control,
        expected_version: int,
        operation_key: str,
        now: datetime,
    ) -> Mapping[str, JsonValue]:
        if (
            action not in ("kill", "resume")
            or type(expected_version) is not int
            or expected_version < 1
        ):
            raise FleetRefused("invalid_control")
        if now.utcoffset() is None:
            raise FleetRefused("deadline_timezone_required")
        run, workers = await self.read_snapshot(parent, policy, run_id)
        _session_authorize(parent, policy, continuing=True)
        self._controller_authorize(parent)
        worker = next((row for row in workers if row.id == worker_id), None)
        if worker_id is not None and worker is None:
            raise FleetRefused("foreign_worker")
        if not operation_key.strip():
            raise FleetRefused("operation_required")
        retained = await self._owner().retained_control(
            parent,
            run,
            worker,
            action=action,
            operation_key=operation_key,
            expected_version=expected_version,
        )
        if retained is not None:
            return self._control_reply(
                retained, run, worker, action, operation_key, expected_version
            )
        target_version = worker.version if worker is not None else run.version
        if expected_version != target_version:
            raise FleetRefused("stale_version")
        if run.status in ("completed", "failed", "cancelled"):
            raise FleetRefused("terminal_run")
        if worker is not None and worker.status in (
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
            "budget_exceeded",
        ):
            raise FleetRefused("terminal_worker")
        if action == "resume" and (
            now >= run.deadline_at
            or (
                worker is not None
                and (now >= worker.deadline_at or worker.turns_used >= worker.max_turns)
            )
        ):
            raise FleetRefused("original_limit_exhausted")
        prepared: list[ResolvedWorkerSpec] = []
        if action == "resume":
            from daimon.core.neutral_resources import WorkerLimits, resolve_worker_spec

            candidates = (
                (worker,)
                if worker is not None
                else tuple(
                    row for row in workers if row.status in ("pending", "starting", "running")
                )
            )
            if not candidates:
                raise FleetRefused("terminal_run")
            for candidate in candidates:
                if now >= candidate.deadline_at or candidate.turns_used >= candidate.max_turns:
                    raise FleetRefused("original_limit_exhausted")
                choices = tuple(
                    grant
                    for grant in parent.backend_grants
                    if grant.backend == candidate.backend
                    and grant.profile == candidate.profile
                    and grant.model in (None, candidate.model)
                )
                if len(choices) != 1:
                    raise FleetRefused("backend_grant_unavailable")
                prepared.append(
                    resolve_worker_spec(
                        parent,
                        AgentSpec.model_validate(candidate.spec),
                        policy=policy,
                        now=now,
                        backend=BackendConfig.model_validate(choices[0].model_dump(mode="json")),
                        limits=WorkerLimits(
                            budget_usd=candidate.budget_usd,
                            deadline=candidate.deadline_at,
                            max_turns=candidate.max_turns,
                        ),
                    )
                )
        reply = await self._owner().control(
            parent,
            run,
            worker,
            action=action,
            expected_version=expected_version,
            operation_key=operation_key,
            resolved=tuple(prepared),
        )
        return self._control_reply(reply, run, worker, action, operation_key, expected_version)

    @staticmethod
    def _control_reply(
        reply: Mapping[str, JsonValue],
        run: FleetRun,
        worker: FleetWorker | None,
        action: Control,
        operation_key: str,
        expected_version: int,
    ) -> Mapping[str, JsonValue]:
        identifiers: dict[str, JsonValue] = {
            "run_id": str(run.id),
            "worker_id": str(worker.id) if worker is not None else None,
            "action": action,
            "operation_key": operation_key,
            "expected_version": expected_version,
        }
        if reply.get("accepted") is False:
            return identifiers | {"accepted": False, "status": "waiting"}
        version = reply.get("version")
        if (
            reply.get("accepted") is not True
            or any(reply.get(key) != value for key, value in identifiers.items())
            or type(version) is not int
            or version <= expected_version
        ):
            raise FleetRefused("control_evidence_invalid")
        return identifiers | {"accepted": True, "status": "accepted", "version": version}

    async def _result(
        self,
        parent: WorkerPermissions,
        policy: TenantAccessPolicy,
        run: FleetRun,
        worker: FleetWorker,
    ) -> Event:
        from daimon.core.fleet import result_digest

        pointer = worker.result
        if worker.status != "succeeded" or pointer is None or worker.binding_id is None:
            raise FleetRefused("result_unavailable")
        result = await self._owner().read_result(parent, policy, run, worker)
        if (
            result.session_id != pointer.session_id
            or result.native.provider != worker.backend
            or result.sequence != pointer.sequence
            or result_digest(dict(result.payload)) != pointer.digest
            or result.authority not in ("record", "reconciled")
            or result.type != "agent.message"
        ):
            raise FleetRefused("result_evidence_invalid")
        message = AgentMessagePayload.model_validate(result.payload)
        if not message.complete:
            raise FleetRefused("result_incomplete")
        return result

    async def collect(
        self, parent: WorkerPermissions, policy: TenantAccessPolicy, *, run_id: uuid.UUID
    ) -> Mapping[str, JsonValue]:
        """Return verified inputs for one summary, never a synthesized success.

        Quorum and wake readiness are scheduling facts. They cannot bypass an
        unfinished/failed worker, a refused review or an invalid result contract.
        """
        run, workers = await self.read_snapshot(parent, policy, run_id)
        self._controller_authorize(parent)
        if run.status in ("cancelled", "failed"):
            return {"run_id": str(run.id), "status": "blocked", "reason": "run_stopped"}
        if not workers:
            raise FleetRefused("empty_run")
        if any(worker.status in ("pending", "starting", "running") for worker in workers):
            await self._owner().wait(parent, run, workers)
            return {"run_id": str(run.id), "status": "waiting"}
        if any(worker.status != "succeeded" for worker in workers):
            return {"run_id": str(run.id), "status": "blocked", "reason": "worker_failed"}
        if any(
            worker.reviewer_of is None
            and _review_required(worker.result_contract)
            and not any(reviewer.reviewer_of == worker.id for reviewer in workers)
            for worker in workers
        ):
            return {"run_id": str(run.id), "status": "blocked", "reason": "review_required"}
        results: dict[uuid.UUID, Event] = {}
        for worker in workers:
            results[worker.id] = await self._result(parent, policy, run, worker)
        for worker in workers:
            if worker.reviewer_of is None:
                continue
            target = next((row for row in workers if row.id == worker.reviewer_of), None)
            if target is None or target.reviewer_of is not None or target.result is None:
                raise FleetRefused("review_target_required")
            contract = worker.result_contract
            if contract != {"format": "review", "reviewed_digest": target.result.digest}:
                raise FleetRefused("review_digest_mismatch")
            message = AgentMessagePayload.model_validate(results[worker.id].payload)
            if not message.content or any(
                not isinstance(part, TextPart) for part in message.content
            ):
                raise FleetRefused("review_evidence_invalid")
            text = "".join(part.text for part in message.content if isinstance(part, TextPart))
            try:
                verdict = json.loads(text, object_pairs_hook=_unique_object)
            except ValueError:
                raise FleetRefused("review_evidence_invalid") from None
            if not isinstance(verdict, dict) or verdict != {
                "verdict": "approved",
                "reviewed_digest": target.result.digest,
            }:
                return {"run_id": str(run.id), "status": "blocked", "reason": "review_refused"}
        outputs: list[JsonValue] = [
            {
                "worker_id": str(worker.id),
                "result": self._owner().validate_contract(
                    worker.result_contract, results[worker.id]
                ),
            }
            for worker in workers
            if worker.reviewer_of is None
        ]
        return {"run_id": str(run.id), "status": "ready", "results": outputs}


class HostFleetPorts:
    """Compose durable fleet, admission and full-result owners for the host.

    Native dispatch and completion/control hooks are installed by their existing
    host owners. An absent hook refuses before funding or native effects.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        prices: Mapping[tuple[str, str], ModelRates | ProviderPrice],
        markup: Decimal,
        dispatch: Callable[
            [WorkerPermissions, FleetWorker, ResolvedWorkerSpec, Event | None], Awaitable[None]
        ]
        | None = None,
        clone_reader: Callable[[WorkerPermissions, ResourceRef, Revision], Awaitable[Agent]]
        | None = None,
        wait: Callable[[WorkerPermissions, FleetRun, tuple[FleetWorker, ...]], Awaitable[None]]
        | None = None,
    ) -> None:
        self.sessions, self.prices, self.markup = sessions, prices, markup
        self._dispatch, self._clone_reader, self._wait = dispatch, clone_reader, wait

    async def read_run(
        self, scope: Scope, run_id: uuid.UUID
    ) -> tuple[FleetRun, tuple[FleetWorker, ...]]:
        from daimon.core.stores import fleet

        if scope.is_platform or scope.is_legacy_host_authorized:
            raise FleetRefused("root_scope_required")
        run, workers = await fleet.run_status(
            self.sessions, tenant_id=uuid.UUID(scope.tenant_id), run_id=run_id
        )
        if str(run.orchestrator_account_id) != scope.account_id:
            raise FleetRefused("foreign_run")
        return run, workers

    async def read_clone(
        self, parent: WorkerPermissions, ref: ResourceRef, revision: Revision
    ) -> Agent:
        if self._clone_reader is None:
            raise FleetRefused("prerequisites_unavailable")
        return await self._clone_reader(parent, ref, revision)

    async def admit(
        self,
        parent: WorkerPermissions,
        resolved: ResolvedWorkerSpec,
        *,
        run_id: uuid.UUID,
        operation_key: str,
        role: str,
        session: SessionSpec | None,
        result_contract: Mapping[str, JsonValue] | None,
        reviewer_of: uuid.UUID | None,
        reviewed_result: Event | None,
    ) -> tuple[FleetRun, FleetWorker]:
        from daimon.core.channel_budget import admit_fleet_worker
        from daimon.core.stores import access_policy, fleet, mux_state
        from mux.state.operations import request_digest

        if self._dispatch is None or session is not None or run_id != parent.fleet.run_id:
            raise FleetRefused("prerequisites_unavailable")
        if (reviewer_of is None) != (reviewed_result is None):
            raise FleetRefused("review_evidence_invalid")
        scope = parent.fleet.scope
        key = f"fleet:{run_id}:admit:{operation_key}"
        digest = request_digest(
            {
                "resolved": resolved.digest,
                "role": role,
                "result_contract": dict(result_contract) if result_contract is not None else None,
                "reviewer_of": str(reviewer_of) if reviewer_of is not None else None,
                "reviewed_result": reviewed_result.model_dump(mode="json")
                if reviewed_result is not None
                else None,
            }
        )
        async with self.sessions.begin() as db:
            policy = await access_policy.load_access_policy(
                db, tenant_id=uuid.UUID(scope.tenant_id)
            )
            _authorize(parent, policy)
            await fleet.lock_budget(
                db,
                tenant_id=uuid.UUID(scope.tenant_id),
                actor_id=parent.fleet.account_id,
                run_id=run_id,
            )
            run, workers = await fleet.run_status(
                db, tenant_id=uuid.UUID(scope.tenant_id), run_id=run_id
            )
            _scope(parent, run)
            begun = await mux_state.begin_operation(
                db,
                scope,
                key=key,
                request_digest=digest,
                operation_id=key,
                now=await mux_state.database_clock(db),
            )
            if not begun.fresh:
                worker = next(
                    (row for row in workers if str(row.id) == begun.record.result.get("worker_id")),
                    None,
                )
                if begun.record.operation.status != "processed" or worker is None:
                    raise FleetRefused("admission_evidence_unavailable")
                return run, worker
            usage = await admit_fleet_worker(
                db,
                parent=parent,
                policy=policy,
                resolved=resolved,
                ordinal=len(workers),
                role=role,
                prices=self.prices,
                markup=self.markup,
                reviewer_of=reviewer_of,
                result_contract=result_contract,
            )
            await mux_state.claim_send(
                db,
                scope,
                key,
                now=await mux_state.database_clock(db),
                clock=mux_state.database_clock,
                fence=None,
            )
            await mux_state.advance_operation(
                db,
                scope,
                key,
                "processed",
                now=await mux_state.database_clock(db),
                clock=mux_state.database_clock,
                fence=None,
                result={"worker_id": str(usage.worker.id)},
            )
        if not usage.admitted:
            raise FleetRefused("financial_admission_refused")
        await self._dispatch(parent, usage.worker, resolved, reviewed_result)
        return usage.run, usage.worker

    async def read_result(
        self,
        parent: WorkerPermissions,
        policy: TenantAccessPolicy,
        run: FleetRun,
        worker: FleetWorker,
    ) -> Event:
        from daimon.core.fleet import check_result, worker_slot
        from daimon.core.stores import access_policy, fleet, mux_state

        _scope(parent, run)
        async with self.sessions.begin() as db:
            fresh = await access_policy.load_access_policy(db, tenant_id=run.tenant_id)
            _authorize(parent, fresh)
            _session_authorize(parent, fresh)
            actual_run, workers = await fleet.run_status(db, tenant_id=run.tenant_id, run_id=run.id)
            _scope(parent, actual_run)
            actual = next((row for row in workers if row.id == worker.id), None)
            if (
                actual is None
                or actual != worker
                or actual.result is None
                or actual.status != "succeeded"
            ):
                raise FleetRefused("result_unavailable")
            binding = await mux_state.get_binding(db, worker_slot(actual_run, actual))
            records = await mux_state.read_events(
                db, actual.result.session_id, after=actual.result.sequence - 1, limit=1
            )
            result = records[0] if records else None
            check_result(actual_run, actual, binding, result, actual.result)
            assert result is not None
            return result

    def validate_contract(
        self, contract: Mapping[str, JsonValue] | None, result: Event
    ) -> JsonValue:
        contract = contract or {}
        format_ = contract.get("format", "text")
        if set(contract) - {"format", "review_required"} or format_ not in ("text", "json"):
            raise FleetRefused("result_contract_unsupported")
        _review_required(contract)
        message = AgentMessagePayload.model_validate(result.payload)
        if (
            not message.complete
            or not message.content
            or any(not isinstance(part, TextPart) for part in message.content)
        ):
            raise FleetRefused("result_contract_invalid")
        text = "".join(part.text for part in message.content if isinstance(part, TextPart))
        if format_ == "text":
            return text
        try:
            value = json.loads(text, object_pairs_hook=_unique_object)
            json.dumps(value, allow_nan=False)
            return value
        except ValueError:
            raise FleetRefused("result_contract_invalid") from None

    async def wait(
        self, parent: WorkerPermissions, run: FleetRun, workers: tuple[FleetWorker, ...]
    ) -> None:
        if self._wait is None:
            raise FleetRefused("prerequisites_unavailable")
        await self._wait(parent, run, workers)

    async def retained_control(
        self,
        parent: WorkerPermissions,
        run: FleetRun,
        worker: FleetWorker | None,
        *,
        action: Control,
        operation_key: str,
        expected_version: int,
    ) -> Mapping[str, JsonValue] | None:
        raise FleetRefused("prerequisites_unavailable")

    async def control(
        self,
        parent: WorkerPermissions,
        run: FleetRun,
        worker: FleetWorker | None,
        *,
        action: Control,
        operation_key: str,
        expected_version: int,
        resolved: tuple[ResolvedWorkerSpec, ...],
    ) -> Mapping[str, JsonValue]:
        raise FleetRefused("prerequisites_unavailable")
