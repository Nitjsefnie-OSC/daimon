# Dev lanes

Staging tracks `main`. A lane is separate: a Preview lane deploys a chosen branch on demand, and a fleet lane is for agents.

Daimon cutover work (#179) targets lane/preview only: open each change as a PR against lane/preview, never main; it reaches main only through the final cutover merge.

Push a branch named `lane/<you>` to deploy its current commit:

```sh
git push origin HEAD:lane/yourname
```

From the **Dev lane** GitHub Actions workflow, select that branch and dispatch `deploy`, `status`, `down`, or `reset`. A push deploys automatically. `down` stops the lane. `reset` stops its services, wipes the lane's disposable Anthropic workspace, removes its database volume, then redeploys the recorded image. The workspace must carry the disposable marker used by the contract tests; reset refuses to wipe without it.

Each lane uses its own environment and repository variables for deployment. The host keeps the lane's secrets in a root-owned `.env` file. Normal lane services publish no container ports; the optional preview fixture below binds a loopback port.

## Preview parity MCP fixture (rows 15 and 34)

`preview-parity-mcp` extends the existing QA host's bearer gate. It has no
provider connection or database. `lead_fixture_read(marker: str)` is annotated
`readOnlyHint=true`; `lead_fixture_submit(marker: str)` is annotated
`readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=false` and
`openWorldHint=false`. A submission only appends synthetic evidence; repeated
calls count separately. This checks Daimon's approval plumbing, not internal
lead-intake data or authorization parity.

The profile is disabled by default. **King setup, preview only, after merge:**

- Add a version of GCP project `pymc-daimon-dev` secret `lane-preview-env`,
  preserving all existing keys. Set `COMPOSE_PROFILES=preview-parity` (append to
  existing profiles if any), `DAIMON_PARITY_LANE=preview`, and
  `DAIMON_PREVIEW_PARITY_MCP_TOKEN=<new random bearer of at least 32 characters>`.
  Keep the bearer in that secret and the lane's root-owned `.env`; never put it
  in an artifact, chat, command argument or committed file. Missing/weak tokens
  and lane values other than `preview` refuse startup. Do not add these keys to
  any other lane. The fixture receives only its own token and configuration.
- Enable the approval-test profile in the same preview secret:
  `DAIMON_TOOL_SAFETY__ENABLED=true`,
  `DAIMON_TOOL_SAFETY__TRUSTED_ANNOTATIONS='["parity-bridge"]'`,
  `DAIMON_TOOL_SAFETY__UNATTENDED_WRITES='[]'`. Preserve existing settings for
  restoration; remove any conflicting allow/effect/deny rules on the disposable
  agent. King redeploys preview; this change does not deploy anything.
- Route `preview-parity.136-90-64-248.sslip.io` on the current preview VM
  ingress to host `127.0.0.1:18766` (the profile's only published port, loopback only). Forward
  Authorization and both `/mcp` and `/calls`, without redirecting the MCP POST.
  Provision its TLS certificate; the planned attachment URL is
  **`https://preview-parity.136-90-64-248.sslip.io/mcp`**. It must be reachable
  from the remote model runtime as well as preview's MCP/Discord/scheduler containers; Docker
  DNS or localhost is not an attachment URL. This sslip.io name follows the
  current preview IP; update it if the lane host moves. No ingress or
  certificate is provisioned by this fixture. HTTPS is terminated by the
  preview ingress, never another lane.

Attach to a disposable preview agent with server name **`parity-bridge`**, that
HTTPS MCP URL, and the bearer from **`DAIMON_PREVIEW_PARITY_MCP_TOKEN`**. Use
`request_mcp_token` with the agent's expected MA ID and current origin context,
then submit the token through its private credential form. This creates an
agent-wide attachment; a public `attach_mcp_server` alone cannot authenticate.
Use `get_agent` to verify the saved name and URL and start fresh sessions.

Authenticated `GET https://preview-parity.136-90-64-248.sslip.io/calls` returns
`calls` and `write_count`. Each accepted, bounded `tools/call` request gets a `received`
event (including unknown tools and invalid arguments); a function that runs
also gets an `executed` event containing tool, arguments, UTC time and caller.
Only executed submissions increment `write_count`. The caller `preview-parity`
is the authenticated token principal, not a verified human identity. Correlate
unique `marker` arguments with runner session/routine IDs to identify turns.
The journal contains no bearer. Use synthetic markers only.

The lane-scoped `preview-parity-calls` volume preserves JSONL evidence across
restarts/redeploys; keep one worker. Readback is operator HTTP only, not a third
MCP tool. There is no reset/delete API: record initial counts, filter events by
unique marker and retain evidence before the normal disposable lane teardown
removes volumes. To revoke immediately, stop the fixture or rotate its token
and recreate it; detach obsolete agent credentials too.

Five-step parity procedure (run only after King's merge, setup and redeploy):

1. Verify deployment SHA and attachment on the disposable preview agent. Fetch
   authenticated `tools/list` and `/calls`, saving annotations and baseline
   counts. Use fresh unique markers such as `fixture-<UTC>-15-deny` and
   `fixture-<UTC>-34-blocked`. Capture policy decisions and session/routine IDs.
2. **Row 15, attended:** a QA human asks Preview in an allowed thread to call
   `lead_fixture_read` and then `lead_fixture_submit` with the attended marker.
   The read must execute without approval. The write must display an approval
   prompt with no `received` or `executed` write event and unchanged write count.
   Deny/cancel; confirm no write arrived. In a separate fresh attended turn,
   approve a new marker and verify exactly one executed submission, to prove
   the fixture is callable after approval.
3. **Row 34, unlisted:** with safety enabled, trusted annotations configured,
   and `UNATTENDED_WRITES=[]`, create/run an owned disposable routine requesting
   exactly one submission plus a read control. Verify the approval refusal in decision
   logs and routine result, no human approval wait, no write received/executed,
   and the read executed. Repeat in a fresh MCP-origin agent turn (`ask` or the
   runner's supported MCP entrypoint), preserving origin evidence; it too must
   obey unattended policy. An unavailable server is not a passing refusal.
4. **Row 34, allow-listed:** King changes only preview's
   `DAIMON_TOOL_SAFETY__UNATTENDED_WRITES` to
   `'["parity-bridge/lead_fixture_submit"]'` and recreates the policy-consuming
   preview services. Use fresh sessions/routines and new markers. Repeat the
   routine and MCP-origin cases: each must execute exactly once without an
   approval wait; confirm marker-specific executed events and write-count
   deltas, along with approval/allow-list decisions and terminal results.
5. Save redacted catalogue, approval and decision evidence, routine/MCP results,
   and journal snapshots. Pause/delete fixture routines, cancel pending turns,
   detach the fixture, and have King restore the previous preview safety/profile
   configuration. Stop/disable the fixture and remove its ingress when finished.
   Report row 15 approval and row 34 unattended subchecks separately; this
   stand-in cannot establish correctness of real internal lead/CRM mutations.

## Migration serialization

Run one migrator at a time per database. Alembic does not acquire an application
advisory lock or retry concurrent migrations. A second manual invocation can fail
with an ordinary PostgreSQL DDL conflict; let the first invocation finish, then
retry. The legacy preview stamp repair is part of the same migration transaction.
Main revision IDs receive no repair writes. Only online upgrade and downgrade
may repair legacy preview stamps. Inspection never performs implicit writes,
even on an empty database. Explicit `stamp` and `ensure_version` bookkeeping
retain normal Alembic behavior on empty, main and canonical databases; stored
legacy preview stamps must be upgraded before bookkeeping.

The serializing deployment callers are:

- **Preview/fleet lanes:** the `lane` job in
  [lane.yml](../../.github/workflows/lane.yml#L46) uses concurrency group
  `lane-${{ needs.prepare.outputs.lane }}` with `cancel-in-progress: false`.
  Its Compose init runs one `alembic upgrade head` before starting services.
- **OS staging:** the `deploy` job in
  [deploy.yml](../../.github/workflows/deploy.yml#L66) uses concurrency group
  `deploy-gcp-staging` and waits for the Cloud Run migrate job in the
  [blocking migration step](../../.github/workflows/deploy.yml#L206).
- **Production:** the same deployment job uses `deploy-gcp-production` and the
  same blocking migration step. `cancel-in-progress: false` queues deployments
  rather than interrupting an in-progress migration.

Any deployer or manual operator outside those workflow groups must preserve this
single-migrator invariant, including when starting or retrying Cloud Run jobs.

### Signed page-event watch sidecar

The optional `preview-sidecar` Compose profile runs `daimon-sidecar` and a TLS
proxy, using the lane's immutable commit image and successful migration dependency.
Only the proxy publishes TLS; database, `/healthz` and other paths stay private.
Set `DAIMON_SIDECAR_PUBLIC_HOST` to a separate preview hostname. The default
`localhost` is for local use. `DAIMON_SIDECAR_TLS_PORT` optionally changes the
published TLS port. The proxy image is pinned by digest; the application uses
`DAIMON_LANE_IMAGE` with `DAIMON_LANE_SHA`, as for other lane services.

`DAIMON_WATCHES__ENABLED` defaults to false. Keep it false until the execution-bound
origin-grant prerequisite and watch release are reviewed. Migration `0117_watches`
follows `0116_thread_model_settings`. Every watch send requires its current
unattended execution origin to hold the bound session grant; missing, attended or
mismatched grants refuse dispatch. The execution cleanup, driver gates and attendance
changes do not automatically enable watches. A separate core-owned database
`global` gate also defaults false. Operators must explicitly enable both after the
release gate; the sidecar cannot edit that database control. Disabling either
blocks new events and future sends; disabling the durable gate also cancels queued
wakes. Stop controls stay available while producers are off.

The migration creates `daimon_sidecar` and `daimon_watch_ingress_owner` NOLOGIN
roles. It refuses unsafe existing role attributes/memberships, pins privileged
function lookup to `pg_catalog,pg_temp`, qualifies permanent objects and revokes
PUBLIC execute/schema creation. A narrow non-login function owner has only source
state and queue privileges. Provision a distinct lane login granted only
`daimon_sidecar` and no ownership, schema CREATE, policy or credential-table rights.
Set `DAIMON_SIDECAR__DATABASE_URL` to its local DSN; require TLS across hosts. The
service checks the actual login's effective privileges at startup and refuses
unsafe inherited/PUBLIC table access. Its environment is explicitly allowlisted.

`DAIMON_SIDECAR__SIGNING_KEYS` is a secret JSON map from key IDs to random OS-host
HMAC keys, each at least 32 bytes. Supply the same authorized key to the relevant
OS page host through secret management, outside browser HTML/model context. Only
hashes are stored with registrations. Raw signing keys are not persisted in the
database and do not use the sidecar's credential stores. Initial enrollment binds an existing Discord thread,
page and key; OS page hosts must check page ACLs and CSRF before signing. The core
`sign_page_event` port signs watch/page/key ID, Unix timestamp, server-minted UUID
event ID and raw UTF-8 body digest. Existing page-host enrollment and migration are
separate work; this service provides no public signing route or external sender mode.

POST signed evidence to `/callbacks/watches/{watch_id}` using `X-Page-ID`,
`X-Watch-Key-ID`, `X-Timestamp`, `X-Event-ID` and `X-Signature`. Duplicate/missing
signed headers, altered bytes, wrong bindings, expired/revoked keys, compressed
bodies and timestamps outside five minutes in either direction are refused.
A 202 means acceptance committed; a 409 means that event ID was already accepted.
Retry transport failures with the same event UUID and a fresh signature. Payloads
are limited to 4 KiB and cannot select instructions, owner, agent or destination.
Access logging is disabled; do not enable proxy/body/header debug logging.

The service allows eight active callback requests with a ten-second deadline;
excess work receives 429. A per-process 1,200-request/minute limit also bounds invalid
callbacks before body/database work. Database-enforced accepted-event limits span replicas: 60 events/watch,
120/owner, 300/tenant and 600 globally per minute; 24 queued turns/watch and 100/owner
per rolling day. Registrations allow 25 live watches per owner and at most thirty
days lifetime. Pending batches retain the latest twenty events and an omitted count.
Retained inbox payload quotas are 128 KiB/watch, 1 MiB/owner, 4 MiB/tenant and
16 MiB globally, with 1,000 replay rows/watch and 100,000 globally. Both inbox and
batch content expire after seven days; bounded hourly cleanup runs even with the
producer gate off. Freshness checks prevent old signatures from replaying after
ledger pruning. Quota/rate failures receive 429; temporary database/timeouts 503.

Core stop/key-rotation ports cancel queued wakes and fence later sends. Active
requests already accepted by a provider cannot be undone. Re-enabling requires new
events; cancelled work is never silently replayed. The migration retains delivery
history and has no downgrade. Keep the compatible-reader rollback floor installed.
Woken turns only read and report: safety is forced on, agent/deployment write
allowances are ignored. Before any provider mutation, core validates an explicit
snapshot-field allowlist and refuses skills, multiagent coordinators and unknown
fields, including fields newly recognized by a provider SDK. A single update keeps
only explicitly named, enabled third-party MCP read configs, disables toolset
defaults and makes every retained tool ask the current read/report decision. Operators
must enumerate read names in per-tool configs; default-only toolsets grant nothing.
Shell, built-ins, custom tools, management endpoints and writes are unavailable.
Mounted data and credential vaults remain attached but grant no native execution
or delegation. Original tool arrays restore before the turn releases its session.
No polling, MCP watch-management tools or live internal migration is included.
