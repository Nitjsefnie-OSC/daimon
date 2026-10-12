from datetime import timedelta

import pytest
from pydantic import ValidationError

from qa.live.config import Pricing
from qa.live.cost import Ledger, estimate
from qa.live.discord import fingerprint
from qa.live.evaluate import evaluate
from qa.live.runner import Executor
from qa.live.schema import Assertion, Scenario, Step
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Message, Turn, card_text_of, text_of, utcnow


def observed(number: int, identity: str, *, settled: bool = True) -> Turn:
    turn = Turn(
        number, str(number), "parent", utcnow(), thread_id=f"thread-{number}", settled=settled
    )
    turn.messages = [{"id": identity, "content": "ANSWER"}]
    return turn


@pytest.mark.parametrize("kind", ["answers_total", "threads_created"])
def test_whole_run_counts_prove_violation_but_not_timed_out_upper_bound(kind: str) -> None:
    backend, judge = FakeBackend(), FakeJudge()
    assertion = Assertion.model_validate({"kind": kind, "max": 1})
    turns = [observed(1, "a"), observed(2, "b", settled=False)]
    assert evaluate(assertion, turns, backend, judge).status == "FAIL"
    turns[1].thread_id = turns[0].thread_id
    turns[1].messages = turns[0].messages
    assert evaluate(assertion, turns, backend, judge).status == "PENDING"
    turns[1].settled = True
    assert evaluate(assertion, turns, backend, judge).status == "PASS"


@pytest.mark.parametrize(
    "pattern,missing,expected",
    [("ANSWER", True, "PASS"), ("OTHER", True, "PENDING"), ("OTHER", False, "FAIL")],
)
def test_any_of_three_valued_evidence(pattern: str, missing: bool, expected: str) -> None:
    assertion = Assertion.model_validate(
        {
            "kind": "any_of",
            "of": [
                {"kind": "text_present", "turn": 1, "pattern": pattern},
                {"kind": "text_present", "turn": 2 if missing else 1, "pattern": "NO"},
            ],
        }
    )
    assert evaluate(assertion, [observed(1, "a")], FakeBackend(), FakeJudge()).status == expected


def test_component_reads_nested_labels_and_refresh_reads_current_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend()
    turn = observed(1, "card")
    turn.messages[0]["components"] = [
        {"type": 1, "components": [{"type": 2, "label": "Connect GitHub"}]}
    ]
    assert (
        evaluate(
            Assertion(kind="component_present", turn=1, label_pattern="(?i)connect github"),
            [turn],
            backend,
            FakeJudge(),
        ).status
        == "PASS"
    )
    monkeypatch.setattr(
        backend, "current_messages", lambda turn: [{"id": "card", "content": "Expired"}]
    )
    check = Assertion(
        kind="card_text_now", turn=1, pattern="(?i)expired", pattern_absent="received"
    )
    assert evaluate(check, [turn], backend, FakeJudge()).status == "PASS"
    assert turn.messages[0]["content"] == "ANSWER"


def test_running_edits_are_distinct_and_exclude_terminal_edits() -> None:
    turn = observed(1, "card")
    edit: Message = {
        "message_id": "card",
        "edited_timestamp": utcnow().isoformat(),
        "phase": "running",
    }
    turn.card_history = [edit, edit, {**edit, "edited_timestamp": "later", "phase": "terminal"}]
    assertion = Assertion(kind="card_edits_min", turn=1, minimum=2, during="running")
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PENDING"
    turn.card_history = [
        {
            **edit,
            "observed_at": (turn.started_at + timedelta(seconds=i)).isoformat(),
            "message": {"id": "card", "edited_timestamp": "original"},
            "poll_interval_s": 4,
        }
        for i in range(3)
    ]
    turn.done_s = 3
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "FAIL"
    turn.settled = False
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PENDING"
    turn.card_history.extend(
        {
            **edit,
            "observed_at": (turn.started_at + timedelta(seconds=i)).isoformat(),
            "message": {"id": "card", "edited_timestamp": str(i)},
        }
        for i in (3, 4)
    )
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PASS"


@pytest.mark.parametrize("samples", [0, 1, 2])
def test_edit_minimum_never_fails_on_missing_or_insufficient_capture(samples: int) -> None:
    turn = observed(1, "card")
    turn.card_history = [
        {
            "message_id": "card",
            "phase": "running",
            "observed_at": utcnow().isoformat(),
            "message": {"id": "card"},
        }
        for _ in range(samples)
    ]
    check = Assertion(kind="card_edits_min", turn=1, minimum=3, during="running")
    assert evaluate(check, [turn], FakeBackend(), FakeJudge()).status == "PENDING"


