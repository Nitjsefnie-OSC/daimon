"""Real catalog names resolve for agent config without granting runtime access."""

import pytest
from daimon.core.agent_model_names import resolve_model, resolve_models
from mux.contracts.ids import ModelRef


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("opus 5.5", "claude-opus-5-5"),
        ("CLAUDE OPUS", "claude-opus-5-5"),
        ("opus", "claude-opus-5-5"),
        ("sonnet", "claude-sonnet-5-5"),
        ("haiku", "claude-haiku-5-5"),
        ("luna", "gpt-6-luna"),
        ("flash", "gemini-3.8-flash"),
        ("gemini 4", "gemini-4-argon"),
        ("argon", "gemini-4-argon"),
        ("lunaa", "gpt-6-luna"),
    ],
)
def test_natural_names_use_the_priced_runtime_catalog(name, model):
    assert resolve_model(name).id == model


def test_real_phrasings_and_b2_availability():
    assert [m.id for m in resolve_models("compare with claude opus and gpt")] == [
        "claude-opus-5-5",
        "gpt-6-luna",
    ]
    assert [m.id for m in resolve_models("3 workers: sonnet, luna, flash")] == [
        "claude-sonnet-5-5",
        "gpt-6-luna",
        "gemini-3.8-flash",
    ]
    try:
        result = resolve_models("use astra, opus 5.5 and gemini 4 argon")
    except ValueError as exc:
        assert '"astra"' in str(exc) and "isn't available" in str(exc)
    else:
        assert [m.id for m in result] == ["gpt-6-astra", "claude-opus-5-5", "gemini-4-argon"]


def test_ambiguous_provider_and_unknown_names_never_pick_a_model():
    choices = tuple(ModelRef(provider="openai", id="gpt-6-" + name) for name in ("luna", "astra"))
    with pytest.raises(ValueError, match="could mean") as error:
        resolve_model("gpt", available=choices)
    assert "luna" in str(error.value) and "astra" in str(error.value)
    with pytest.raises(ValueError, match="isn't a known"):
        resolve_model("nothing-like-a-model")


@pytest.mark.parametrize("name", ["opuss", "sonnett", "sonet", "claud sonnet"])
@pytest.mark.parametrize("reverse", [False, True])
def test_real_family_typos_never_silently_choose_an_older_pricier_model(name, reverse):
    from daimon.core.agent_model_names import agent_catalog, model_name

    models = [entry.model for entry in agent_catalog()]
    if reverse:
        models.reverse()
    family = "opus" if "opuss" in name else "sonnet"
    latest = resolve_model(family, available=models)
    with pytest.raises(ValueError, match="could mean") as error:
        resolve_model(name, available=models)
    assert model_name(latest) in str(error.value)
    assert "Choose a model by name" in str(error.value)


@pytest.mark.parametrize(
    ("typo", "family"), [("opuss", "opus"), ("sonet", "sonnet"), ("lunaa", "luna")]
)
def test_unambiguous_typos_match_the_exact_family_selection(typo, family):
    latest = resolve_model(family)
    assert resolve_model(typo, available=(latest,)) == resolve_model(family, available=(latest,))


def test_typo_in_an_explicit_version_never_chooses_another_version():
    with pytest.raises(ValueError):
        resolve_model("opuss 4.9")
