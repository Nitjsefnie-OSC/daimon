"""`scrub_other_platforms`: other chat apps' names never reach a person on one app."""

from __future__ import annotations

import time

import pytest
from daimon.core.platform_words import scrub_other_platforms


@pytest.mark.parametrize(
    ("platform", "text", "expected"),
    [
        # Each pair, both ways round.
        ("teams", "Use Discord for that.", "Use another app for that."),
        ("teams", "Use Slack for that.", "Use another app for that."),
        ("discord", "Use Slack for that.", "Use another app for that."),
        ("discord", "Use Microsoft Teams for that.", "Use another app for that."),
        ("discord", "Use Teams for that.", "Use another app for that."),
        ("slack", "Use Discord for that.", "Use another app for that."),
        ("slack", "Use Microsoft Teams for that.", "Use another app for that."),
        ("slack", "Use Teams for that.", "Use another app for that."),
        # Discord is matched in any case.
        ("slack", "ask discord and DISCORD and Discord", "ask other apps"),
        ("slack", "ask in discord", "ask in another app"),
        ("teams", "call discord_send_message", "call another app_send_message"),
        # Possessives and an article.
        ("teams", "Check Discord's API", "Check another app's API"),
        ("teams", "post in a Slack channel", "post in another app channel"),
        ("teams", "post in the Discord server", "post in another app server"),
        # A name opening a sentence leaves a capital.
        ("teams", "Slack is busy.", "Another app is busy."),
        ("teams", "Done. Discord is busy.", "Done. Another app is busy."),
        ("teams", "- Slack\n- Discord", "- Another app\n- Another app"),
    ],
)
def test_other_apps_are_named_another_app(platform: str, text: str, expected: str) -> None:
    assert scrub_other_platforms(text, platform) == expected


@pytest.mark.parametrize(
    ("platform", "text"),
    [
        ("teams", "Never mind, cut me some slack."),
        ("discord", "slack time and a slack rope"),
        ("discord", "Our team and the teams I work with."),
        ("slack", "five teams and one team"),
        ("teams", "Welcome to Teams."),
        ("discord", "Welcome to Discord."),
        ("slack", "Welcome to Slack."),
        ("slack", "notslack.net and teamsters and Slackware"),
        ("teams", "nothing to see"),
        ("teams", ""),
    ],
)
def test_the_current_apps_own_name_and_plain_words_are_kept(platform: str, text: str) -> None:
    assert scrub_other_platforms(text, platform) == text


def test_no_platform_leaves_text_alone() -> None:
    assert scrub_other_platforms("Discord and Slack", None) == "Discord and Slack"
    assert scrub_other_platforms("Discord and Slack", "cli") == "Discord and Slack"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ask Discord or Slack", "Ask another app"),
        ("Ask Slack or Discord.", "Ask another app."),
        ("Ask Discord and Slack", "Ask other apps"),
        ("Ask Discord, Slack", "Ask other apps"),
        ("Ask Discord, or Slack", "Ask another app"),
        ("Ask Discord/Slack", "Ask other apps"),
        ("Ask Discord & Slack", "Ask other apps"),
        ("on Discord or Slack today", "on another app today"),
        ("Ask Discord and Slack's bots", "Ask other apps' bots"),
    ],
)
def test_a_run_of_names_reads_as_one_name(text: str, expected: str) -> None:
    assert scrub_other_platforms(text, "teams") == expected


def test_the_live_staging_failure_is_scrubbed() -> None:
    text = (
        'Nothing here says "Never upload customer data to Discord or Slack." '
        "The only mention of Discord or Slack is in the quoted line."
    )
    assert scrub_other_platforms(text, "teams") == (
        'Nothing here says "Never upload customer data to another app." '
        "The only mention of another app is in the quoted line."
    )


@pytest.mark.parametrize(
    ("platform", "text", "expected"),
    [
        ("teams", "Join https://discord.gg/abc123 now", "Join another app now"),
        ("teams", "See discord.com/channels/1/2.", "See another app."),
        ("teams", "See HTTPS://Discord.COM/x", "See another app"),
        ("teams", "See https://cdn.discordapp.com/attachments/1/a.png", "See another app"),
        ("teams", "Open https://acme.slack.com/archives/C1/p2, then", "Open another app, then"),
        ("discord", "a link: slack.com", "a link: another app"),
        ("discord", "See https://teams.microsoft.com/l/channel/x", "See another app"),
        ("slack", "See https://teams.microsoft.com/l/channel/x", "See another app"),
        ("discord", "See [the docs](https://acme.slack.com/docs 'Docs')", "See another app"),
        ("discord", "See ![shot](https://files.slack.com/a.png)", "See another app"),
        ("discord", "See <https://acme.slack.com/archives/C1|the thread>", "See another app"),
        ("discord", "See <https://acme.slack.com/archives/C1>", "See another app"),
        ("discord", "[Slack](https://example.com/page)", "[Another app](https://example.com/page)"),
        (
            "discord",
            "[docs](https://example.com/slack.com-guide)",
            "[docs](https://example.com/slack.com-guide)",
        ),
    ],
)
def test_links_to_other_apps_are_replaced_whole(platform: str, text: str, expected: str) -> None:
    assert scrub_other_platforms(text, platform) == expected


def test_own_links_are_kept() -> None:
    text = "[docs](https://discord.com/developers) and https://acme.slack.com/x"
    assert scrub_other_platforms(text, "discord") == (
        "[docs](https://discord.com/developers) and another app"
    )


