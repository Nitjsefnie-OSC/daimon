"""Answer the tool confirmations of agent-chat turns, which nobody watches.

Only the process that sent a turn answers its pauses: `watch_turn` starts a
bounded worker for that one turn boundary, right after an authorized
`start_turn` or `continue_turn` send. Each pause is decided at call time by
`unattended_posture` from the current policy. Nothing else answers one. A
read (`turn_status`) only reports; after a restart the session stays paused
and gated until its owner calls `continue_turn`, which re-gates it and
starts a worker on the paused turn (`resume_paused`).
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from typing import Literal

import structlog
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.sessions import BetaManagedAgentsSessionEvent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.resource_ports import (
    list_session_events,
    mcp_scope,
    retrieve_session,
)
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._session_access import hub_access_mode
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT
from daimon.core.stores import session_settle_state
from daimon.core.turn.approvals import (
    AnnotationLookup,
    build_decision_events,
    pending_confirmation_ids,
    tool_calls_for,
)
from daimon.core.turn.ceiling import TURN_CEILING_S
from daimon.core.turn.io import LegacyTurnIO
from daimon.core.turn.reducers import apply
from daimon.core.turn.state import TurnState
from daimon.core.turn.unattended import gate_session_snapshot, unattended_posture
from fastmcp.exceptions import ToolError

log = structlog.get_logger(__name__)

PAUSED_HINT = "paused: call continue_turn to resume"
"""What a read reports for a turn waiting on confirmations nobody is answering."""

# Newest-first pages read back to a turn's boundary before giving up.
_MAX_TURN_PAGES = 20
_POLL_S = 1.0

_workers: dict[tuple[int, str], asyncio.Task[bool]] = {}


def require_session_driver(session: BetaManagedAgentsSession, auth: AuthIdentity) -> None:
    """Refuse anyone but the session's own account, outside a hub read.

    `_verify_agent_owns_session` already holds this for a continue; it is
    checked again where pauses get answered, so a read grant (an admin's hub
    read of a channel session) can never reach that code.
    """
    if (session.metadata or {}).get(MA_METADATA_KEY_ACCOUNT) != str(
        auth.account_id
    ) or hub_access_mode() == "read":
        raise ToolError("session not found")


async def gate(runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession) -> None:
    """Rewrite `session`'s tool config from the current policy before a send."""
    await gate_session_snapshot(
        runtime.client,
        session,
        runtime.settings.tool_safety,
        scope=mcp_scope(auth),
        public_url=_public_url(runtime),
        session_factory=runtime.session_factory,
        tenant_id=auth.tenant_id,
    )


def _public_url(runtime: McpRuntime) -> str | None:
    url = runtime.settings.mcp.public_url
    return str(url) if url is not None else None


def _annotations_for(
    runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession
) -> AnnotationLookup | None:
    """Operator-trusted MCP hints for `session`'s calls; none on this build."""
    del runtime, auth, session
    return None


@dataclass
class _Turn:
    boundary_id: str | None = None
    state: TurnState = field(default_factory=TurnState)
    confirmed: set[str] = field(default_factory=set[str])


async def _read_turn(runtime: McpRuntime, auth: AuthIdentity, handle: str) -> _Turn:
    """The newest turn, folded oldest-first from its `user.message` boundary.

    Reads newest-first back to the boundary, so a previous turn's end can
    never hide the current pause. Raises past the page ceiling: no evidence
    is no answer.
    """
    events: list[BetaManagedAgentsSessionEvent] = []
    turn = _Turn()
    page: str | None = None
    for _ in range(_MAX_TURN_PAGES):
        query: dict[str, object] = {"order": "desc", "limit": 100}
        if page is not None:
            query["page"] = page
        result = await list_session_events(
            runtime.client, handle, query=query, scope=mcp_scope(auth)
        )
        for event in result.data:
            if event.type == "user.message":
                turn.boundary_id = event.id
                break
            events.append(event)
        if turn.boundary_id is not None or result.next_page is None:
            break
        page = result.next_page
    else:
        raise RuntimeError("MCP turn history exceeds the confirmation page limit")
    for event in reversed(events):
        turn.state = apply(turn.state, event)
        if event.type == "user.tool_confirmation":
            turn.confirmed.add(event.tool_use_id)
    return turn


def watch_turn(
    runtime: McpRuntime, auth: AuthIdentity, handle: str, *, turn_event_id: str
) -> asyncio.Task[bool]:
    """Answer the pauses of the turn that starts at `turn_event_id`, and no other.

    One worker per handle: a newer turn's worker replaces the older one. The
    task runs in a fresh context, outside the request that started it.
    """
    key = (id(runtime), handle)
    if (previous := _workers.get(key)) is not None:
        previous.cancel()
    task = asyncio.create_task(
        _watch(runtime, auth, handle, turn_event_id=turn_event_id), context=contextvars.Context()
    )
    _workers[key] = task

    def finished(done: asyncio.Task[bool]) -> None:
        if _workers.get(key) is done:
            _workers.pop(key, None)

    task.add_done_callback(finished)
    return task


