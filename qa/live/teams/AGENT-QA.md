# Explore Teams QA from a coding agent

Use this guide for a short, evidence-backed staging session. Choose checks from
the current Teams code and compare `admin` with `member`; do not write a live
test script. `smoke --all` is the sole fixed live check.

## Before posting

Follow your team's staging rules. Have the operator confirm that no staging
deploy is in progress before this session posts. Use only the configured QA
team/channel and each persona's own 1:1 chat with the bot. Never post to another
channel, chat, or person. Do not click card actions or open dialogs.

With staging access, configure application default credentials and the secret
name supplied by the operator (keep real names out of reports):

```bash
gcloud auth application-default login
export DAIMON_TEAMS_QA_CONFIG_SECRET=projects/<project>/secrets/<name>
uv run python -m qa.live.teams status --persona admin
```

The `admin` persona owns the configured admin team and may manage routines,
billing, and setup. The `member` persona is an ordinary team member: expect
refusal or fewer controls for admin actions. Check both personas' status before
posting. The driver verifies identities and the configured target on `send`.

## Pick checks from the product code

Read `packages/adapters/teams/daimon/adapters/teams/` before choosing cases:
`http_service.py` registers handlers, `commands.py` parses text, `help.py`
describes commands, and the panel modules define permissions and card actions.
These standalone text commands are code-routed, **zero model turns**:

| Text | What to inspect |
| --- | --- |
| `help` | Available commands; `support` appears only when enabled. |
| `here` | Who answers and what is accessible here. |
| `setup` | Agent roster and setup controls by persona. |
| `routines` | Admin sees `New routine`; member sees only their own routines and no create action. |
| `billing` | Admin sees credit controls; member sees their own usage and fewer controls. |
| `memory` | What the agent remembers for this persona. |
| `privacy` | Personal data controls; inspect only, do not invoke actions. |
| `new` | Starts a fresh conversation; use only when a reset is intended. |
| `support` | Available only when enabled; inspect its card without sending a request. |

Only a bare command word is code-routed. A leading `/` works; `memory` also
accepts specific documented arguments. Other prose, including `routines please`,
is a **model turn**. Free text in the configured QA channel also takes a model
turn and returns a readable thread reply. Keep free text scoped to harmless QA
questions; do not ask the bot to change settings, contact people, or access real
user data. Checks should judge the observed response, not assume every member
command is refused outright.

## Send, wait, read

Send commands to the persona's existing bot 1:1 with `--to chat`. Channel
command replies are private and unreadable through this driver. Each verb prints
one JSON line. Use the returned `message_id` as the wait anchor:

```bash
uv run python -m qa.live.teams send --persona admin --to chat "routines"
uv run python -m qa.live.teams wait --persona admin --after <message_id> --timeout 60
uv run python -m qa.live.teams read --persona admin --from chat --since 5m
```

`send` returns
`{"ok":true,"persona":"admin","to":"chat","message_id":"<message_id>","posted_at":"<time>","chat_id":"<chat_id>"}`.
`wait` and `read` return
`{"ok":true,"persona":"admin","source":"chat","messages":[{"id":"<reply_id>","created":"<time>","text":"...","card_texts":["..."],"action_titles":["Refresh"]}]}`.
Use `card_texts` for card copy and `action_titles` for visible controls. Titles
are evidence only: do not click them. The loop is **send → wait after its ID →
judge the text/card/actions → next check**. Record each reply ID. `read` helps
inspect recent bot messages if a wait is uncertain.

For an approved free-text channel probe, send with `--to channel`, then read
with `--from thread:<message_id>` or wait with that same source and
`--after <message_id>`. Stay in the configured QA channel. The default budget is at most
**5 model turns** and **20 posts** per session unless the operator changes it.
Commands consume posts but zero model turns; free text consumes both. The QA
agent uses the cheap staging model, but your verdict must not depend on a
model-specific phrasing.

## Stop and report

Stop posting on `post_outcome_unknown`: run `read` to investigate, then report
uncertainty; never resend blindly. Stop on `chat_not_quiet`, `reauth_required`, a
reply from an unexpected bot or model, anything that looks like production, a
staging deploy starting (ask the operator), or an action that would affect or
contact a real person. Admin cards such as `billing` list staging users' names
and spend; that is expected and not a reason to stop, but keep those names out
of your report.
For a timeout or other error, preserve the JSON and investigate before any next
post. Do not treat absent or unreadable replies as permission evidence.

Report persona, each sent message ID and exact text, reply IDs, card texts and
action titles, and a verdict for each check (`allowed`, `refused`, or
`unexpected`) with a brief reason. Include total posts and model turns. For
each model turn, include its UTC start/end window so the operator can query
staging logs and verify the model. Keep evidence with real message IDs private;
never put tenant, team, user, secret, or chat identifiers in public reports.

Example (all IDs are placeholders):

| Persona | Sent | Reply evidence | Verdict |
| --- | --- | --- | --- |
| admin | `<admin_post_id>` `routines` | `<admin_reply_id>`; card `Routines`; action `New routine` | allowed |
| member | `<member_post_id>` `routines` | `<member_reply_id>`; card `Routines`; no `New routine` | refused create control |

The fixed smoke check runs four posts and zero model turns (admin/member
`routines` and `billing`):

```bash
uv run python -m qa.live.teams smoke --all --go --results <dir>
```
