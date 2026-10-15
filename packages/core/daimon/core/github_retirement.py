"""Operator-supplied exceptions for the GitHub App-only migration."""

from __future__ import annotations

import os
import uuid

ACCEPTED_LOSS_ENV = "DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS"


def accepted_loss_agent_ids() -> set[uuid.UUID]:
    """Parse the comma-separated agent UUID allowlist, rejecting malformed input."""
    raw = os.environ.get(ACCEPTED_LOSS_ENV, "")
    if not raw.strip():
        return set()
    try:
        return {uuid.UUID(item.strip()) for item in raw.split(",")}
    except ValueError as exc:
        raise ValueError(f"{ACCEPTED_LOSS_ENV} must contain only agent UUIDs") from exc