def test_adequately_sampled_frozen_card_fails_edit_minimum() -> None:
    turn = observed(1, "card")
    turn.card_history = [
        {
            "message_id": "card",
            "phase": "running",
            "observed_at": (turn.started_at + timedelta(seconds=i * 10)).isoformat(),
            "message": {"id": "card", "embeds": [{"footer": {"text": "Working · 0s"}}]},
            "poll_interval_s": 4,
        }
        for i in range(5)
    ]
    turn.done_s = 45
    check = Assertion(kind="card_edits_min", turn=1, minimum=3, during="running")
    assert evaluate(check, [turn], FakeBackend(), FakeJudge()).status == "FAIL"


def test_footer_changes_count_even_when_discord_edit_timestamp_is_missing() -> None:
    turn = observed(1, "card")
    turn.card_history = [
        {
            "message_id": "card",
            "phase": "running",
            "observed_at": (turn.started_at + timedelta(seconds=i * 10)).isoformat(),
            "message": {"id": "card", "embeds": [{"footer": {"text": f"Working · {i * 10}s"}}]},
        }
        for i in range(4)
    ]
    check = Assertion(kind="card_edits_min", turn=1, minimum=3, during="running")
    assert evaluate(check, [turn], FakeBackend(), FakeJudge()).status == "PASS"


def test_current_card_reads_nested_v2_copy_without_button_labels() -> None:
    turn = observed(1, "card")
    turn.messages = [
        {
            "id": "card",
            "components": [
                {
                    "type": 17,
                    "components": [
                        {
                            "type": 9,
                            "components": [{"type": 10, "content": "Request expired."}],
                            "accessory": {"type": 2, "label": "Received"},
                        }
                    ],
                }
            ],
        }
    ]
    check = Assertion(
        kind="card_text_now", turn=1, pattern="(?i)expired", pattern_absent="Received"
    )
    assert evaluate(check, [turn], FakeBackend(), FakeJudge()).status == "PASS"
    assert card_text_of(turn.messages[0]).strip() == "Request expired."
    assert not text_of(turn.messages[0]).strip()


@pytest.mark.parametrize("absence", [False, True])
@pytest.mark.parametrize("with_readable_row", [False, True])
def test_current_card_unreadable_copy_never_proves_presence_or_absence(
    absence: bool, with_readable_row: bool
) -> None:
    turn = observed(1, "card")
    turn.messages = [{"id": "card", "components": [{"type": 2, "label": "Expired"}]}]
    if with_readable_row:
        turn.messages.append({"id": "answer", "content": "Expired"})
    check = Assertion(
        kind="card_text_now",
        turn=1,
        pattern=None if absence else "Expired",
        pattern_absent="Received" if absence else None,
    )
    assert evaluate(check, [turn], FakeBackend(), FakeJudge()).status == "PENDING"


def test_component_only_edits_change_fingerprint_without_edit_timestamp() -> None:
    before: Message = {
        "id": "card",
        "components": [{"type": 17, "components": [{"type": 10, "content": "Received"}]}],
    }
    after: Message = {
        **before,
        "components": [{"type": 17, "components": [{"type": 10, "content": "Expired"}]}],
    }
    assert fingerprint(before) != fingerprint(after)


def test_v2_card_extraction_does_not_relax_anchored_answer_matching() -> None:
    turn = observed(1, "answer")
    turn.messages = [
        {
            "id": "answer",
            "content": "-# qa-agent\nNot B1: needs context\nB1",
            "components": [
                {"type": 10, "content": "B1"},
                {"type": 10, "content": "Not B1: context"},
            ],
        }
    ]
    turn.agent_subtext_headers = [{"message_id": "answer", "line": "-# qa-agent"}]
    assertion = Assertion(kind="text_present", turn=1, pattern="^B1$")
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "FAIL"
    turn.messages[0]["content"] = "-# qa-agent\nB1"
    turn.messages[0]["components"] = [{"type": 10, "content": "Footer: $0.01"}]
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PASS"


