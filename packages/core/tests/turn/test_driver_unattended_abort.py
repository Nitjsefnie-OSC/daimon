"""An unattended run's abort at a pending confirmation: one owned interrupt.

Every abort signal, live and on replay, with and without a refresh gate,
with one cancellation or several, and with a cancellation held at each await
in the cleanup: the run sends no confirmation (a denial resumes execution
too), attempts exactly one interrupt, and leaves no driver task behind.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

import pytest
from anthropic import AsyncAnthropic
from daimon.core.tool_safety import ToolCall
from daimon.core.turn import driver, run_turn
from daimon.core.turn.posture import BillingExempt, PolicyApproval, ToolConfirmationResult
from daimon.core.turn.state import TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.testing.turn_fakes import (
    BlockForever,
    FakeAnthropic,
    RecordingLifecycle,
    StreamAction,
    YieldEvent,
)

from .conftest import (
    make_agent_message,
    make_end_turn,
    make_mcp_tool_use,
    make_requires_action,
    make_status_idle,
)

_EXEMPT = BillingExempt(reason="cli-operator-run")

#: Signals while the decision is pending, and the cleanup awaits they reach.
_DECISION_SIGNALS = ("stop", "cancel", "ceiling", "decision_failure")
#: Signals once the decision is made, before its batch is sent.
_SEND_SIGNALS = ("stop_after_decision", "gate_stop", "gate_cancel", "gate_ceiling", "gate_failure")
_GATE_ONLY = frozenset({"gate_stop", "gate_cancel", "gate_ceiling", "gate_failure"})
_HOLDS = ("decide_task_join", "retire_unsent_confirmations", "refusal", "stream_close", "interrupt")


def _applies(signal: str, gated: bool, replay: bool, hold: str | None) -> bool:
    if signal in _GATE_ONLY and not gated:
        return False
    if hold == "decide_task_join" and signal not in _DECISION_SIGNALS:
        return False
    if hold == "refusal" and signal == "gate_failure":
        return False  # a failed gate sends nothing, refusals included
    # The replay path decides with no stream open.
    return not (hold == "stream_close" and replay)


def _cases() -> list[Any]:
    cases: list[Any] = []
    for replay in (False, True):
        for gated in (False, True):
            for signal in (*_DECISION_SIGNALS, *_SEND_SIGNALS):
                for hold in (None, *_HOLDS):
                    for cancels in (0, 1, 2) if hold is not None else (0, 2):
                        if signal == "decision_failure" and hold is None and cancels:
                            continue
                        if not _applies(signal, gated, replay, hold):
                            continue
                        case_id = "-".join(
                            [
                                "replay" if replay else "live",
                                "gated" if gated else "ungated",
                                signal,
                                f"hold_{hold}" if hold else "nohold",
                                f"cancels_{cancels}",
                            ]
                        )
                        cases.append(pytest.param(replay, gated, signal, hold, cancels, id=case_id))
    return cases


def _script(fa: FakeAnthropic, *, replay: bool, ids: tuple[str, ...] = ("tu_write",)) -> None:
    uses = [
        make_mcp_tool_use(event_id=tool_use_id, name="create_issue", mcp_server_name="linear")
        for tool_use_id in ids
    ]
    pause = make_status_idle(
        event_id="sevt_pause", stop_reason=make_requires_action(event_ids=list(ids))
    )
    ack: list[StreamAction] = [
        YieldEvent(make_status_idle(event_id="interrupt_ack", stop_reason=make_end_turn()))
    ]
    if replay:
        # The pause is lost to a clean close and only found in the replay.
        fa.beta.sessions.events.stream_scripts = [[YieldEvent(use) for use in uses], ack]
        fa.beta.sessions.events.replay_events = [*uses, pause]
        fa.beta.sessions.retrieve_statuses = ["idle"]
        return
    fa.beta.sessions.events.stream_scripts = [
        [
            *(YieldEvent(use) for use in uses),
            YieldEvent(pause),
            YieldEvent(make_agent_message(event_id="sevt_msg", text="done")),
            YieldEvent(make_status_idle(event_id="sevt_end", stop_reason=make_end_turn())),
        ],
        ack,
    ]


def _sent(fa: FakeAnthropic, kind: str) -> list[dict[str, object]]:
    return [
        event
        for _session, batch in fa.beta.sessions.events.sent_events
        for event in batch
        if event["type"] == kind
    ]


@pytest.mark.parametrize("replay,gated,signal,hold,cancels", _cases())
async def test_unattended_abort_sends_one_interrupt_and_no_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    replay: bool,
    gated: bool,
    signal: str,
    hold: str | None,
    cancels: int,
) -> None:
    fa = FakeAnthropic()
    _script(fa, replay=replay)
    cancel = asyncio.Event()
    signalled = asyncio.Event()  # the abort point is reached
    armed = False  # the hold applies only to cleanup after the signal
    held = asyncio.Event()
    release = asyncio.Event()

    def reached() -> None:
        nonlocal armed
        armed = hold is not None
        signalled.set()

    async def hold_here() -> None:
        nonlocal armed
        armed = False
        held.set()
        await release.wait()

    original_bounded = driver._bounded  # pyright: ignore[reportPrivateUsage]

    async def bounded(task: asyncio.Task[Any], *, what: str, session_id: str) -> None:
        if signal == "stop_after_decision" and what == "decide_task_join" and not cancel.is_set():
            # Stop lands after the decision race, while its children are joined.
            cancel.set()
            reached()
        if armed and (what == hold or (hold == "refusal" and what.endswith("_refusal"))):
            await hold_here()
        await original_bounded(task, what=what, session_id=session_id)

    monkeypatch.setattr(driver, "_bounded", bounded)

    real_send = fa.beta.sessions.events.send

    async def send(session_id: str, *, events: list[dict[str, Any]]) -> None:
        await real_send(session_id, events=events)
        if armed and hold == "interrupt" and any(e["type"] == "user.interrupt" for e in events):
            await hold_here()

    fa.beta.sessions.events.send = send  # type: ignore[method-assign]

    real_stream = fa.beta.sessions.events.stream

    async def stream(**kwargs: Any) -> Any:
        opened = await real_stream(**kwargs)
        if fa.beta.sessions.events.stream_calls == 1:
            real_close: Callable[[], Awaitable[None]] = opened.close

            async def close() -> None:
                if armed and hold == "stream_close":
                    await hold_here()
                await real_close()

            opened.close = close  # type: ignore[method-assign]
        return opened

    fa.beta.sessions.events.stream = stream  # type: ignore[method-assign]

    gate_calls = 0

    async def gate() -> None:
        nonlocal gate_calls
        gate_calls += 1
        if gate_calls == 1 or signal not in _GATE_ONLY:
            return  # the turn's message, or a gate this abort does not use
        reached()
        if signal == "gate_failure":
            raise RuntimeError("gate failed")
        await asyncio.Event().wait()

    async def decide(call: ToolCall) -> ToolConfirmationResult:
        if signal in _DECISION_SIGNALS:
            reached()
            if signal == "decision_failure":
                raise ValueError("decider failed")
            await asyncio.Event().wait()
        # Allowed: an abort that let the batch out would show here.
        return ToolConfirmationResult(allow=True)

    async def run() -> TurnState:
        with driver.unattended_sends(gate if gated else None):
            return await run_turn(
                anthropic=cast(AsyncAnthropic, fa),
                session_id="sess_1",
                user_message="nightly",
                lifecycle=RecordingLifecycle(),
                cancel=cancel,
                render_interval_s=0.001,
                interrupt_timeout_s=5,
                billing=_EXEMPT,
                tool_confirmation=PolicyApproval(decide=decide),
                deadline=datetime.now(UTC) + timedelta(seconds=0.3)
                if signal in ("ceiling", "gate_ceiling")
                else None,
            )

    turn = asyncio.create_task(run())
    await asyncio.wait_for(signalled.wait(), timeout=2)
    if signal in ("stop", "gate_stop"):
        cancel.set()
    elif signal in ("cancel", "gate_cancel"):
        turn.cancel()
    if hold is not None:
        await asyncio.wait_for(held.wait(), timeout=2)
        assert not turn.done()
    for _ in range(cancels):
        turn.cancel()
        await asyncio.sleep(0)
    release.set()
    [outcome] = await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=10)

    if cancels or signal in ("cancel", "gate_cancel"):
        assert isinstance(outcome, asyncio.CancelledError)
    elif signal in ("ceiling", "gate_ceiling"):
        assert isinstance(outcome, TurnState)
        assert outcome.error is not None and outcome.error.kind == "ceiling"
    elif signal == "gate_failure":
        assert isinstance(outcome, RuntimeError)
    elif signal == "decision_failure":
        assert isinstance(outcome, ValueError)
    else:
        assert isinstance(outcome, TurnState)
        assert outcome.termination == TerminationReason.INTERRUPTED
    assert _sent(fa, "user.tool_confirmation") == [], "an unattended abort answers nothing"
    assert len(_sent(fa, "user.interrupt")) == 1, "exactly one interrupt attempt"
    # `_pump` cancels its render loop without draining it on a cancellation;
    # that task reaps within one loop turn.
    await asyncio.sleep(0)
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("turn.")
        and not task.done()
    ], "the abort and its cleanup are joined"


@pytest.mark.parametrize("replay", [False, True], ids=["live", "replay"])
@pytest.mark.parametrize("gated", [False, True], ids=["ungated", "gated"])
async def test_unattended_normal_path_is_unchanged(replay: bool, gated: bool) -> None:
    """No abort: the decided batch goes out and no interrupt is sent."""
    from daimon.core.turn.unattended import refuse_ungated_confirmation

    fa = FakeAnthropic()
    _script(fa, replay=replay)

    async def gate() -> None:
        return None

    async def allow(call: ToolCall) -> ToolConfirmationResult:
        return ToolConfirmationResult(allow=True)

    decide = allow if gated else refuse_ungated_confirmation
    with driver.unattended_sends(gate if gated else None):
        final = await run_turn(
            anthropic=cast(AsyncAnthropic, fa),
            session_id="sess_1",
            user_message="nightly",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=_EXEMPT,
            tool_confirmation=PolicyApproval(decide=decide),
        )
    assert final.error is None
    [confirmation] = _sent(fa, "user.tool_confirmation")
    if gated:
        assert confirmation["result"] == "allow"
    else:
        # The normal-path refusal on runtimes without session reads.
        assert confirmation["result"] == "deny"
        assert "cannot refresh" in str(confirmation["deny_message"])
    assert _sent(fa, "user.interrupt") == []


@pytest.mark.parametrize("replay", [False, True], ids=["live", "replay"])
@pytest.mark.parametrize("signal", ["stop", "cancel"])
async def test_attended_abort_still_refuses_pending_calls(replay: bool, signal: str) -> None:
    fa = FakeAnthropic()
    _script(fa, replay=replay)
    cancel = asyncio.Event()
    entered = asyncio.Event()

    async def decide(call: ToolCall) -> ToolConfirmationResult:
        entered.set()
        await asyncio.Event().wait()
        return ToolConfirmationResult(allow=True)

    turn = asyncio.create_task(
        run_turn(
            anthropic=cast(AsyncAnthropic, fa),
            session_id="sess_1",
            user_message="file a bug",
            lifecycle=RecordingLifecycle(),
            cancel=cancel,
            render_interval_s=0.001,
            interrupt_timeout_s=5,
            billing=_EXEMPT,
            tool_confirmation=PolicyApproval(decide=decide),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    if signal == "stop":
        cancel.set()
        final = await asyncio.wait_for(turn, timeout=5)
        assert final.termination == TerminationReason.INTERRUPTED
        assert len(_sent(fa, "user.interrupt")) == 1
    else:
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        assert _sent(fa, "user.interrupt") == []
    [refusal] = _sent(fa, "user.tool_confirmation")
    assert refusal["result"] == "deny"


def _live_turn_tasks() -> list[asyncio.Task[Any]]:
    return [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("turn.")
        and not task.done()
    ]


async def _until_sent(fa: FakeAnthropic, kind: str) -> None:
    async def poll() -> None:
        while not _sent(fa, kind):
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=2)


async def _settled(turn: asyncio.Task[TurnState]) -> object:
    [outcome] = await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=10)
    # `_pump` cancels its render loop without draining it on a cancellation.
    await asyncio.sleep(0)
    return outcome


def _run(
    fa: FakeAnthropic,
    *,
    gate: Callable[[], Awaitable[None]] | None,
    decide: Callable[[ToolCall], Awaitable[ToolConfirmationResult]],
    cancel: asyncio.Event,
    lifecycle: RecordingLifecycle | None = None,
    interrupt_timeout_s: float = 5,
    deadline_s: float | None = None,
) -> asyncio.Task[TurnState]:
    async def run() -> TurnState:
        with driver.unattended_sends(gate):
            return await run_turn(
                anthropic=cast(AsyncAnthropic, fa),
                session_id="sess_1",
                user_message="nightly",
                lifecycle=lifecycle or RecordingLifecycle(),
                cancel=cancel,
                render_interval_s=0.001,
                interrupt_timeout_s=interrupt_timeout_s,
                billing=_EXEMPT,
                tool_confirmation=PolicyApproval(decide=decide),
                deadline=None
                if deadline_s is None
                else datetime.now(UTC) + timedelta(seconds=deadline_s),
            )

    return asyncio.create_task(run())


async def _allow(call: ToolCall) -> ToolConfirmationResult:
    return ToolConfirmationResult(allow=True)


class _CancelOnDrain(asyncio.Event):
    """A Stop event whose gate waiter cancels the turn while it is drained."""

    def __init__(self, cancels: int) -> None:
        super().__init__()
        self.cancels = cancels
        self.turn: asyncio.Task[TurnState] | None = None
        self.fired = False

    async def wait(self) -> Literal[True]:
        try:
            return await super().wait()
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.get_name() == "turn.gate_cancel_waiter":
                assert self.turn is not None
                self.fired = True
                for _ in range(self.cancels):
                    self.turn.cancel()
            raise


@pytest.mark.parametrize("replay", [False, True], ids=["live", "replay"])
@pytest.mark.parametrize("cancels", [1, 2])
async def test_cancel_while_a_passed_gate_drains_its_waiter_sends_nothing(
    replay: bool, cancels: int
) -> None:
    """Review s1-abort, finding 1: the drain swallowed the turn's own cancel."""
    fa = FakeAnthropic()
    _script(fa, replay=replay)
    cancel = _CancelOnDrain(cancels)

    async def gate() -> None:
        return None

    turn = _run(fa, gate=gate, decide=_allow, cancel=cancel)
    cancel.turn = turn
    outcome = await _settled(turn)

    assert cancel.fired, "the cancel landed on the gate waiter's drain"
    assert isinstance(outcome, asyncio.CancelledError)
    assert _sent(fa, "user.tool_confirmation") == [], "the allow must not go out"
    assert len(_sent(fa, "user.interrupt")) == 1
    assert not _live_turn_tasks()


