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

The optional `preview-sidecar` profile runs `daimon-sidecar` and a digest-pinned
TLS proxy. The application uses `DAIMON_LANE_IMAGE` / `DAIMON_LANE_SHA` and waits
for migrations. Only callback paths are public; `/healthz` and the database stay
private. Set `DAIMON_SIDECAR_PUBLIC_HOST` (default `localhost` for local use) and,
optionally, `DAIMON_SIDECAR_TLS_PORT`.

Migration `0117_watches` follows `0116_thread_model_settings`. Keep both
`DAIMON_WATCHES__ENABLED` and the core-owned database `global` gate off until
release review. The sidecar cannot change either gate. Disabling the durable gate
also cancels queued wakes; stop controls remain available with producers off.

Provision a distinct login granted only `daimon_sidecar`, with no ownership,
schema CREATE, policy or credential-table rights. Set its local DSN in
`DAIMON_SIDECAR__DATABASE_URL`; require TLS across hosts. Startup audits the actual
login's inherited/PUBLIC privileges and installed ingress functions. Supply
`DAIMON_SIDECAR__SIGNING_KEYS` through secret management to the sidecar and authorized
OS page hosts, never browsers. The Compose environment is explicitly allowlisted.

See [self-hosting](../../docs/self-hosting.md#signed-page-event-watch-sidecar) for
signing, callback headers, limits, role checks and stop/retention behavior, and
[architecture](../../docs/architecture.md#signed-page-event-watch-entry-point) for
read/report authorization and restoration. The migration has no downgrade: retain
the compatible-reader rollback floor. Do not enable request/header/body logs.
