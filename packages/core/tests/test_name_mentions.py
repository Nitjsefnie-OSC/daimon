"""Tests for daimon.core.name_mentions: the plain-text "@<bot name>" matcher."""

from __future__ import annotations

import pytest
from daimon.core.name_mentions import mentions_by_name, name_variants, strip_name_mentions


@pytest.mark.parametrize(
    "text",
    [
        "@Daimon what changed?",
        "hey @daimon, what changed?",
        "please check (@DAIMON)",
        "line one\n@Daimon\nline two",
        "@Daimon.",
        "ask `@Daimon` in code too",
    ],
)
def test_mentions_by_name_matches_case_insensitive_name_token(text: str) -> None:
    """The name matches regardless of case and surrounding punctuation."""
    assert mentions_by_name(text, ["Daimon"]), f"{text!r} names the bot and must match"


@pytest.mark.parametrize(
    "text",
    [
        "Daimon without the at sign",
        "mail me@daimon.example",
        "@@Daimon",
        "@Daimonic is a different word",
        "@Daimon-dev is another bot",
        "@Daim",
        "",
    ],
)
def test_mentions_by_name_rejects_near_misses(text: str) -> None:
    """Emails, longer words, hyphenated names and bare names never match."""
    assert not mentions_by_name(text, ["Daimon"]), f"{text!r} must not count as a mention"


def test_mentions_by_name_is_false_without_names() -> None:
    """Empty and missing names are ignored rather than matching a bare "@"."""
    assert not mentions_by_name("@ hello @Daimon", ["", None, "  "]), (
        "no usable name means nothing can match"
    )


def test_name_variants_adds_the_name_without_its_trailing_parenthetical() -> None:
    """A deployment called "Daimon (Staging)" also answers to "@Daimon"."""
    variants = name_variants(["Daimon (Staging)"])
    assert variants == ("daimon (staging)", "daimon"), "both forms must be present, longest first"
    assert mentions_by_name("@daimon do this", ["Daimon (Staging)"]), "the short form must match"
    assert mentions_by_name("@Daimon (Staging) do this", ["Daimon (Staging)"]), (
        "the full form must match"
    )


def test_mentions_by_name_handles_unicode_and_multi_word_names() -> None:
    """Names are escaped, so spaces and non-ASCII letters match literally."""
    assert mentions_by_name("@Dämon Helfer bitte", ["Dämon Helfer"]), (
        "a non-ASCII multi-word name must match"
    )
    assert not mentions_by_name("@Dämonen", ["Dämon"]), (
        "a Unicode word character after the name must block the match"
    )


def test_strip_name_mentions_removes_every_token_and_trims() -> None:
    """The matched names leave the text the way stripping a real mention would."""
    assert strip_name_mentions("@Daimon summarise this, @DAIMON", ["Daimon"]) == (
        "summarise this,"
    ), "every occurrence and the blank after it must go"
    assert strip_name_mentions("@Daimon (Staging) go", ["Daimon (Staging)"]) == "go", (
        "the longest form must be removed whole, not leave '(Staging)' behind"
    )
    assert strip_name_mentions("@Daimon", ["Daimon"]) == "", "a bare name leaves nothing"
    assert strip_name_mentions("mail me@daimon.example", ["Daimon"]) == (
        "mail me@daimon.example"
    ), "non-matching text is returned unchanged"