@pytest.mark.parametrize("gated", [False, True], ids=["ungated", "gated"])
@pytest.mark.parametrize("cancels", [0, 1, 2])
async def test_ordinary_stop_interrupts_before_the_stream_closes(gated: bool, cancels: int) -> None:
    """Finding 2: Stop with no call pending, then cancels during the close."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_agent_message(event_id="sevt_msg", text="working")), BlockForever()],
        [YieldEvent(make_status_idle(event_id="interrupt_ack", stop_reason=make_end_turn()))],
    ]
    cancel = asyncio.Event()
    lifecycle = RecordingLifecycle()
    closing = asyncio.Event()
    release = asyncio.Event()
    real_stream = fa.beta.sessions.events.stream

    async def stream(**kwargs: Any) -> Any:
        opened = await real_stream(**kwargs)
        if fa.beta.sessions.events.stream_calls == 1:
            real_close = opened.close

            async def close() -> None:
                closing.set()
                await release.wait()
                await real_close()

            opened.close = close  # type: ignore[method-assign]
        return opened

    fa.beta.sessions.events.stream = stream  # type: ignore[method-assign]

    async def gate() -> None:
        return None

    turn = _run(fa, gate=gate if gated else None, decide=_allow, cancel=cancel, lifecycle=lifecycle)

    async def streaming() -> None:
        while not lifecycle.sse_events:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(streaming(), timeout=2)
    cancel.set()
    await asyncio.wait_for(closing.wait(), timeout=2)
    await _until_sent(fa, "user.interrupt")  # before the close is released
    for _ in range(cancels):
        turn.cancel()
        await asyncio.sleep(0)
    release.set()
    outcome = await _settled(turn)

    if cancels:
        assert isinstance(outcome, asyncio.CancelledError)
    else:
        assert isinstance(outcome, TurnState)
        assert outcome.termination == TerminationReason.INTERRUPTED
    assert len(_sent(fa, "user.interrupt")) == 1
    assert not _live_turn_tasks()


@pytest.mark.parametrize("gated", [False, True], ids=["ungated", "gated"])
@pytest.mark.parametrize("signal", ["cancel", "ceiling"])
async def test_abort_while_the_pause_is_delivered_interrupts(gated: bool, signal: str) -> None:
    """Finding 2: the provider paused, and the turn ends before the decision starts."""
    fa = FakeAnthropic()
    _script(fa, replay=False)
    delivering = asyncio.Event()
    closing = asyncio.Event()
    release = asyncio.Event()

    class PauseHeld(RecordingLifecycle):
        async def on_sse_event(self, event: Any) -> None:
            await super().on_sse_event(event)
            if event.type == "session.status_idle":
                delivering.set()
                await asyncio.Event().wait()

    real_stream = fa.beta.sessions.events.stream

    async def stream(**kwargs: Any) -> Any:
        opened = await real_stream(**kwargs)
        if fa.beta.sessions.events.stream_calls == 1:
            real_close = opened.close

            async def close() -> None:
                closing.set()
                await release.wait()
                await real_close()

            opened.close = close  # type: ignore[method-assign]
        return opened

    fa.beta.sessions.events.stream = stream  # type: ignore[method-assign]

    async def gate() -> None:
        return None

    turn = _run(
        fa,
        gate=gate if gated else None,
        decide=_allow,
        cancel=asyncio.Event(),
        lifecycle=PauseHeld(),
        deadline_s=0.3 if signal == "ceiling" else None,
    )
    await asyncio.wait_for(delivering.wait(), timeout=2)
    if signal == "cancel":
        turn.cancel()
    await asyncio.wait_for(closing.wait(), timeout=2)
    await _until_sent(fa, "user.interrupt")  # before the close is released
    release.set()
    outcome = await _settled(turn)

    if signal == "cancel":
        assert isinstance(outcome, asyncio.CancelledError)
    else:
        assert isinstance(outcome, TurnState)
        assert outcome.error is not None and outcome.error.kind == "ceiling"
    assert _sent(fa, "user.tool_confirmation") == []
    assert len(_sent(fa, "user.interrupt")) == 1
    assert not _live_turn_tasks()


@pytest.mark.parametrize("gated", [False, True], ids=["ungated", "gated"])
@pytest.mark.parametrize("signal", ["cancel", "ceiling"])
async def test_abort_during_replay_preprocessing_interrupts(gated: bool, signal: str) -> None:
    """Finding 2: the turn ends while the replay path checks the session's status."""
    fa = FakeAnthropic()
    _script(fa, replay=True)
    checking = asyncio.Event()
    real_retrieve = fa.beta.sessions.retrieve

    async def retrieve(session_id: str) -> Any:
        checking.set()
        await asyncio.Event().wait()
        return await real_retrieve(session_id)

    fa.beta.sessions.retrieve = retrieve  # type: ignore[method-assign]

    async def gate() -> None:
        return None

    turn = _run(
        fa,
        gate=gate if gated else None,
        decide=_allow,
        cancel=asyncio.Event(),
        deadline_s=0.3 if signal == "ceiling" else None,
    )
    await asyncio.wait_for(checking.wait(), timeout=2)
    if signal == "cancel":
        turn.cancel()
    outcome = await _settled(turn)

    if signal == "cancel":
        assert isinstance(outcome, asyncio.CancelledError)
    else:
        assert isinstance(outcome, TurnState)
        assert outcome.error is not None and outcome.error.kind == "ceiling"
    assert _sent(fa, "user.tool_confirmation") == []
    assert len(_sent(fa, "user.interrupt")) == 1
    assert not _live_turn_tasks()


