"""Projection bounds and internal-compatible watch comparison semantics."""

import math

import pytest
from daimon.core.continuity.watch_projection import bounded, evaluate, project, projection_path


@pytest.mark.parametrize(
    "expression", ["", "..", ".[]", ".x | length", "env", ".x()", ".[0]", ".x[-1]", ".x "]
)
def test_projection_refuses_evaluation_and_expansion(expression):
    with pytest.raises(ValueError):
        projection_path(expression)


def test_projection_safe_subset_and_missing_fields():
    value = {"rows": [{"status": "done"}]}
    assert project(value, ".rows[0].status") == "done"
    assert project(value, ".rows[1].status") is None
    assert project(value, ".missing") is None
    assert project(value) == value


@pytest.mark.parametrize(
    "value", ["x" * 4097, "😀" * 1000, [0] * 1025, {"x" * 4097: 1}, math.nan, math.inf, 2**129]
)
def test_projection_output_is_bounded(value):
    with pytest.raises(ValueError):
        project(value)


def test_projection_rejects_deep_input_even_when_selected_output_is_small():
    value = {"ok": 1}
    for _ in range(17):
        value = {"ok": 1, "nested": value}
    with pytest.raises(ValueError):
        project(value, ".ok")


def test_projection_rejects_cycles_without_recursive_traversal():
    value = []
    value.append(value)
    with pytest.raises(ValueError):
        bounded(value)


@pytest.mark.parametrize("empty", [None, False, "", [], {}])
def test_change_suppresses_empty_and_false(empty):
    assert evaluate("change", "old", empty) is None


def test_change_zero_is_meaningful_and_boolean_is_distinct():
    assert evaluate("change", False, 0) == 0
    assert evaluate("change", 1, True) is True
    assert evaluate("change", {"a": 1, "b": 2}, {"b": 2, "a": 1}) is None


def test_added_ignores_removals_and_deduplicates_new_elements():
    assert evaluate("added", [1, 2], [2]) is None
    assert evaluate("added", [1], [1, True, 2, 2]) == [True, 2]


def test_updated_ignores_deletions_and_includes_new_and_changed_keys():
    assert evaluate("updated", {"a": 1, "b": 2}, {"b": 2}) is None
    assert evaluate("updated", {"a": 1}, {"a": True, "b": 2}) == {"a": True, "b": 2}


@pytest.mark.parametrize(
    "rule,previous,current",
    [("added", {}, []), ("added", [], {}), ("updated", [], {}), ("updated", {}, [])],
)
def test_rules_refuse_wrong_baseline_shapes(rule, previous, current):
    with pytest.raises(ValueError):
        evaluate(rule, previous, current)
