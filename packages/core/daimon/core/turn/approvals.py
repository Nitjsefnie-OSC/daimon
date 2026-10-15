"""Pure decision logic for `AutoApprove` tool confirmations.

This module is pure — no `anthropic` client, no `httpx`, no DB, no clock —
so the dedup rule (which `tool_use_id`s are still fresh) is unit-testable
without a fake stream, which is exactly where a reconnect-dedup bug would
otherwise hide. The SEND itself (`anthropic.beta.sessions.events.send(...)`)
stays in `driver.py` because it is I/O; this module only decides WHAT to
send.

`build_confirmation_events` hardcodes `result="allow"` because it serves
`AutoApprove` only. The per-call posture, `PolicyApproval`, has its own pair:
`tool_calls_for` turns the blocked ids into `ToolCall`s from the folded
state, and `build_decision_events` sends each call's own allow/deny.

The decider builders are the policy shells over
`daimon.core.tool_safety.decide_tool_call`. `unattended_decider` never waits
on anyone; `interactive_decider` hands `ask` verdicts to the adapter's
`ConfirmationHook`. Neither does I/O of its own — the hook is the only thing
that talks to a platform, and a hook that raises counts as a refusal."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

import structlog
from anthropic.types.beta.sessions import BetaManagedAgentsUserToolConfirmationEventParams
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import StopReason
from daimon.core.confirmation import (
    ApprovedConfirmation,
    ConfirmationHook,
    ToolRuleCardReference,
    no_confirmation_surface,
    prompt_for_tool_call,
)
from daimon.core.stores import agent_tool_rules
from daimon.core.tool_safety import (
    ResolvedAgentToolRule,
    ToolCall,
    ToolSafetyPolicy,
    ToolVerdict,
    decide_tool_call,
    is_publish_call,
)
from daimon.core.turn.posture import (
    AutoApprove,
    PolicyApproval,
    RequireApproval,
    ToolCallDecider,
    ToolConfirmation,
    ToolConfirmationResult,
)
from daimon.core.turn.state import ToolUseBlock, TurnState
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)
RuleLookup = Callable[[ToolCall], Awaitable[ResolvedAgentToolRule | None]]
RuleCardFor = Callable[[ToolCall], ToolRuleCardReference | None]


def channel_for_tool_rules(
    *, platform: str, channel_id: str | None, private_dm_id: str | None
) -> str | None:
    """Use the admitted parent channel; a DM never inherits its source's rule."""
    if private_dm_id is not None or channel_id is None:
        return None
    return channel_id.partition(":")[0] if platform == "slack" else channel_id


def agent_rule_lookup(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    server_urls: Mapping[str, str],
    channel_id: str | None = None,
) -> RuleLookup:
    """Capture a session's agent and endpoint bindings; read the row per call."""

    async def _lookup(call: ToolCall) -> ResolvedAgentToolRule | None:
        url = server_urls.get(call.server_name or "")
        if url is None:
            return None
        async with factory() as session:
            return await agent_tool_rules.get_for_call(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                tool_key=call.key,
                server_url=url,
                channel_id=channel_id,
            )

    return _lookup


async def _rule_for(
    policy: ToolSafetyPolicy,
    call: ToolCall,
    trusted_servers: frozenset[str],
    rules_for: RuleLookup | None,
) -> tuple[ResolvedAgentToolRule | None, bool]:
    if (
        not policy.enabled
        or not policy.agent_rules
        or rules_for is None
        or call.server_name is None
        or call.server_name in trusted_servers
    ):
        return None, False
    try:
        return await rules_for(call), False
    except Exception as err:
        log.warning("tool_safety.rule_read_failed", tool=call.key, error=str(err))
        return None, True


def _read_error_verdict(verdict: ToolVerdict, *, attended: bool) -> ToolVerdict:
    if verdict.reason == "denied_by_operator":
        return verdict
    return ToolVerdict(
        outcome="ask" if attended else "deny",
        effect="write",
        reason="write_needs_confirmation" if attended else "unattended_write",
    )


#: Stand-in for a blocked id whose `tool_use` event never reached the folded
#: state (a replay window that missed it). Nobody has seen its server, tool
#: or input, so both deciders refuse it outright — a card for it would ask a
#: person to approve something they cannot see.
_UNKNOWN_TOOL = "unknown"

_UNSEEN_MESSAGE = (
    "Refused: daimon could not see this call's server, tool or input, so nobody could "
    "approve it. It did not run. If it is still needed, call the tool again."
)