async def _watch(
    runtime: McpRuntime, auth: AuthIdentity, handle: str, *, turn_event_id: str
) -> bool:
    """True once the turn ended; False if it was left paused (it stays gated)."""
    answered: set[str] = set()
    try:
        async with asyncio.timeout(TURN_CEILING_S):
            while True:
                session = await retrieve_session(runtime.client, handle, scope=mcp_scope(auth))
                if session.status == "terminated" or session.archived_at is not None:
                    return True
                if session.status == "idle":
                    turn = await _read_turn(runtime, auth, handle)
                    if turn.boundary_id != turn_event_id:
                        # Not listed yet, or a newer turn whose sender answers it.
                        await asyncio.sleep(_POLL_S)
                        continue
                    stop = turn.state.stop_reason
                    ids = pending_confirmation_ids(stop, confirmed=turn.confirmed | answered)
                    if ids:
                        await _answer(runtime, auth, session, turn, ids)
                        answered.update(ids)
                    elif stop is not None and stop.type != "requires_action":
                        if answered:
                            await _keep_settle_request(runtime, handle, turn_event_id)
                        return True
                await asyncio.sleep(_POLL_S)
    except Exception:
        # The session stays gated on any failure; no failure path allows a call.
        log.exception("mcp.agent_chat.confirmation_worker_failed", session_id=handle)
        return False


async def _answer(
    runtime: McpRuntime,
    auth: AuthIdentity,
    session: BetaManagedAgentsSession,
    turn: _Turn,
    ids: list[str],
) -> None:
    posture = unattended_posture(
        runtime.settings.tool_safety,
        session=session,
        public_url=_public_url(runtime),
        session_factory=runtime.session_factory,
        tenant_id=auth.tenant_id,
        annotations_for=_annotations_for(runtime, auth, session),
    )
    decisions = [
        (call.tool_use_id, await posture.decide(call)) for call in tool_calls_for(turn.state, ids)
    ]
    # The session port carries only messages and interrupts; confirmations go
    # through the turn transport the routine driver answers with.
    await LegacyTurnIO(runtime.client, session.id, scope=mcp_scope(auth)).send(
        build_decision_events(decisions)
    )


async def _keep_settle_request(runtime: McpRuntime, handle: str, turn_event_id: str) -> None:
    """Leave a settle request for a turn that ran on after a pause.

    A read or the drain may settle at a pause (an idle after the boundary)
    and clear the request; the calls made after it are billed by this one.
    A request already pending, this turn's or a newer send's, is left alone.
    """
    try:
        async with runtime.session_factory.begin() as db:
            if await session_settle_state.settle_request(db, managed_session_id=handle) is not None:
                return
            _, requested_at = await session_settle_state.request_settle(
                db, managed_session_id=handle
            )
            await session_settle_state.name_settle_turn(
                db,
                managed_session_id=handle,
                requested_at=requested_at,
                turn_event_id=turn_event_id,
            )
    except Exception:
        # Named boundary: the turn has ended; billing it never reopens it.
        log.exception("billing.settle_request_failed", managed_session_id=handle)


async def _paused(
    runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession
) -> bool:
    if session.status != "idle":
        return False
    result = await list_session_events(
        runtime.client,
        session.id,
        scope=mcp_scope(auth),
        query={"order": "desc", "limit": 1, "types": ["session.status_idle"]},
    )
    event = result.data[0] if result.data else None
    return (
        event is not None
        and event.type == "session.status_idle"
        and event.stop_reason.type == "requires_action"
    )


async def turn_status(
    runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession
) -> Literal["running", "paused"] | None:
    """What a read reports for a turn paused on confirmations; never answers one.

    `running` while this process's worker is answering it, `paused` when
    nobody is; None for any session not paused, which reads as MA reports it.
    """
    if not await _paused(runtime, auth, session):
        return None
    worker = _workers.get((id(runtime), session.id))
    return "running" if worker is not None and not worker.done() else "paused"


@dataclass(frozen=True)
class Resumed:
    worker: asyncio.Task[bool]
    turn_event_id: str


async def resume_paused(
    runtime: McpRuntime, auth: AuthIdentity, session: BetaManagedAgentsSession
) -> Resumed | None:
    """Answer `session`'s paused turn from the current policy; None if not paused.

    Only for the owner's `continue_turn`, after `gate`, inside its fence.
    """
    require_session_driver(session, auth)
    if not await _paused(runtime, auth, session):
        return None
    turn = await _read_turn(runtime, auth, session.id)
    if turn.boundary_id is None:
        raise ToolError("This session's paused turn could not be read; try again.")
    worker = _workers.get((id(runtime), session.id))
    if worker is None or worker.done():
        worker = watch_turn(runtime, auth, session.id, turn_event_id=turn.boundary_id)
    return Resumed(worker=worker, turn_event_id=turn.boundary_id)