def test_code_is_scrubbed_too() -> None:
    text = "```py\nclient = Slack(token)  # post to Discord\n```\nRun `slack.com`."
    assert scrub_other_platforms(text, "teams") == (
        "```py\nclient = another app(token)  # post to another app\n```\nRun `another app`."
    )


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
def test_scrubbing_is_idempotent(platform: str) -> None:
    text = (
        "Slack or Discord. Discord and Teams, Slack's API at https://x.slack.com/a "
        "[Microsoft Teams](https://teams.microsoft.com/x) a Discord thread. Slack, Discord."
    )
    once = scrub_other_platforms(text, platform)
    assert scrub_other_platforms(once, platform) == once
    # A streamed answer is scrubbed again from the start on every render tick.
    assert all(
        scrub_other_platforms(scrub_other_platforms(text[:n], platform), platform)
        == scrub_other_platforms(text[:n], platform)
        for n in range(len(text))
    )


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
def test_no_other_name_survives(platform: str) -> None:
    names = {"discord": "Discord", "slack": "Slack", "teams": "Teams"}
    text = "Discord, Slack, Teams, Microsoft Teams, discord.gg/x, a.slack.com, Discord or Slack"
    out = scrub_other_platforms(text, platform)
    for other, name in names.items():
        if other != platform:
            assert name not in out
    if platform != "teams":
        assert "microsoft" not in out.lower()


@pytest.mark.parametrize(
    ("platform", "text"),
    [
        # Another host: the URL is kept byte for byte, whatever its path names.
        ("slack", "<https://github.com/discord/discord-api-docs|API docs>"),
        ("slack", "<https://github.com/discord/discord-api-docs>"),
        ("teams", "[docs](https://github.com/discord/x)"),
        ("teams", "[docs](https://github.com/discord/x 'Discord docs')"),
        ("teams", "![logo](https://cdn.example.com/Slack/logo.png)"),
        ("teams", "see https://github.com/discord/discord-api-docs/blob/main/README.md"),
        ("discord", "see https://example.com/Slack/Teams?next=https://slack.example/Teams#Slack"),
        ("discord", "Visit (https://github.com/Teams/x) today"),
        ("teams", "https://github.com/discord/x."),
    ],
)
def test_a_url_on_another_host_is_kept_byte_for_byte(platform: str, text: str) -> None:
    assert scrub_other_platforms(text, platform) == text


@pytest.mark.parametrize(
    ("platform", "text", "expected"),
    [
        (
            "slack",
            "See <https://github.com/discord/x|the Discord docs> now",
            "See <https://github.com/discord/x|Another app docs> now",
        ),
        (
            "teams",
            "See [the Slack docs](https://github.com/slack/x) now",
            "See [Another app docs](https://github.com/slack/x) now",
        ),
        (
            "teams",
            "See [https://github.com/discord/x](https://github.com/discord/x)",
            "See [https://github.com/discord/x](https://github.com/discord/x)",
        ),
        (
            "slack",
            "Discord is at https://github.com/discord/x, Teams at <https://teams.microsoft.com/l>.",
            "Another app is at https://github.com/discord/x, another app at another app.",
        ),
    ],
)
def test_only_the_visible_label_of_a_kept_link_is_scrubbed(
    platform: str, text: str, expected: str
) -> None:
    assert scrub_other_platforms(text, platform) == expected


def test_a_link_on_another_apps_host_is_replaced_even_with_userinfo_tricks() -> None:
    assert scrub_other_platforms("[x](https://discord.com@github.com/a)", "teams") == (
        "[x](https://discord.com@github.com/a)"
    ), "the host is github.com"
    assert scrub_other_platforms("[x](https://github.com@discord.com/a)", "teams") == "Another app"


_ADVERSARIAL = (
    "[" * 100_000,
    "[Discord or Slack](" * 3_000,
    "Discord or Slack or Teams, " * 6_000,
    "Discord" + " " * 50_000 + "x",
    "a " * 25_000 + "Discord",
    "https://" + "a." * 20_000 + "z",
    "x" * 100_000,
    "< " * 20_000 + "<https://",
    "![" + "a" * 100_000,
)


def _seconds(text: str) -> float:
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        scrub_other_platforms(text, "teams")
        best = min(best, time.perf_counter() - started)
    return best


@pytest.mark.parametrize("text", _ADVERSARIAL, ids=range(len(_ADVERSARIAL)))
def test_adversarial_input_scales_linearly(text: str) -> None:
    """Four times the input takes about four times as long, not sixteen."""
    small, large = text[: len(text) // 4], text
    scrub_other_platforms(small, "teams")  # build the patterns outside the timing
    ratio = _seconds(large) / max(_seconds(small), 1e-4)
    assert ratio < 10, f"{ratio:.1f}x for 4x the input"
    assert _seconds(large) < 1.0


def test_two_hundred_kilobytes_of_everything_is_scrubbed_quickly() -> None:
    text = "".join(piece[:22_000] for piece in _ADVERSARIAL)
    assert len(text) > 150_000
    assert _seconds(text) < 1.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hi. Slack", "Hi. Another app"),
        ("Hi, Slack", "Hi, another app"),
        ("Hi.Slack", "Hi.another app"),
        ("a\n\n  - Slack", "a\n\n  - Another app"),
        ("x" * 50 + "\n" + "-" * 30 + " Slack", "x" * 50 + "\n" + "-" * 30 + " another app"),
        ("x" * 50 + ". Slack", "x" * 50 + ". Another app"),
    ],
)
def test_a_sentence_start_is_read_from_a_bounded_look_back(text: str, expected: str) -> None:
    assert scrub_other_platforms(text, "teams") == expected