def _is_unseen(call: ToolCall) -> bool:
    return call.server_name == _UNKNOWN_TOOL and call.tool_name == _UNKNOWN_TOOL


def _unseen_refusal(call: ToolCall) -> ToolConfirmationResult:
    log.warning("tool_safety.unseen_call_refused", tool_use_id=call.tool_use_id)
    return ToolConfirmationResult(allow=False, deny_message=_UNSEEN_MESSAGE)


def pending_confirmation_ids(
    stop_reason: StopReason | None,
    *,
    confirmed: set[str],
) -> list[str]:
    """Return `stop_reason.event_ids` not already in `confirmed`.

    Takes the SDK `StopReason` union directly (not the `session.status_idle`
    event that carries it) so this ONE implementation serves both call
    sites that need it: the live consume loop, which has the idle event
    itself, and the eventless-cycle reconnect branch in `driver.py`, which
    only has the folded `TurnState.stop_reason` (the reducer stores the
    same SDK union verbatim). A second, near-identically-named function for
    the reconnect branch is precisely where a reconnect-dedup bug would
    hide — one function, two callers, zero duplicated dedup logic.

    Order is preserved from `stop_reason.event_ids`. Does NOT mutate
    `confirmed` — the caller owns that step, so "which ids did we just
    claim" stays visible at the call site instead of hidden inside this
    function.

    `None` and any non-`requires_action` member return `[]` rather than
    raising, so neither caller needs a pre-check: the live loop already
    knows `terminal_stop_reason(event) == "requires_action"` before
    calling, and the reconnect branch may have folded a `TurnState` whose
    `stop_reason` is `None` or some other member entirely.
    """
    if stop_reason is None or stop_reason.type != "requires_action":
        return []
    return [tool_use_id for tool_use_id in stop_reason.event_ids if tool_use_id not in confirmed]


def build_confirmation_events(
    ids: Sequence[str],
) -> list[BetaManagedAgentsUserToolConfirmationEventParams]:
    """Build one `user.tool_confirmation` `allow` payload per id, in order."""
    return [
        BetaManagedAgentsUserToolConfirmationEventParams(
            type="user.tool_confirmation",
            result="allow",
            tool_use_id=tool_use_id,
        )
        for tool_use_id in ids
    ]


def tool_calls_for(state: TurnState, ids: Sequence[str]) -> list[ToolCall]:
    """One `ToolCall` per blocked id, from the tool-use blocks in `state`."""
    blocks = {b.id: b for b in state.content if isinstance(b, ToolUseBlock)}
    calls: list[ToolCall] = []
    for tool_use_id in ids:
        block = blocks.get(tool_use_id)
        if block is None:
            calls.append(
                ToolCall(
                    tool_use_id=tool_use_id, server_name=_UNKNOWN_TOOL, tool_name=_UNKNOWN_TOOL
                )
            )
            continue
        calls.append(
            ToolCall(
                tool_use_id=tool_use_id,
                server_name=block.mcp_server_name,
                tool_name=block.name,
                input=dict(block.input),
            )
        )
    return calls


def build_decision_events(
    decisions: Iterable[tuple[str, ToolConfirmationResult]],
) -> list[BetaManagedAgentsUserToolConfirmationEventParams]:
    """One `user.tool_confirmation` per `(tool_use_id, result)`, in order."""
    events: list[BetaManagedAgentsUserToolConfirmationEventParams] = []
    for tool_use_id, result in decisions:
        if result.allow:
            events.append(
                BetaManagedAgentsUserToolConfirmationEventParams(
                    type="user.tool_confirmation", result="allow", tool_use_id=tool_use_id
                )
            )
            continue
        event = BetaManagedAgentsUserToolConfirmationEventParams(
            type="user.tool_confirmation", result="deny", tool_use_id=tool_use_id
        )
        if result.deny_message:
            event["deny_message"] = result.deny_message
        events.append(event)
    return events


