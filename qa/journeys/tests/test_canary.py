import math

import pytest

from qa.journeys.canary import Haiku55Only, scenario, validate
from qa.live.config import Config, Pricing


def config():
    result = Config(
        pricing=Pricing(per_turn_usd=0.1, judge_input_per_million=0.1, judge_output_per_million=0.5)
    )
    result.staging.model_probe = ["read-only-model-probe"]
    result.staging.deployment_probe = ["read-only-deployment-probe"]
    result.staging.qa_agent_name = "qa-agent"
    return result


def test_canary_has_two_triggers_and_no_judge():
    assert scenario().est_turns == 2
    assert all(a.kind != "judge" for a in scenario().assertions)
    validate(config(), 0.25)


@pytest.mark.parametrize(
    "model", ["claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-sonnet-5", "gpt-6-luna"]
)
def test_old_staging_override_and_other_models_refused(model):
    assert not Haiku55Only().accepts("anthropic", model, "staging")


def test_haiku55_staging_only():
    policy = Haiku55Only()
    assert policy.accepts("anthropic", "claude-haiku-5-5", "staging")
    assert policy.accepts("anthropic", "claude-haiku-5-5-20261001", "staging")
    assert not policy.accepts("anthropic", "claude-haiku-5-5", "prod")
    assert not policy.accepts("openai", "claude-haiku-5-5", "staging")


@pytest.mark.parametrize("budget", [0, -1, 0.3, 0.19, math.nan, math.inf])
def test_invalid_or_insufficient_budget_refused(budget):
    with pytest.raises(ValueError):
        validate(config(), budget)


def test_missing_probes_refused():
    value = config()
    value.staging.model_probe = []
    with pytest.raises(ValueError):
        validate(value, 0.25)
