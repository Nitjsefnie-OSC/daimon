"""Plain-text "@<bot name>" mentions: the pure matcher behind the per-tenant toggle.

A real mention on Teams mobile means typing part of the name, waiting for the
suggestion list and tapping the bot, and a pasted prompt that says "@Daimon"
is only text. With ``tenants.plain_text_mentions`` on, each adapter treats a
human channel or thread message that names the bot this way like a real
mention. The adapters run this regex before any I/O, so only a matching
message pays the read of the flag.

Matching is case-insensitive and Unicode-aware. The "@" must not follow a
word character or another "@" (so ``me@daimon.example`` never matches) and
the name must not run on into a word character or hyphen ("@Daimonic",
"@Daimon-dev"). Code spans and blocks are not special: a name inside
backticks still matches, which keeps the rule one sentence long for admins.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")


def name_variants(names: Iterable[str | None]) -> tuple[str, ...]:
    """Every name the bot answers to: each name, plus it without a trailing "(...)".

    A deployment named "Daimon (Staging)" still answers to "@Daimon". Longest
    first, so the alternation consumes "Daimon (Staging)" whole before "Daimon".
    """
    variants: set[str] = set()
    for name in names:
        cleaned = (name or "").strip()
        if not cleaned:
            continue
        variants.add(cleaned.casefold())
        short = _TRAILING_PARENTHETICAL.sub("", cleaned).strip()
        if short:
            variants.add(short.casefold())
    return tuple(sorted(variants, key=lambda variant: (-len(variant), variant)))


@lru_cache(maxsize=64)
def _pattern(variants: tuple[str, ...]) -> re.Pattern[str]:
    alternation = "|".join(re.escape(variant) for variant in variants)
    return re.compile(rf"(?<![\w@])@(?:{alternation})(?![\w-])", re.IGNORECASE)


def mentions_by_name(text: str, names: Iterable[str | None]) -> bool:
    """True when ``text`` contains "@<name>" for any of ``names``; False with no names."""
    if "@" not in text:
        return False
    variants = name_variants(names)
    return bool(variants) and _pattern(variants).search(text) is not None


def strip_name_mentions(text: str, names: Iterable[str | None]) -> str:
    """Remove every "@<name>" token (and the blank after it), then trim the ends."""
    if "@" not in text:
        return text
    variants = name_variants(names)
    if not variants:
        return text
    return re.sub(rf"(?:{_pattern(variants).pattern})[ \t]?", "", text, flags=re.IGNORECASE).strip()