def refusal_message(call: ToolCall, verdict: ToolVerdict) -> str:
    """What the model is told when `call` is refused without a person."""
    if verdict.reason == "denied_by_operator":
        return f"Refused: the operator does not allow {call.key}. Do not retry it."
    if verdict.reason == "agent_rule_deny":
        return f"Refused: this agent's tool rule denies {call.key}. Do not retry it."
    if verdict.reason == "agent_rule_ask":
        return (
            f"Refused: this agent's tool rule requires approval for {call.key}, "
            "and nobody is here to approve. Do not retry it."
        )
    if verdict.reason == "unattended_publish":
        return (
            f"Refused: {call.tool_name} publishes, which this agent does only after the "
            "requester approves it, and nobody is here to approve. Do not retry it in this run."
        )
    return (
        f"Refused: {call.key} changes data outside this session and nobody is here to "
        "approve it, so it did not run. Do not retry it in this run; report what you "
        "would have written instead."
    )


def _answer_message(call: ToolCall, answer: str) -> str:
    if answer == "expired":
        return (
            f"Nobody approved {call.key} in time, so it did not run. Do not retry it. "
            "Tell the person in one line: ask again and approve the card when it appears."
        )
    return (
        f"The user denied {call.key}; it did not run. Do not retry it. Ask them what "
        "they want instead."
    )


def unattended_decider(
    policy: ToolSafetyPolicy,
    *,
    trusted_servers: frozenset[str] = frozenset(),
    confirmed_tools: frozenset[str] = frozenset(),
    rules_for: RuleLookup | None = None,
) -> ToolCallDecider:
    """Decider for runs nobody is watching: reads run, writes are refused
    unless the operator allowed them there."""

    async def _decide(call: ToolCall) -> ToolConfirmationResult:
        if _is_unseen(call):
            return _unseen_refusal(call)
        rule, read_error = await _rule_for(policy, call, trusted_servers, rules_for)
        verdict = decide_tool_call(
            policy,
            call,
            attended=False,
            trusted_servers=trusted_servers,
            confirmed_tools=confirmed_tools,
            agent_rule=rule,
        )
        if read_error:
            verdict = _read_error_verdict(verdict, attended=False)
        log.info(
            "tool_safety.decided",
            tool=call.key,
            outcome=verdict.outcome,
            reason=verdict.reason,
            attended=False,
            **({"rule": rule.rule} if verdict.reason.startswith("agent_rule_") and rule else {}),
        )
        if verdict.outcome == "allow":
            return ToolConfirmationResult(allow=True)
        return ToolConfirmationResult(allow=False, deny_message=refusal_message(call, verdict))

    return _decide


def interactive_decider(
    policy: ToolSafetyPolicy,
    *,
    requester_platform_user_id: str,
    confirm: ConfirmationHook = no_confirmation_surface,
    trusted_servers: frozenset[str] = frozenset(),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    confirmed_tools: frozenset[str] = frozenset(),
    rules_for: RuleLookup | None = None,
    rule_card_for: RuleCardFor | None = None,
) -> ToolCallDecider:
    """Decider for chat: reads run, writes wait for `confirm`.

    `confirm` is the adapter's card hook; the default refuses, which is what a
    surface without cards gets. A hook that raises is logged and counts as a
    refusal — a write never runs because the card failed to post.
    """

    async def _decide(call: ToolCall) -> ToolConfirmationResult:
        if _is_unseen(call):
            return _unseen_refusal(call)
        rule, read_error = await _rule_for(policy, call, trusted_servers, rules_for)
        verdict = decide_tool_call(
            policy,
            call,
            attended=True,
            trusted_servers=trusted_servers,
            confirmed_tools=confirmed_tools,
            agent_rule=rule,
        )
        if read_error:
            verdict = _read_error_verdict(verdict, attended=True)
        log.info(
            "tool_safety.decided",
            tool=call.key,
            outcome=verdict.outcome,
            reason=verdict.reason,
            attended=True,
            **({"rule": rule.rule} if verdict.reason.startswith("agent_rule_") and rule else {}),
        )
        if verdict.outcome == "allow":
            return ToolConfirmationResult(allow=True)
        if verdict.outcome == "deny":
            return ToolConfirmationResult(allow=False, deny_message=refusal_message(call, verdict))
        try:
            prompt = prompt_for_tool_call(
                call,
                requester_platform_user_id=requester_platform_user_id,
                now=now(),
                timeout=timedelta(seconds=policy.confirmation_timeout_s),
            )
            if policy.enabled and policy.agent_rules:
                prompt = prompt.model_copy(update={"tool_rules_enabled": True})
            if (
                policy.enabled
                and policy.agent_rules
                and verdict.reason == "write_needs_confirmation"
                and not read_error
                and call.server_name not in trusted_servers
                and rule_card_for is not None
            ):
                prompt = prompt.model_copy(update={"tool_rule": rule_card_for(call)})
            response = await confirm(prompt)
        except Exception as err:
            # Named boundary: prompt validation and the adapter's card must
            # both succeed before the write may run.
            log.warning("tool_safety.confirm_failed", tool=call.key, error=str(err))
            response = "denied"
        answer = response.answer if isinstance(response, ApprovedConfirmation) else response
        log.info("tool_safety.answered", tool=call.key, answer=answer)
        if answer == "approved":
            if policy.enabled and policy.agent_rules and rules_for is not None:
                latest, read_failed = await _rule_for(policy, call, trusted_servers, rules_for)
                if read_failed or (latest is not None and latest.rule == "deny"):
                    if isinstance(response, ApprovedConfirmation):
                        await response.retire_unsent()
                    return ToolConfirmationResult(
                        allow=False,
                        deny_message=(
                            refusal_message(
                                call,
                                ToolVerdict(
                                    outcome="deny", effect="write", reason="agent_rule_deny"
                                ),
                            )
                            if not read_failed
                            else f"Refused: could not check this agent's rule for {call.key}."
                        ),
                    )
            return ToolConfirmationResult(
                allow=True,
                retire_unsent=response.retire_unsent
                if isinstance(response, ApprovedConfirmation)
                else None,
            )
        return ToolConfirmationResult(allow=False, deny_message=_answer_message(call, answer))

    return _decide


