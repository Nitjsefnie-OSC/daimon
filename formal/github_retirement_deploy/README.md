# GitHub retirement rollout

The one-shot version of migration 0105 could break an in-flight turn: it dropped schema while old MCP and worker processes could still query it. The expand migration now retires the current legacy rows but keeps every old table and column. Old processes can complete reads and writes until their containers refresh. New code ignores late legacy keys and bindings and reads `app_visible` instead of `staged`; a trigger keeps both grant visibility columns consistent for overlapping writers. The expand migration creates `mode='app'` rows for every agent with a retained visible grant, including grants whose installation is temporarily unusable. The same trigger creates or restores that row whenever a later grant becomes visible. The deployed worker and session preparation treat a missing row as legacy.

| Action | Code boundary |
| --- | --- |
| `StartTurn`, `EndTurn` | An old worker may run across the migration gate; `.github/workflows/deploy.yml:206-215,346-355` does not stop it first. |
| `Expand` | `packages/core/alembic/versions/0105_github_app_only.py:23-185` retires current legacy rows, adds `app_visible` and a trigger, and leaves old tables and columns in place. `PreserveModes=TRUE` represents retaining/upserting modes; `BackfillAllRetained=TRUE` represents selecting every `app_visible` grant at lines 150-158. `FALSE` represents the rejected usable-only backfill. |
| `RetireCredentialSessions` | The expand migration resolves every retired credential to one tenant from all tenant-scoped key, repo, skill, mode, and grant rows plus exact derived agent UUID matches against known live sessions. Each scoped source contributes independently; principal links are not ownership evidence. Conflicting or missing evidence stops before writes and names the agent UUID; retirement-preview reports the same set. It marks the affected agents' live sessions and unknown-identity live sessions in their resolved tenants, recording each original session ID and marking time in `github_retirement_session_audit` in the same transaction. Current `session_preparation.py` checks the marker before reuse and skips copying the old workspace on replacement (`:426`). During the rollout window, an old-image process may reuse a marked session when its legacy budget metadata read fails (`d95cef53d:session_preparation.py:795-818`) or return a marked active session for reuse (`:920-926`). Repeated metadata failures allow repeated reuse until that process refreshes. The marker persists, and new code replaces the session on its next turn. This session boundary is replayed in the database tests and is outside the rollout order model. |
| `OneShot` | Rejected version of 0105 dropped the old tables and the `staged` and `activation_status` columns before replacement processes started. |
| `DeployMcp`, `DeployWorker` | `.github/workflows/deploy.yml:212-215,346-355` updates MCP and workers after 0105. |
| `Contract` | **Later schema-removal migration, absent from this PR.** After every MCP and worker process is on the new image and old turns have drained, repeat the exact-UUID loss guard under table locks; remove any late legacy key, binding, mode, token-file, skill-credential and staged-grant rows; then drop the old tables, `staged`, `activation_status`, the visibility trigger and function, the audit table after the release checks, and the retired credential-request kinds. Keep `app_visible` as a regular true-default column for new readers. |
| `OldSchemaRead` | The deployed base revision `d95cef53d` reads `AgentGitHubGrant.staged` in `packages/core/daimon/core/stores/github_access.py:142` and `GitHubConnectInvitation.activation_status` in `stores/github_connect.py:598,659`. |
| `OldGrantWrite` | `d95cef53d:packages/core/daimon/core/stores/github_access.py:375-377,447-486` treats a missing mode as legacy and writes `staged=TRUE`; 0105's visibility trigger then sets `app_visible=FALSE`. An App mode row makes that old write keep the grant visible. |
| `InstallationRecovers` | `packages/core/daimon/core/stores/github_app_installations.py:65-89` refreshes `suspended_at` and `repo_full_names` without changing the grant. `packages/core/daimon/core/stores/github_access.py:134-154` then accepts a formerly unusable visible grant. The model enables its old-worker write after recovery. |
| `NewGrant` | New code in `packages/core/daimon/core/stores/github_access.py:447-473` can write a grant after MCP refresh while old workers still run. The 0105 trigger at `packages/core/alembic/versions/0105_github_app_only.py:85-106` atomically upserts `mode='app'` when the grant becomes visible. `UpsertModeOnGrant=FALSE` is the rejected trigger without that step. |
| `OldNewGrantWrite` | The old `stage_grant` at `d95cef53d:packages/core/daimon/core/stores/github_access.py:447-485` reads the new grant owner's mode and assigns `staged`; this can hide the new grant when the mode row is missing. Old session preparation also selects its legacy path for a missing mode at `d95cef53d:packages/core/daimon/core/session_preparation_stages.py:322`. |

