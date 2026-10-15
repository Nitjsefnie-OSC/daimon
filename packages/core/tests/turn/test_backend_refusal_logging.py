"""Backend denials identify the failed gate without exposing private diagnostics."""

import json

import pytest
from daimon.core.channel_backend import AgentBackendRefused, BackendUnsupported, check_backend
from daimon.core.scope import ResolvedConfig
from daimon.core.turn.admission import Admission, _log_backend_unsupported, _require_backend_origin
from daimon.core.turn.errors import AdmissionDenied
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.errors import UnsupportedCapability
from mux.profiles import backend_selection
from structlog.testing import capture_logs

from .test_provider_dispatch import REVISION, TENANT


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_disabled_agent_origin_logs_the_gate_without_changing_the_denial(provider):
    with capture_logs() as logs, pytest.raises(AdmissionDenied) as refused:
        _require_backend_origin(backend_selection(provider), "agent", agent_backends=False)
    assert refused.value.reason == "backend_unsupported"
    assert logs == [
        {
            "event": "turn.backend_unsupported",
            "stage": "backend_origin",
            "causes": [],
            "log_level": "info",
        }
    ]


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_supported_agent_origin_adds_no_refusal_log(provider):
    with capture_logs() as logs:
        _require_backend_origin(backend_selection(provider), "agent", agent_backends=True)
    assert logs == []


def test_missing_admission_origin_logs_before_raising():
    with capture_logs() as logs, pytest.raises(AdmissionDenied, match="backend_unsupported"):
        Admission(
            account_id=TENANT,
            agent=ma_agent(),
            environment=ma_environment(),
            config=ResolvedConfig(agent_name="agent", environment_name="default"),
            backend_revision=REVISION,
        )
    assert logs[0]["stage"] == "admission_origin"


def test_workspace_policy_refusal_has_a_structured_reason():
    with capture_logs() as logs, pytest.raises(BackendUnsupported):
        check_backend(REVISION, enabled_backends=("anthropic",))
    assert logs == [
        {
            "event": "turn.backend_unsupported",
            "stage": "workspace_policy",
            "reason": "backend_not_enabled",
            "channel_id": REVISION.channel.channel_id,
            "profile": REVISION.profile,
            "log_level": "info",
        }
    ]


def test_nested_private_errors_log_only_closed_reason_and_capability_labels():
    private = "credential-PRIVATE-value source instructions https://private.invalid"
    native = RuntimeError(private)
    capability = UnsupportedCapability(("source_definition", private), private)
    capability.__context__ = native
    owner = AgentBackendRefused("source_unavailable")
    owner.__cause__ = capability
    denied = AdmissionDenied(reason="backend_unsupported")
    denied.__cause__ = owner
    # Even an unexpected provider reason is not safe merely because it is a string.
    unknown = AgentBackendRefused(private)
    unknown.__cause__ = denied
    with capture_logs() as logs:
        _log_backend_unsupported("preparation_counterpart", unknown)
    assert logs[0]["causes"] == [
        {"type": "AgentBackendRefused", "reason": "redacted"},
        {"type": "AdmissionDenied", "reason": "backend_unsupported"},
        {"type": "AgentBackendRefused", "reason": "source_unavailable"},
        {"type": "UnsupportedCapability", "missing": ["source_definition"]},
        {"type": "RuntimeError"},
    ]
    assert private not in json.dumps(logs)


def test_cyclic_exception_context_cannot_block_a_denial_log():
    error = RuntimeError("private")
    error.__context__ = error
    with capture_logs() as logs:
        _log_backend_unsupported("preparation_counterpart", error)
    assert logs[0]["causes"] == [{"type": "RuntimeError"}]
