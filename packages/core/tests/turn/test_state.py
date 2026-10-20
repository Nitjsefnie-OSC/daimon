from __future__ import annotations

import dataclasses

import pytest
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_text_block import (
    BetaManagedAgentsTextBlock,
)
from daimon.core.errors import TurnError
from daimon.core.turn.state import (
    ContentBlock,
    Task,
    TextBlock,
    ToolUseBlock,
    TurnState,
    extract_final_response,
    extract_sealed_responses,
)


def test_turn_state_defaults_populate_empty_collections_when_constructed() -> None:
    state = TurnState()
    assert state.content == [], "content must default to empty list"
    assert state.tasks == [], "tasks must default to empty list"
    assert state.rate_limit_until is None
    assert state.stop_reason is None
    assert state.error is None
    assert state.seen_event_ids == frozenset(), (
        "seen_event_ids must default to empty frozenset for immutable state"
    )


def test_turn_state_is_frozen_so_reducers_must_return_new_instances() -> None:
    state = TurnState()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.stop_reason = BetaManagedAgentsSessionEndTurn(type="end_turn")  # type: ignore[misc]


def test_text_block_is_discriminated_by_kind_literal_when_instantiated() -> None:
    text: ContentBlock = TextBlock(kind="text", text="hello")
    assert text.kind == "text"
    # pyright narrows on `kind` — this cast is legal:
    if text.kind == "text":
        assert text.text == "hello"


def test_tool_use_block_defaults_status_to_pending_when_built() -> None:
    block = ToolUseBlock(
        kind="tool_use",
        id="tu_1",
        type="agent.tool_use",
        name="bash",
        input={"command": "ls"},
    )
    assert block.kind == "tool_use"
    assert block.status == "pending", "a freshly-observed tool_use event has no result yet"
    assert block.result_content is None
    assert block.is_error is False
    assert block.evaluated_permission is None
    assert block.mcp_server_name is None


def test_tool_use_block_round_trips_complete_status_and_result_when_set() -> None:
    block = ToolUseBlock(
        kind="tool_use",
        id="tu_1",
        type="agent.tool_use",
        name="bash",
        input={},
        status="complete",
        result_content=[BetaManagedAgentsTextBlock(type="text", text="ok")],
        is_error=False,
    )
    assert block.status == "complete"
    assert block.result_content is not None
    assert len(block.result_content) == 1


def test_turn_state_accepts_turn_error_on_error_field_when_upstream_fails() -> None:
    err = TurnError(kind="upstream", message="429")
    state = TurnState(error=err)
    assert state.error is err


def test_task_is_empty_placeholder_kept_for_forward_compat_when_referenced() -> None:
    task = Task()
    assert isinstance(task, Task), "Task is a placeholder; Managed Agents emits no task events yet"


def _tool(tool_id: str) -> ToolUseBlock:
    return ToolUseBlock(kind="tool_use", id=tool_id, type="agent.tool_use", name="bash", input={})


