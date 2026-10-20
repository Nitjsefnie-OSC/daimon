"""Bounded, deterministic watch projections and comparison rules.

This deliberately small jq-style subset accepts identity, object-field paths
and numeric array indexes. It has no evaluation, subprocess, network, regex,
recursion or expansion operators. A migrated jq expression outside this subset
requires an operator rewrite before enrollment; never silently ignore it.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import JsonValue

MAX_BYTES = 4096
MAX_DEPTH = 16
MAX_NODES = 1024
MAX_PATH = 256
Rule = Literal["change", "added", "updated"]
_TOKEN = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[([0-9]{1,4})\]")


def bounded(value: JsonValue) -> JsonValue:
    """Validate both source and output before traversal or serialization.

    Iterative traversal bounds work even for adversarial in-memory trees.
    Shared subtrees count for every visit; cycles exhaust the depth bound.
    Non-JSON values and non-finite numbers fail closed.
    """
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise ValueError("Watch document exceeds depth or node limit")
        if isinstance(item, dict):
            if len(item) + nodes + len(pending) > MAX_NODES or any(
                not isinstance(key, str) or len(key) > MAX_BYTES  # pyright: ignore[reportUnnecessaryIsInstance]
                for key in item
            ):
                raise ValueError("Watch object exceeds limits")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) + nodes + len(pending) > MAX_NODES:
                raise ValueError("Watch array exceeds limits")
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > MAX_BYTES:
                raise ValueError("Watch string exceeds limit")
        elif item is not None and type(item) not in (bool, int, float):
            raise ValueError("Watch document must be JSON")
        elif type(item) is int and item.bit_length() > 128:
            raise ValueError("Watch integer exceeds limit")
    if len(json.dumps(value, allow_nan=False, ensure_ascii=True).encode()) > MAX_BYTES:
        raise ValueError("Watch document exceeds byte limit")
    return value


def projection_path(expression: str) -> list[str | int]:
    """Parse `.`, `.field.nested`, and `.rows[0].status` only."""
    if expression == ".":
        return []
    if not expression or len(expression) > MAX_PATH:
        raise ValueError("Unsupported watch projection")
    position = 0
    path: list[str | int] = []
    while position < len(expression):
        match = _TOKEN.match(expression, position)
        if match is None:
            raise ValueError("Unsupported watch projection")
        field, index = match.groups()
        path.append(field if field is not None else int(index))
        position = match.end()
        if len(path) > MAX_DEPTH:
            raise ValueError("Watch projection is too deep")
    return path


def project(document: JsonValue, expression: str = ".") -> JsonValue:
    current = bounded(document)
    for part in projection_path(expression):
        if isinstance(part, str) and isinstance(current, dict):
            current = current.get(part)
        elif isinstance(part, int) and isinstance(current, list):
            current = current[part] if part < len(current) else None
        else:
            current = None
    return bounded(current)


def _key(value: JsonValue) -> str:
    # JSON equality preserves boolean/number distinctions, unlike Python ==.
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def evaluate(rule: Rule, previous: JsonValue, current: JsonValue) -> JsonValue:
    """Return firing evidence or None; caller always saves a successful baseline.

    `change` suppresses empty/false results, but zero is meaningful. `added`
    compares full array elements; `updated` compares objects by stable key.
    Deletions alone do not fire. Initial-baseline handling belongs to the
    transaction caller and must distinguish uninitialized from JSON null.
    """
    bounded(previous)
    bounded(current)
    if rule == "change":
        meaningful = current is not None and current is not False and current not in ("", [], {})
        return current if meaningful and _key(previous) != _key(current) else None
    if rule == "added":
        if not isinstance(previous, list) or not isinstance(current, list):
            raise ValueError("added requires array baselines")
        seen = {_key(item) for item in previous}
        result: list[JsonValue] = []
        for item in current:
            key = _key(item)
            if key not in seen:
                result.append(item)
                seen.add(key)
        return bounded(result) if result else None
    if rule == "updated":
        if not isinstance(previous, dict) or not isinstance(current, dict):
            raise ValueError("updated requires object baselines")
        changed = {
            key: value
            for key, value in current.items()
            if key not in previous or _key(previous[key]) != _key(value)
        }
        return bounded(changed) if changed else None
    raise ValueError("Unknown watch comparison rule")