def test_chunk_gap_uses_answer_edit_instead_of_progress_card_creation() -> None:
    turn = observed(1, "1")
    turn.baseline_message_ids = ["1"]
    turn.messages = [
        {
            "id": "1",
            "content": "one",
            "timestamp": turn.started_at.isoformat(),
            "edited_timestamp": (turn.started_at + timedelta(seconds=40)).isoformat(),
        },
        {
            "id": "2",
            "content": "two",
            "timestamp": (turn.started_at + timedelta(seconds=43)).isoformat(),
        },
    ]
    assertion = Assertion(kind="chunks_gap_max_s", turn=1, maximum=5)
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PASS"
    turn.settled = False
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PENDING"
    turn.messages[1]["timestamp"] = (turn.started_at + timedelta(seconds=47)).isoformat()
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "FAIL"


def test_foreign_or_unresolved_guild_refuses_before_setup_mutation(
    scenario: Scenario, ledger: Ledger, pricing: Pricing
) -> None:
    scenario.setup = [Step(do="admin", tool="cli", args="daimon tenants credit forbidden")]
    scenario.steps[0].guild = "other-guild"
    backend = FakeBackend()
    result = Executor(backend, FakeJudge(), ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING" and not backend.events
    assert ledger.charged({result.run_id}) == 0


def test_step_scope_validation_and_nested_judge_budget(
    scenario: Scenario, pricing: Pricing
) -> None:
    with pytest.raises(ValidationError):
        Step(do="mention", text="x", allow_fail=True)
    scenario.assertions = [
        Assertion(
            kind="any_of",
            alternatives=[
                Assertion(kind="judge", turn=1, rubric="answer"),
                Assertion(kind="judge", turn=2, rubric="answer"),
            ],
        )
    ]
    assert estimate(scenario, pricing) > scenario.est_turns * pricing.per_turn_usd


def test_late_answer_edits_cannot_hide_or_invent_chunk_gaps() -> None:
    turn = observed(1, "1")
    turn.messages = [
        {
            "id": "1",
            "content": "one",
            "timestamp": turn.started_at.isoformat(),
            "edited_timestamp": (turn.started_at + timedelta(seconds=40)).isoformat(),
        },
        {
            "id": "2",
            "content": "two",
            "timestamp": (turn.started_at + timedelta(seconds=43)).isoformat(),
            "edited_timestamp": (turn.started_at + timedelta(seconds=100)).isoformat(),
        },
    ]
    assertion = Assertion(kind="chunks_gap_max_s", turn=1, maximum=5)
    # Creation-to-creation stall remains a failure despite later edits.
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "FAIL"
    turn.messages[1]["timestamp"] = (turn.started_at + timedelta(seconds=3)).isoformat()
    # A late footer edit cannot invent a stall.
    assert evaluate(assertion, [turn], FakeBackend(), FakeJudge()).status == "PASS"


def test_same_turn_running_card_delivery_uses_its_answer_edit() -> None:
    turn = observed(1, "card")
    turn.card_history = [{"message_id": "card", "phase": "running"}]
    turn.messages = [
        {
            "id": "card",
            "content": "chunk one",
            "timestamp": (turn.started_at + timedelta(seconds=1)).isoformat(),
            "edited_timestamp": (turn.started_at + timedelta(seconds=17)).isoformat(),
        },
        {
            "id": "chunk2",
            "content": "chunk two",
            "timestamp": (turn.started_at + timedelta(seconds=17.237)).isoformat(),
        },
    ]
    check = Assertion(kind="chunks_gap_max_s", turn=1, maximum=5)
    result = evaluate(check, [turn], FakeBackend(), FakeJudge())
    assert result.status == "PASS" and "0.237" in result.reason
    turn.messages[1]["timestamp"] = (turn.started_at + timedelta(seconds=23)).isoformat()
    assert evaluate(check, [turn], FakeBackend(), FakeJudge()).status == "FAIL"
    turn.messages[0].pop("edited_timestamp")
    assert evaluate(check, [turn], FakeBackend(), FakeJudge()).status == "PENDING"
    # A terminal-only observation cannot prove that creation preceded answer delivery.
    turn.card_history[0]["phase"] = "terminal"
    assert evaluate(check, [turn], FakeBackend(), FakeJudge()).status == "FAIL"


@pytest.mark.parametrize(
    "args",
    [
        "daimon tenants credit discord 1435062989119295640 -999 --note qa",
        "uv run daimon tenants credit discord 1435062989119295640 -999 --note qa",
        "daimon --env staging tenants credit discord 1435062989119295640 -999 --note qa",
    ],
)
def test_shared_guild_credit_is_refused_before_any_mutation(
    args: str, scenario: Scenario, ledger: Ledger, pricing: Pricing
) -> None:
    scenario.setup = [
        Step(
            do="admin",
            tool="cli",
            args=args,
        )
    ]
    scenario.steps[0].guild = "1435062989119295640"
    backend = FakeBackend()
    result = Executor(backend, FakeJudge(), ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING" and not backend.events
    assert any("credit mutations" in c.reason for c in result.checks)
    assert ledger.charged({result.run_id}) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"allow_fail": True},
        {"allow_fail": True, "allow_fail_pattern": ".*"},
        {"allow_fail": True, "allow_fail_pattern": "^"},
        {"allow_fail": True, "allow_fail_pattern": "["},
        {"allow_fail": True, "allow_fail_pattern": "refus", "allow_fail_exit_codes": [0]},
    ],
)
def test_expected_refusal_is_explicit_and_validated(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Step.model_validate(
            {"do": "admin", "tool": "cli", "args": "daimon skills delete qa-test", **payload}
        )


@pytest.mark.parametrize("section", ["setup", "teardown"])
def test_dict_cli_arguments_are_refused_before_mutation(
    scenario: Scenario, ledger: Ledger, pricing: Pricing, section: str
) -> None:
    setattr(scenario, section, [Step(do="admin", tool="cli", args={"argv": "tenants credit"})])
    backend = FakeBackend()
    result = Executor(backend, FakeJudge(), ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING" and not backend.events
    assert any("validated command string" in c.reason for c in result.checks)
    assert ledger.charged({result.run_id}) == 0


def sampled(times: list[float], states: list[str], *, done_s: float = 65) -> Turn:
    turn = observed(1, "card")
    turn.done_s = done_s
    turn.card_history = [
        {
            "message_id": "card",
            "phase": "running",
            "observed_at": (turn.started_at + timedelta(seconds=t)).isoformat(),
            "elapsed_s": t,
            "poll_interval_s": 4,
            "message": {
                "id": "card",
                "edited_timestamp": None,
                "embeds": [{"footer": {"text": state}}],
            },
        }
        for t, state in zip(times, states, strict=True)
    ]
    return turn


EDITS_MIN_3 = Assertion(kind="card_edits_min", turn=1, minimum=3, during="running")


@pytest.mark.parametrize(
    "times",
    [
        [0, 4, 8, 60],  # long mid-run gap: ticks at 10..50 s were never observable
        [0, 4, 8, 12],  # early-only capture: nothing seen during the last 53 s
    ],
)
def test_capture_gaps_never_turn_a_missed_edit_minimum_into_fail(times: list[float]) -> None:
    states = ["Working · 0s"] * 3 + [f"Working · {times[-1]:g}s"]
    check = evaluate(EDITS_MIN_3, [sampled(times, states)], FakeBackend(), FakeJudge())
    assert check.status == "PENDING" and "capture gap" in check.reason, check.reason


def test_unavailable_card_readbacks_leave_a_gap_that_stays_pending() -> None:
    turn = sampled([0, 6, 12, 54, 60], ["Working · 0s"] * 5)
    turn.card_history[3:3] = [
        {
            "message_id": "card",
            "phase": "running",
            "observed_at": (turn.started_at + timedelta(seconds=t)).isoformat(),
            "poll_interval_s": 4,
            "unavailable": "running card readback unavailable: SystemExit",
        }
        for t in range(18, 54, 6)
    ]
    check = evaluate(EDITS_MIN_3, [turn], FakeBackend(), FakeJudge())
    assert check.status == "PENDING" and "6 card readbacks unavailable" in check.reason


def test_unknown_capture_cadence_stays_pending() -> None:
    turn = sampled([0, 6, 12, 18, 24], ["Working · 0s"] * 5, done_s=30)
    del turn.card_history[2]["poll_interval_s"]
    assert evaluate(EDITS_MIN_3, [turn], FakeBackend(), FakeJudge()).status == "PENDING"


def test_closely_sampled_frozen_card_shaped_like_live_d6_still_fails() -> None:
    # Spacing and tail from saved result 20261010T152722810837Z-a5c43f74.
    times = [8.74, 14.96, 21.6, 28.71, 35.24, 42.14, 49.24, 56.43]
    turn = sampled(times, ["Working · 0s"] * len(times), done_s=62.91)
    check = evaluate(EDITS_MIN_3, [turn], FakeBackend(), FakeJudge())
    assert check.status == "FAIL" and "0 successive" in check.reason, check.reason


def test_repeating_states_count_successive_edits() -> None:
    turn = sampled([0, 10, 20, 30], ["Running A", "Running B", "Running A", "Running B"])
    check = evaluate(EDITS_MIN_3, [turn], FakeBackend(), FakeJudge())
    assert check.status == "PASS" and "observed 3 successive" in check.reason, check.reason


def test_duplicate_observations_and_unchanged_samples_are_not_edits() -> None:
    turn = sampled([0, 6, 12, 18, 24], ["A", "A", "B", "B", "A"], done_s=28)
    turn.card_history.insert(3, dict(turn.card_history[2]))  # same observation twice
    turn.card_history.reverse()  # evaluation orders by observation time
    check = evaluate(EDITS_MIN_3, [turn], FakeBackend(), FakeJudge())
    assert check.status == "FAIL" and "observed 2 successive" in check.reason, check.reason


def test_proven_edits_pass_despite_capture_gaps() -> None:
    turn = sampled([0, 30, 31, 60], ["Working · 0s", "Working · 30s", "x", "Working · 60s"])
    assert evaluate(EDITS_MIN_3, [turn], FakeBackend(), FakeJudge()).status == "PASS"


def card_sample(turn: Turn, identity: str, seconds: float, footer: str) -> Message:
    return {
        "message_id": identity,
        "phase": "running",
        "observed_at": (turn.started_at + timedelta(seconds=seconds)).isoformat(),
        "elapsed_s": seconds,
        "poll_interval_s": 4,
        "message": {
            "id": identity,
            "channel_id": "parent",
            "edited_timestamp": None,
            "embeds": [{"footer": {"text": footer}}],
        },
    }


def card_unavailable(turn: Turn, identity: str, seconds: float) -> Message:
    row = card_sample(turn, identity, seconds, "")
    del row["message"]
    row["unavailable"] = "running card readback unavailable: SystemExit"
    return row


def test_other_card_samples_cannot_fill_a_same_card_capture_gap() -> None:
    # Reviewer reproduction (REVIEW-730-e7d753b): card B is read on every poll,
    # but card A's own readbacks failed from +12 to +56.
    turn = observed(1, "card-a")
    turn.done_s = 65
    turn.card_history = [
        card_sample(turn, "card-a", t, "0s" if t < 60 else "60s") for t in (0, 4, 8, 60)
    ]
    turn.card_history.extend(card_unavailable(turn, "card-a", t) for t in range(12, 60, 4))
    turn.card_history.extend(card_sample(turn, "card-b", t, "unchanged") for t in range(1, 65, 4))
    check = evaluate(EDITS_MIN_3, [turn], FakeBackend(), FakeJudge())
    assert check.status == "PENDING" and "card-a capture gap 52.0s" in check.reason, check.reason


def test_failed_early_readbacks_leave_a_leading_gap_that_stays_pending() -> None:
    # Reviewer reproduction: the card is known running from +0, readbacks fail
    # until +52, and dense late capture shows only one transition.
    turn = observed(1, "card")
    turn.done_s = 70
    turn.card_history = [card_unavailable(turn, "card", t) for t in range(0, 54, 4)]
    turn.card_history.extend(
        card_sample(turn, "card", t, f"{t // 10 * 10}s") for t in (54, 58, 62, 66)
    )
    check = evaluate(EDITS_MIN_3, [turn], FakeBackend(), FakeJudge())
    assert check.status == "PENDING" and "card capture gap 54.0s" in check.reason, check.reason


def test_card_never_read_back_stays_pending() -> None:
    turn = observed(1, "card-a")
    turn.done_s = 25
    turn.card_history = [card_sample(turn, "card-a", t, "0s") for t in range(0, 24, 4)]
    turn.card_history.append(card_unavailable(turn, "card-b", 8))
    check = evaluate(EDITS_MIN_3, [turn], FakeBackend(), FakeJudge())
    assert check.status == "PENDING" and "card-b was never read back" in check.reason


def test_each_card_covered_through_its_own_end_can_still_fail() -> None:
    # Card A stops being listed as working after +20; the next poll (+24, card B)
    # bounds its end, so its interval is covered without extending to terminal.
    turn = observed(1, "card-b")
    turn.done_s = 46
    turn.card_history = [card_sample(turn, "card-a", t, "frozen A") for t in range(0, 24, 4)]
    turn.card_history.extend(card_sample(turn, "card-b", t, "frozen B") for t in range(24, 46, 4))
    check = evaluate(EDITS_MIN_3, [turn], FakeBackend(), FakeJudge())
    assert check.status == "FAIL" and "observed 0 successive" in check.reason, check.reason