class TestExtractFinalResponse:
    def test_multi_tool_returns_text_after_last_tool_use(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="I'll look that up. "),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            TextBlock(kind="text", text="Let me check another thing. "),
            ToolUseBlock(kind="tool_use", id="tu_2", type="agent.tool_use", name="read", input={}),
            TextBlock(kind="text", text="Here is the answer."),
        ]
        assert extract_final_response(content) == "Here is the answer."

    def test_no_tool_returns_all_text(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="Hello "),
            TextBlock(kind="text", text="world"),
        ]
        assert extract_final_response(content) == "Hello world"

    @pytest.mark.usefixtures("status_log_on")
    def test_a_status_line_before_the_answer_is_left_out(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="🔍 **Looking up:** saturation priors"),
            _tool("tu_1"),
            TextBlock(kind="text", text="📚 **Found:** 14 patterns | top area: diagnostics"),
            TextBlock(kind="text", text="Priors on saturation are usually Beta.\n\nSee below."),
        ]
        assert (
            extract_final_response(content)
            == "Priors on saturation are usually Beta.\n\nSee below."
        )

    @pytest.mark.usefixtures("status_log_on")
    def test_several_status_lines_before_the_answer_are_left_out(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="🔍 one"),
            TextBlock(kind="text", text="📚 two"),
            TextBlock(kind="text", text="The answer."),
        ]
        assert extract_final_response(content) == "The answer."

    @pytest.mark.usefixtures("status_log_on")
    def test_a_short_last_message_is_always_the_answer(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="📚 Found: 14 patterns"),
            TextBlock(kind="text", text="Yes."),
        ]
        assert extract_final_response(content) == "Yes."

    @pytest.mark.usefixtures("status_log_on")
    def test_a_long_message_before_the_answer_stays_and_is_set_apart(self) -> None:
        long_line = "x" * 301
        content: list[ContentBlock] = [
            TextBlock(kind="text", text=long_line),
            TextBlock(kind="text", text="Done."),
        ]
        assert extract_final_response(content) == f"{long_line}\n\nDone."

    @pytest.mark.usefixtures("status_log_on")
    def test_a_multi_line_message_before_the_answer_is_not_a_status_line(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="Plan:\n- one\n- two"),
            TextBlock(kind="text", text="Done."),
        ]
        assert extract_final_response(content) == "Plan:\n- one\n- two\n\nDone."

    @pytest.mark.usefixtures("status_log_on")
    def test_a_short_plain_message_before_the_answer_is_kept_and_set_apart(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="Yes."),
            TextBlock(kind="text", text="The saturation prior is Beta(2, 2)."),
        ]
        assert extract_final_response(content) == "Yes.\n\nThe saturation prior is Beta(2, 2)."

    @pytest.mark.usefixtures("status_log_on")
    @pytest.mark.parametrize("opener", ["Done", "- item", "**Bold**", "1. first", "> quote", "…"])
    def test_a_short_message_not_opening_with_an_emoji_is_never_dropped(self, opener: str) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text=opener),
            TextBlock(kind="text", text="The answer."),
        ]
        assert extract_final_response(content) == f"{opener}\n\nThe answer."

    @pytest.mark.usefixtures("status_log_on")
    def test_a_status_line_with_markdown_after_the_emoji_is_left_out(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="⚠️ **Issue found:** x"),
            TextBlock(kind="text", text="The answer."),
        ]
        assert extract_final_response(content) == "The answer."

    @pytest.mark.usefixtures("status_log_on")
    @pytest.mark.parametrize("emoji", ["🔍", "📚", "⚠️", "🔧", "💻", "✅", "📊", "⏳", "✔️"])
    def test_emoji_openers_with_or_without_a_variation_selector_are_status_lines(
        self, emoji: str
    ) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text=f"  {emoji} working"),
            TextBlock(kind="text", text="The answer."),
        ]
        assert extract_final_response(content) == "The answer."

    @pytest.mark.usefixtures("status_log_on")
    def test_a_variation_selector_after_a_letter_does_not_make_a_status_line(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="A\ufe0f note"),
            TextBlock(kind="text", text="The answer."),
        ]
        assert extract_final_response(content) == "A\ufe0f note\n\nThe answer."

    @pytest.mark.usefixtures("status_log_on")
    def test_a_status_line_that_is_the_last_message_is_kept(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="The answer is below.\nSecond line."),
            TextBlock(kind="text", text="✅ Done"),
        ]
        assert extract_final_response(content) == "The answer is below.\nSecond line.\n\n✅ Done"

    @pytest.mark.usefixtures("status_log_on")
    def test_a_blank_message_before_the_answer_is_left_out(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text=" "),
            TextBlock(kind="text", text="The answer."),
        ]
        assert extract_final_response(content) == "The answer."

    @pytest.mark.usefixtures("status_log_on")
    def test_a_lone_message_is_returned_untouched(self) -> None:
        content: list[ContentBlock] = [TextBlock(kind="text", text="  padded\n")]
        assert extract_final_response(content) == "  padded\n"

    @pytest.mark.usefixtures("status_log_on")
    def test_a_status_line_before_a_tool_is_not_in_the_final_response(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="📚 Found: 14 patterns"),
            _tool("tu_1"),
            TextBlock(kind="text", text="The answer."),
        ]
        assert extract_final_response(content) == "The answer."

    def test_empty_content_returns_empty_string(self) -> None:
        assert extract_final_response([]) == ""

    def test_zero_message_after_tools_returns_empty(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="I'll do that."),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
        ]
        assert extract_final_response(content) == ""

    def test_single_tool_with_final_text(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="Let me check."),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            TextBlock(kind="text", text="Done. The result is 42."),
        ]
        assert extract_final_response(content) == "Done. The result is 42."

    def test_only_tool_blocks_returns_empty(self) -> None:
        content: list[ContentBlock] = [
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            ToolUseBlock(kind="tool_use", id="tu_2", type="agent.tool_use", name="read", input={}),
        ]
        assert extract_final_response(content) == ""


