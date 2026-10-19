# Teams QA for coding agents

Hand this file to your coding agent. It covers everything the agent needs to
run a short QA session against Daimon's **staging** Teams bot as two test
personas, `admin` and `member`, and report what it saw. The agent picks the
checks from the current code; `smoke --all` is the only fixed live test.

## 1. Prerequisites

- Your own `@pymc-labs.com` Google account must already be on the Teams QA
  secrets in the `pymc-daimon-staging` project. Only the teammates the Teams QA
  operator granted have that access; if `status` below fails with a permission
  error, ask the operator instead of requesting broader access.
- `gcloud`, `git` and `uv` installed.

Sign in once with your PyMC Labs account (the second command is the one the
QA tool uses):

```bash
gcloud auth login
gcloud auth application-default login
```

## 2. Repo setup

```bash
git clone https://github.com/pymc-labs/daimon.git
cd daimon
uv sync
export DAIMON_TEAMS_QA_CONFIG_SECRET=projects/pymc-daimon-staging/secrets/teams-qa-config
uv run python -m qa.live.teams status --persona admin
uv run python -m qa.live.teams status --persona member
```

`status` prints each persona's token expiry. You need no local files, browser
or Teams sign-in: the config and both personas' token caches live in Secret
Manager. Using the tokens may add a new secret version; that is expected.

## 3. Rules

**Never:**

- print, copy, save or paste token caches, access tokens or secret payloads;
- touch production Daimon or any production tenant, team or chat;
- post anywhere but the configured QA channel and each persona's own 1:1 chat
  with the staging bot, or contact a real person;
- click card actions or open dialogs (action titles are evidence only);
- run `logout` or `login`, or change secrets, IAM, config or agent defaults;
- spend money, create things or change settings through the bot.

**Runs that need no staging claim.** A run qualifies only if all of these hold:
at most 10 posts, every one a code-routed command from the table below, sent by
`admin` or `member` in its own 1:1 with the staging bot; zero model turns; and
you only read the cards (no clicks, spend, creation or other changes). Before
the first post, record the running staging SHA or image, and confirm that no
main or manual deploy is running and that nobody else is changing the QA
fixtures, the admin team or agent defaults. Run only one session at a time per
persona chat. Watch for deploys throughout. If a deploy starts, or a model turn
or any other unexpected side effect happens, stop, don't retry, record the cost
and any writes, and mark the run INCONCLUSIVE. Before calling the run a pass,
check again: record the UTC window and the SHA afterwards, the total posts, and
the turn events you actually observed. The window must be clean. Anything else
(free text, channel posts, config, account or grant changes, card actions)
needs a staging claim and a bounded approval first. A staging claim means the
staging owner has given you a time window; ask the Teams QA operator.

**Cost limits.** Commands cost posts but no model turns. Free text costs a
model turn on the QA team's cheap staging model. Per session: at most **20
posts** and at most **5 model turns**, unless the operator sets other limits.

## 4. Personas

`admin` owns the configured admin team and may manage routines, billing and
setup. `member` is an ordinary team member: expect refusal or fewer controls
for admin actions. Judge the observed response; do not assume every member
command is refused outright.

## 5. Pick checks from the product code

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
is a **model turn**. Keep any approved free text to harmless QA questions; do
not ask the bot to change settings, contact people, or access real user data.

## 6. The verbs

Each verb prints one JSON line and exits non-zero on failure. The loop is
**send → wait after its ID → judge the text, card and actions → next check**.

`send` posts exactly once (never retried) to the persona's 1:1 chat:

```bash
uv run python -m qa.live.teams send --persona admin --to chat "routines"
```

```json
{"ok":true,"persona":"admin","to":"chat","message_id":"<message_id>","posted_at":"<time>","chat_id":"<chat_id>"}
```

`wait` returns once the bot's reply after that message has settled. It skips
the "Working on it…" progress card; on timeout it returns
`{"ok":false,"error":"timeout",...,"in_progress":true|false}`:

```bash
uv run python -m qa.live.teams wait --persona admin --after <message_id> --timeout 60
```

```json
{"ok":true,"persona":"admin","source":"chat","messages":[{"id":"<reply_id>","created":"<time>","modified":"<time or null>","text":"...","card_texts":["..."],"action_titles":["New routine"],"in_progress":false}]}
```

`read` lists recent bot messages in the same shape, for when a wait is
uncertain:

```bash
uv run python -m qa.live.teams read --persona admin --from chat --since 5m
```

Use `card_texts` for card copy and `action_titles` for visible controls.
Commands must go to the 1:1 chat (`--to chat`): in a channel the bot answers
privately and the tool can't read it. For an approved free-text channel probe,
`send --to channel`, then `wait` or `read` with `--from thread:<message_id>`.

## 7. Smoke test

The fixed smoke test posts four commands (admin and member, `routines` and
`billing`) with zero model turns and writes a summary to the results folder:

```bash
uv run python -m qa.live.teams smoke --all --go --results <dir>
```

## 8. When something goes wrong

- `reauth_required`: a persona's sign-in has expired. Stop, and tell the Teams
  QA operator which persona; only the operator can sign it in again. A weekly
  job refreshes both personas, so this should be rare.
- `post_outcome_unknown`: the post may or may not exist. Run `read` to check,
  report the uncertainty, and never resend blindly.
- `chat_not_quiet`: the bot hasn't answered the last post yet. Wait or `read`;
  do not post over it.
- Permission denied on the secrets: your account isn't granted. Ask the
  operator.
- Stop too on a reply from an unexpected bot or model, anything that looks like
  production, a staging deploy starting, or an action that would affect or
  contact a real person. Admin cards such as `billing` list staging users' names
  and spend; that is expected and not a reason to stop, but keep those names
  out of your report.
- For a timeout or other error, keep the JSON and investigate before the next
  post. Absent or unreadable replies are not permission evidence.

## 9. Report

Report persona, each sent message ID and exact text, reply IDs, card texts and
action titles, and a verdict for each check (`allowed`, `refused`, or
`unexpected`) with a brief reason. Include total posts and model turns. For
each model turn, include its UTC start/end window so the operator can check
staging logs for the model used. Keep evidence with real message IDs private;
never put tenant, team, user, secret or chat identifiers in public reports.

Example (all IDs are placeholders):

| Persona | Sent | Reply evidence | Verdict |
| --- | --- | --- | --- |
| admin | `<admin_post_id>` `routines` | `<admin_reply_id>`; card `Routines`; action `New routine` | allowed |
| member | `<member_post_id>` `routines` | `<member_reply_id>`; card `Routines`; no `New routine` | refused create control |
