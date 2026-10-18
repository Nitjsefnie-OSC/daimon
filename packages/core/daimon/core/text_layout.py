"""Small helpers for bounded platform message layouts."""

from __future__ import annotations

from collections.abc import Iterable


def fit_lines(lines: Iterable[str], *, max_chars: int) -> list[str]:
    """Keep the leading lines whose newline-joined text fits the limit."""
    kept: list[str] = []
    used = -1
    for line in lines:
        used += len(line) + 1
        if used > max_chars:
            break
        kept.append(line)
    return kept
