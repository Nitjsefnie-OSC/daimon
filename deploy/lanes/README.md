# Dev lanes

Staging tracks `main`. A lane is separate: a Preview lane deploys a chosen branch on demand, and a fleet lane is for agents.

Daimon cutover work (#179) targets lane/preview only: open each change as a PR against lane/preview, never main; it reaches main only through the final cutover merge.

Push a branch named `lane/<you>` to deploy its current commit:

```sh
git push origin HEAD:lane/yourname
```

From the **Dev lane** GitHub Actions workflow, select that branch and dispatch `deploy`, `status`, `down`, or `reset`. A push deploys automatically. `down` stops the lane. `reset` stops its services, wipes the lane's disposable Anthropic workspace, removes its database volume, then redeploys the recorded image. The workspace must carry the disposable marker used by the contract tests; reset refuses to wipe without it.

Each lane uses its own environment and repository variables for deployment. The host keeps the lane's secrets in a root-owned `.env` file. No container ports are published by this Compose file.

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
