from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import structlog
from daimon.core.logging_setup import configure_log_level
from daimon.core.turn import lifecycle


async def test_existing_adapter_without_hook_is_safe():
    await lifecycle.acknowledge(object(), "accepted")
    await lifecycle.acknowledge(object(), "done")


@pytest.mark.parametrize("prior_level", ["INFO", "WARNING"])
async def test_signals_and_delivery_failure_are_best_effort(
    monkeypatch: pytest.MonkeyPatch, prior_level: str
) -> None:
    phases: list[str] = []

    class Adapter:
        async def on_acknowledgment(self, phase: str) -> None:
            phases.append(phase)
            if phase == "done":
                raise OSError("reaction permission denied")

    previous_config = structlog.get_config()
    was_configured = structlog.is_configured()
    try:
        configure_log_level(prior_level)
        logger = Mock()

        def get_logger(_name: str) -> Mock:
            return logger

        monkeypatch.setattr(lifecycle, "structlog", SimpleNamespace(get_logger=get_logger))
        adapter = Adapter()
        await lifecycle.acknowledge(adapter, "accepted")
        logger.debug.assert_not_called()

        await lifecycle.acknowledge(adapter, "done")
        logger.debug.assert_called_once_with(
            "turn.acknowledgment_failed", phase="done", error_type="OSError"
        )
        assert phases == ["accepted", "done"]
    finally:
        if was_configured:
            structlog.configure(**previous_config)
        else:
            structlog.reset_defaults()
