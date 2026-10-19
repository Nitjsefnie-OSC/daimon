"""The system prompt a session runs on one platform.

The system prompt is a field of the agent, which every platform shares, and its
hash (`SessionSnapshot.system_sha256`) decides when a session is replaced. The
shipped prompt covers all three chat platforms, so on Teams it would tell the
model about the other two. A Teams session therefore runs a variant, sent as the
session's own `system` override (`agent_with_overrides`) and hashed the same way
(`desired_snapshot`). Discord and Slack sessions keep the agent's own prompt,
byte for byte, so their hash never moves.

The variant swaps known passages of the shipped prompt (`defaults/agents/daimon.yaml`
and `agent_guidance`) for Teams-only wording. An agent whose prompt carries none
of them, such as a tenant's own, has no variant and runs on its own prompt.
"""

from __future__ import annotations

#: (shipped passage, Teams wording). Each passage appears verbatim in the shipped
#: prompt; `test_platform_system` fails when one drifts out of it.
_TEAMS_PASSAGES: tuple[tuple[str, str], ...] = (
    (
        "with the same plain words on every chat platform and no dot separators.",
        "with the same plain words and no dot separators.",
    ),
    (
        """\
You run across more than one chat platform. The `<channel>` element in your
context says which: `platform="discord"`, `"slack"` or `"teams"`, plus the
`id` of the channel you are in right now. Format your output for that
platform:
- Discord: a CommonMark subset — `**bold**`, `*italic*`, `` `code` ``,
  fenced code blocks, `# / ## / ###` headings, `> quote`, `- list`, and
  `[text](url)` links all work.
- Slack: mrkdwn — `*bold*` (single asterisks), `_italic_`, `` `code` ``,
  fenced code blocks, `> quote`, `- list`. Links render as either
  `[text](url)` or `<url|text>`; `#` headings do NOT render.
  To mention a person, write `<@USER_ID>` taking USER_ID from the
  `author_id` attribute on the `<user_query>` element. To link a channel,
  write `<#CHANNEL_ID>` taking CHANNEL_ID from the `id` attribute on the
  `<channel>` element. Both render as clickable names — do NOT print raw
  IDs. Never write `<!channel>`, `<!here>`, or `<!everyone>`.
- Teams: Markdown — `**bold**`, `*italic*`, `` `code` ``, fenced code
  blocks, `- list` and `[text](url)` links. Tables do not render.
Final-reply Markdown tables render as images on Discord unless the tenant
opts out, and as native tables on Slack when the tenant enables them. """
        "Otherwise prefer bullets or code blocks. "
        "Use blank lines for separation rather than horizontal rules (`---`).",
        """\
The `<channel>` element in your context gives the `id` of the channel you are
in right now. Format your output as Markdown: `**bold**`, `*italic*`,
`` `code` ``, fenced code blocks, `- list` and `[text](url)` links. Tables do
not render; prefer bullets or code blocks. Use blank lines for separation
rather than horizontal rules (`---`).""",
    ),
    (
        """\
the channel you are already in, and never
assume a platform: read it from the `<channel>` element.""",
        "the channel you are already in.",
    ),
    (
        """\
In a chat turn on Discord, Slack or Teams,
every file you save under /mnt/session/outputs is attached to your reply
after your turn, except where a Teams `files` attribute (below) says
`unavailable`.""",
        """\
In a chat turn, every file you save under
/mnt/session/outputs is attached to your reply after your turn, except where
the `files` attribute (below) says `unavailable`.""",
    ),
)


def system_for_platform(system: str | None, platform: str | None) -> str | None:
    """The prompt to send for this platform's session instead of the agent's own.

    None when the agent's own prompt is right: every platform but Teams, and an
    agent none of whose passages the Teams wording replaces."""
    if system is None or platform != "teams":
        return None
    variant = system
    for shipped, teams in _TEAMS_PASSAGES:
        variant = variant.replace(shipped, teams)
    return None if variant == system else variant
