"""The turn leg a test run selects: one real `TurnSettings` read from the environment.

CI runs the turn-relevant suites twice. The legacy leg is the default and sets
nothing. The mux leg sets ``DAIMON_TURN__PATH=mux`` and
``DAIMON_TURN__CHANNEL_BACKENDS=true``. Fixtures that build turn dependencies
(`daimon.testing.turn_fakes.deps_for`, the parity drivers) take their path and
backend flags from `turn_leg()` instead of hard-coding them, so the environment
moves them with the product settings loader and not around it.

A test that only makes sense on one path pins it explicitly, using
`MUX_LEG` / `LEGACY_LEG` (and the ``*_pinned_mux`` / ``*_pinned_legacy`` name
suffix) so a reader sees the pin and the other leg is not silently converted.
"""

from __future__ import annotations

from daimon.core.config import TurnSettings, load_turn_settings

LEGACY_LEG = TurnSettings(path="legacy")
MUX_LEG = TurnSettings(path="mux", channel_backends=True)


def turn_leg() -> TurnSettings:
    """The selected leg, read fresh so a monkeypatched environment is honoured.

    Reads the process environment only: a developer's ``.env`` must not move a
    test run onto a leg the command line did not ask for.
    """
    return load_turn_settings(_env_file=None)


def is_mux_leg() -> bool:
    """True when the run selected the mux path."""
    return turn_leg().path == "mux"
