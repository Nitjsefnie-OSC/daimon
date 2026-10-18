"""Private, bounded journey result records."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Literal


def excerpt(html: str) -> str:
    """Return a short plain-text fragment for private evidence."""
    return unescape(re.sub(r"<[^>]+>", "", html))[:300]


@dataclass
class Result:
    mode: Literal["live", "mocked"]
    persona: str
    correlation_id: str
    posted_message_id: str | None
    started_at: str
    ended_at: str
    reply_message_ids: list[str]
    reply_excerpt: str
    classification: Literal["allowed", "refused", "timeout", "error"]
    staging_turn: str | None = None
    action_titles: list[str] = field(default_factory=list[str])

    def write(self, directory: Path) -> Path:
        """Write this result under a private directory."""
        directory = directory.expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        path = directory / f"{self.correlation_id}.json"
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")
        path.chmod(0o600)
        return path


def timestamp() -> str:
    """Return a UTC timestamp for result records."""
    return datetime.now(UTC).isoformat()