def headless_tool_confirmation(
    policy: ToolSafetyPolicy,
    *,
    trusted_servers: frozenset[str] = frozenset(),
    rules_for: RuleLookup | None = None,
) -> ToolConfirmation:
    """Posture for a run nobody is watching (routines, smoke, transfers).

    Disabled keeps `AutoApprove`, byte-identical to before the policy existed.
    """
    if not policy.enabled:
        return AutoApprove()
    return PolicyApproval(
        decide=unattended_decider(policy, trusted_servers=trusted_servers, rules_for=rules_for)
    )


def chat_tool_confirmation(
    policy: ToolSafetyPolicy,
    *,
    requester_platform_user_id: str,
    confirm: ConfirmationHook | None,
    attended: bool = True,
    trusted_servers: frozenset[str] = frozenset(),
    asks_before_publishing: bool = False,
    confirmed_tools: frozenset[str] = frozenset(),
    rules_for: RuleLookup | None = None,
    rule_card_for: RuleCardFor | None = None,
) -> ToolConfirmation:
    """Posture for a chat turn.

    Disabled keeps `RequireApproval`, as before, unless the session asks
    before publishing (`decide_tool_call`). `attended=False` is a chat turn
    nobody asked for just now (a wake continuing earlier work): it gets the
    unattended rules even though it renders into a thread.
    """
    if not policy.enabled and not asks_before_publishing and not confirmed_tools:
        return RequireApproval()
    decide = (
        interactive_decider(
            policy,
            requester_platform_user_id=requester_platform_user_id,
            confirm=confirm if confirm is not None else no_confirmation_surface,
            trusted_servers=trusted_servers,
            confirmed_tools=confirmed_tools,
            rules_for=rules_for,
            rule_card_for=rule_card_for,
        )
        if attended
        else unattended_decider(
            policy,
            trusted_servers=trusted_servers,
            confirmed_tools=confirmed_tools,
            rules_for=rules_for,
        )
    )
    if not policy.enabled:
        decide = _selected_only(
            decide,
            trusted_servers=trusted_servers,
            confirmed_tools=confirmed_tools,
            asks_before_publishing=asks_before_publishing,
        )
    return PolicyApproval(decide=decide)


def _selected_only(
    decide: ToolCallDecider,
    *,
    trusted_servers: frozenset[str],
    confirmed_tools: frozenset[str],
    asks_before_publishing: bool,
) -> ToolCallDecider:
    """Tool safety off: only explicitly selected calls are decided; others are
    refused, as `RequireApproval` would have ended the turn before it ran."""

    async def _decide(call: ToolCall) -> ToolConfirmationResult:
        if _is_unseen(call):
            return _unseen_refusal(call)
        if (asks_before_publishing and is_publish_call(call, trusted_servers)) or (
            call.server_name in trusted_servers and call.tool_name in confirmed_tools
        ):
            return await decide(call)
        return ToolConfirmationResult(
            allow=False,
            deny_message=f"Refused: {call.key} waits for an approval nobody here can give, "
            "so it did not run. Do not retry it.",
        )

    return _decide