@pytest.mark.parametrize("signal", ["failure", "cancel", "stop", "ceiling"])
async def test_an_aborted_initial_gate_interrupts(signal: str) -> None:
    """Finding 2: the gate before the turn's message fails or is cancelled."""
    fa = FakeAnthropic()
    fa.beta.sessions.events.stream_scripts = [
        [BlockForever()],
        [YieldEvent(make_status_idle(event_id="interrupt_ack", stop_reason=make_end_turn()))],
    ]
    cancel = asyncio.Event()
    entered = asyncio.Event()

    async def gate() -> None:
        entered.set()
        if signal == "failure":
            raise RuntimeError("session update refused")
        await asyncio.Event().wait()

    turn = _run(
        fa,
        gate=gate,
        decide=_allow,
        cancel=cancel,
        deadline_s=0.3 if signal == "ceiling" else None,
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    if signal == "cancel":
        turn.cancel()
    elif signal == "stop":
        cancel.set()
    outcome = await _settled(turn)

    if signal == "failure":
        assert isinstance(outcome, RuntimeError)
    elif signal == "cancel":
        assert isinstance(outcome, asyncio.CancelledError)
    elif signal == "ceiling":
        assert isinstance(outcome, TurnState)
        assert outcome.error is not None and outcome.error.kind == "ceiling"
    else:
        assert isinstance(outcome, TurnState)
        assert outcome.error is not None and outcome.error.kind == "interrupted"
    assert _sent(fa, "user.message") == [], "the message never went out"
    assert len(_sent(fa, "user.interrupt")) == 1
    assert not _live_turn_tasks()


@pytest.mark.parametrize("replay", [False, True], ids=["live", "replay"])
@pytest.mark.parametrize("signal", ["stop", "cancel"])
async def test_interrupt_starts_before_a_cancelled_gate_finishes_its_cleanup(
    replay: bool, signal: str
) -> None:
    """Finding 2: a confirmation gate with awaited cancellation cleanup."""
    fa = FakeAnthropic()
    _script(fa, replay=replay)
    cancel = asyncio.Event()
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def gate() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    turn = _run(fa, gate=gate, decide=_allow, cancel=cancel)
    await asyncio.wait_for(entered.wait(), timeout=2)
    if signal == "stop":
        cancel.set()
    else:
        turn.cancel()
    await asyncio.wait_for(cleaning.wait(), timeout=2)
    await _until_sent(fa, "user.interrupt")  # before the gate's cleanup ends
    release.set()
    outcome = await _settled(turn)

    if signal == "stop":
        assert isinstance(outcome, TurnState)
        assert outcome.termination == TerminationReason.INTERRUPTED
    else:
        assert isinstance(outcome, asyncio.CancelledError)
    assert _sent(fa, "user.tool_confirmation") == []
    assert len(_sent(fa, "user.interrupt")) == 1
    assert not _live_turn_tasks()


@pytest.mark.parametrize("started_by", ["stop", "cancel"])
async def test_an_interrupt_stuck_in_its_own_cleanup_cannot_hold_the_turn(
    monkeypatch: pytest.MonkeyPatch, started_by: str
) -> None:
    """Review s1-abort, finding 3: the join stays bounded after it cancels the interrupt."""
    monkeypatch.setattr(driver, "CLEANUP_BUDGET_S", 0.2)
    fa = FakeAnthropic()
    _script(fa, replay=False)
    cancel = asyncio.Event()
    entered = asyncio.Event()
    unstick = asyncio.Event()
    real_stream = fa.beta.sessions.events.stream

    async def stream(**kwargs: Any) -> Any:
        if fa.beta.sessions.events.stream_calls == 1:
            # The interrupt's acknowledgement stream: it never answers, and
            # its teardown ignores cancellation until released.
            while not unstick.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await unstick.wait()
            raise asyncio.CancelledError
        return await real_stream(**kwargs)

    fa.beta.sessions.events.stream = stream  # type: ignore[method-assign]

    async def decide(call: ToolCall) -> ToolConfirmationResult:
        entered.set()
        await asyncio.Event().wait()
        return ToolConfirmationResult(allow=True)

    turn = _run(fa, gate=None, decide=decide, cancel=cancel, interrupt_timeout_s=60)
    await asyncio.wait_for(entered.wait(), timeout=2)
    if started_by == "stop":
        cancel.set()
        await asyncio.sleep(0.05)
    turn.cancel()
    started = asyncio.get_running_loop().time()
    [outcome] = await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), timeout=5)
    elapsed = asyncio.get_running_loop().time() - started

    assert isinstance(outcome, asyncio.CancelledError)
    assert elapsed < 1.5, f"the abort join must stay bounded, took {elapsed:.2f}s"
    assert _sent(fa, "user.tool_confirmation") == []
    [abandoned] = [t for t in _live_turn_tasks() if t.get_name() == "turn.unattended_abort"]
    unstick.set()
    await asyncio.wait_for(asyncio.gather(abandoned, return_exceptions=True), timeout=2)
    await asyncio.sleep(0)
    assert not _live_turn_tasks()