`CurrentOrder` violates `NoOldSchemaReadFailure` in 18 distinct states: migration contracts the schema, then an old process reads it. `DeletesModes` violates `NoLiveGrantHidden` in 18 states: expansion deletes the App mode row, then an old-worker grant write stages a live grant. `NoGrantModeUpsert` violates `NoLiveGrantHidden` in 33 states: a new MCP grant has no mode row, then an old worker stages it. `UsableOnlyBackfill` violates `NoLiveGrantHidden` in 42 states: an initially unusable grant receives no mode, its installation recovers, then an old worker stages it. `PhasedOrder` checks clean in 40 states: expansion gives every retained visible grant App mode, new visible grants upsert it, and contraction waits until MCP and workers are new and the old turn is idle. The model has one schema, one MCP revision, one worker revision, one turn, one existing grant and one initially absent grant for a different agent; the existing grant's initial mode can be App, legacy or missing, and its installation can be usable or unusable. There are no scaled numerical constants. The post-0105 migration test replays old reads and inserts, installation recovery, old mode-dependent grant writes, a new grant after migration, a new update of an old staged row, and the trigger's visibility result. The later schema-removal migration is specified here but not implemented or deployed; the model assumes its process-version and turn-drain gate is enforced. No live deploy trace has been replayed.

## Release gate

After the migrate job, finish the MCP update and worker refresh. Run the
read-only image checks with the deploy's exact image reference. Set `REGION`,
`ZONE`, `AR_REPO` and `EXPECTED_DAIMON_IMAGE` to that run's values on the
deploy runner:

```bash
set -e
mcp_revisions=$(gcloud run services describe "${AR_REPO}-mcp" --region "$REGION" --format=json |
  jq -r '.status.traffic[] | select((.percent // 0) > 0 or .tag != null) | .revisionName')
test -n "$mcp_revisions"
while IFS= read -r revision; do
  image=$(gcloud run revisions describe "$revision" --region "$REGION" \
    --format='value(spec.containers[0].image)')
  printf '%s %s\n' "$revision" "$image"
  test "$image" = "$EXPECTED_DAIMON_IMAGE" || exit 1
done <<< "$mcp_revisions"
gcloud compute ssh "${AR_REPO}-workers" --zone "$ZONE" --tunnel-through-iap
```

In that SSH shell, set `EXPECTED_DAIMON_IMAGE` to the same reference:

```bash
set -e
cd /etc/daimon
expected_id=$(sudo docker image inspect "$EXPECTED_DAIMON_IMAGE" --format '{{.Id}}')
containers=$(sudo docker compose -f compose.worker.yml ps -q)
test -n "$containers"
for container in $containers; do
  image_id=$(sudo docker inspect "$container" --format '{{.Image}}')
  name=$(sudo docker inspect "$container" --format '{{.Name}}')
  printf '%s %s\n' "$name" "$image_id"
  test "$image_id" = "$expected_id" || exit 1
done
```

Every printed MCP revision must show `EXPECTED_DAIMON_IMAGE`; every running
worker must show `expected_id`. After in-flight turns drain, run these
read-only queries. The first finds every recorded outcome, regardless of
reason, for a turn that started on a marked original session after its mark:

```sql
SELECT a.thread_session_id, a.ma_session_id, a.marked_at,
       o.id AS turn_outcome_id, o.started_at, o.reason
FROM github_retirement_session_audit AS a
JOIN turn_outcomes AS o
  ON o.session_id = a.ma_session_id AND o.started_at > a.marked_at
ORDER BY a.thread_session_id, o.started_at;
```

The second lists marked original mappings still live on the same session:

```sql
SELECT a.thread_session_id, a.ma_session_id, a.marked_at
FROM github_retirement_session_audit AS a
JOIN thread_sessions AS s ON s.id = a.thread_session_id
WHERE s.status = 'live' AND s.ma_session_id = a.ma_session_id
ORDER BY a.thread_session_id;
```

Expected: zero rows from each query. The second query shows replacement still
pending, not reuse. `turn_outcomes.started_at` is captured at turn creation
(`packages/core/daimon/core/turn/outcomes.py:92-94`), copied at finish
(`:264-276`), and inserted later (`:325-330`); insertion time is not used.
Only after the image checks and both queries pass is the old-process window
closed.
The outcome writer is best effort (`turn/outcomes.py:47-60,325-330`): the
first query classifies recorded turns exactly but cannot detect an unrecorded
turn.
