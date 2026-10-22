"""Replay the frozen Gemini fixtures inside their seven-day workspace window."""

import time
from datetime import UTC, datetime, timedelta

import pytest

# The pinned native fixtures were recorded at this instant (their snapshots'
# `updated`). Gemini workspaces expire seven days after activity, so against
# the wall clock every replayed session read as expired from 2026-10-17.
FIXTURE_RECORDED_AT = datetime(2026, 10, 10, tzinfo=UTC)


@pytest.fixture(autouse=True)
def gemini_fixture_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    from mux.drivers.gemini import core

    start = time.monotonic()
    anchor = FIXTURE_RECORDED_AT + timedelta(hours=1)
    monkeypatch.setattr(core, "now", lambda: anchor + timedelta(seconds=time.monotonic() - start))