@pytest.mark.parametrize("replay", [False, True], ids=["live", "replay"])
@pytest.mark.parametrize("gated", [False, True], ids=["ungated", "gated"])
@pytest.mark.parametrize("failure", ["error", "cancelled"])
async def test_a_failed_call_decision_cancels_and_joins_its_siblings(
    replay: bool, gated: bool, failure: str
) -> None:
    """Review s1-abort, finding 4: no sibling decider outlives the decision."""
    fa = FakeAnthropic()
    _script(fa, replay=replay, ids=("tu_wait", "tu_fail"))
    sibling_waiting = asyncio.Event()
    sibling_finished = False

    async def decide(call: ToolCall) -> ToolConfirmationResult:
        nonlocal sibling_finished
        if call.tool_use_id == "tu_wait":
            sibling_waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)  # a card hook retiring its card
                sibling_finished = True
        await sibling_waiting.wait()
        if failure == "error":
            raise ValueError("decider failed")
        raise asyncio.CancelledError

    async def gate() -> None:
        return None

    turn = _run(fa, gate=gate if gated else None, decide=decide, cancel=asyncio.Event())
    outcome = await _settled(turn)

    assert isinstance(outcome, ValueError if failure == "error" else asyncio.CancelledError)
    assert sibling_finished, "the waiting sibling was cancelled and joined"
    assert _sent(fa, "user.tool_confirmation") == []
    assert len(_sent(fa, "user.interrupt")) == 1
    assert not _live_turn_tasks()