class TestExtractSealedResponses:
    def test_long_text_followed_by_tool_is_returned_with_index(self) -> None:
        answer = "x" * 600
        content: list[ContentBlock] = [
            TextBlock(kind="text", text=answer),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            TextBlock(kind="text", text="Recap."),
        ]
        assert extract_sealed_responses(content, min_chars=500) == [(0, answer)], (
            "a text block sealed by a tool use and >= min_chars should be returned"
        )

    def test_short_text_followed_by_tool_is_excluded(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="Let me check that."),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
        ]
        assert extract_sealed_responses(content, min_chars=500) == [], (
            "narration under min_chars stays suppressed"
        )

    def test_trailing_text_after_last_tool_is_excluded(self) -> None:
        content: list[ContentBlock] = [
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            TextBlock(kind="text", text="y" * 600),
        ]
        assert extract_sealed_responses(content, min_chars=500) == [], (
            "unsealed trailing text is the final response, not a sealed block"
        )

    def test_multiple_sealed_blocks_returned_in_stream_order(self) -> None:
        first = "a" * 700
        second = "b" * 900
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="short narration"),
            ToolUseBlock(kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}),
            TextBlock(kind="text", text=first),
            ToolUseBlock(kind="tool_use", id="tu_2", type="agent.tool_use", name="read", input={}),
            TextBlock(kind="text", text=second),
            ToolUseBlock(kind="tool_use", id="tu_3", type="agent.tool_use", name="edit", input={}),
        ]
        assert extract_sealed_responses(content, min_chars=500) == [(2, first), (4, second)], (
            "every sealed block over the threshold is returned, oldest first"
        )

    def test_empty_content_returns_empty_list(self) -> None:
        assert extract_sealed_responses([], min_chars=500) == []

    @pytest.mark.usefixtures("status_log_on")
    def test_a_status_line_beside_a_sealed_answer_is_left_out(self) -> None:
        answer = "x" * 600
        content: list[ContentBlock] = [
            TextBlock(kind="text", text=answer),
            TextBlock(kind="text", text="🔍 Looking up: saturation priors"),
            _tool("tu_1"),
            TextBlock(kind="text", text="Recap."),
        ]
        assert extract_sealed_responses(content, min_chars=500) == [(0, answer)]

    @pytest.mark.usefixtures("status_log_on")
    def test_consecutive_messages_are_one_candidate_set_apart_by_a_blank_line(self) -> None:
        first = "a" * 250
        second = "b" * 250
        content: list[ContentBlock] = [
            TextBlock(kind="text", text=first),
            TextBlock(kind="text", text=second),
            _tool("tu_1"),
        ]
        assert extract_sealed_responses(content, min_chars=500) == [(0, f"{first}\n\n{second}")]

    @pytest.mark.usefixtures("status_log_on")
    def test_a_short_plain_message_in_a_sealed_run_is_kept(self) -> None:
        answer = "x" * 600
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="Yes."),
            TextBlock(kind="text", text=answer),
            _tool("tu_1"),
        ]
        assert extract_sealed_responses(content, min_chars=500) == [(0, f"Yes.\n\n{answer}")]

    @pytest.mark.usefixtures("status_log_on")
    def test_status_lines_alone_are_not_sealed(self) -> None:
        content: list[ContentBlock] = [
            TextBlock(kind="text", text="🔍 one"),
            TextBlock(kind="text", text="📚 two"),
            _tool("tu_1"),
        ]
        assert extract_sealed_responses(content, min_chars=500) == []
