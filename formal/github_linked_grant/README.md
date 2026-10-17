# Linked GitHub chat grant model

`GitHubChatGrant.tla` asks whether a chat request can save a grant after link identity, manager authority, or installation coverage changes, including while a lookup or the grant transaction is still in flight. One intent and one request cross the browser, GitHub, and serializable database boundaries. Run `FORMAL_CHECK_FILTER=github_linked_grant formal/check.sh` from the repository root with the jar specified in [`formal/README.md`](../README.md).

Three questions, one per part of the request path:

1. **Link and commit authority** (original): can a forwarded link attach someone else's GitHub account, or a grant commit after the manager, link, or coverage changed?
2. **Coverage removed during the lookup** (`LateCoverageCheck`, `LockRepoCheck`): the lookup finds a repo covered, local coverage is removed while the repo permission GET is outstanding, and the late snapshot then reads the already-removed set. Can the grant commit, and does the commit put the repo back into the installation's `repo_full_names`?
3. **Role read after the transaction's first read** (`RoleRead`): a demotion commits after the grant transaction took its snapshot and before it reads the requester's role. Can the grant still commit on the old role?

## Action-to-code map

Paths are under `packages/adapters/mcp/daimon/adapters/mcp/` (`mcp/`) and `packages/core/daimon/core/` (`core/`). Line numbers are approximate and drift; the function names are the contract.

| Model action | Code boundary |
| --- | --- |
| `Mint`, `Open` | `core/stores/github_personal_links.py` `mint_link` creates a one-use intent; `oauth_github_personal.py` opens it. |
| `PlatformCallback`, `RejectForeign` | `oauth_github_personal.py` verifies the chat identity before GitHub authorization. |
| `GitHubCallback`, `ReplayCallback` | `oauth_github_personal.py` saves a verified link and consumes the intent in one transaction. |
| `Request` | `mcp/tools/github_connect.py` (direct path) and `mcp/tools/github_linked_pending.py` `resume_pending` (after the link) call `linked_grant`. |
| `Discover` | `mcp/tools/github_linked_grant.py` `linked_grant`: `linked_access_token_with_generation` (`:207`, reads the link generation paired with the token), `_installations` (`:217`), `get_app_installation_details` (`:236`). GitHub says the repo is covered. |
| `CheckPermission` | `linked_grant` repo GET at `:257`, then `proposed_linked_grant_policy`. This is the outstanding call coverage can be removed during. |
| `LateSnapshot`, `LateReject` | `linked_grant` `:302-311`: `get_installation`, `originally_present`, `original_names`, and the `installation_covers_repo` early exit (`changed_try_again`). |
| `Begin`, `BeginReject` | `linked_grant` `:314-335`: `SET TRANSACTION ISOLATION LEVEL SERIALIZABLE`, then `get_account_link_for_update`, `get_user_for_update`, `get_for_update` (installation) and the link, generation, and coverage comparison. The first statement fixes the snapshot. |
| `ReadRole`, `RoleDeny` | `manages_now(session)` (`:335`): `github_connect.py` `manages_now` (`:430-458`) and `github_linked_pending.py` `_outcome_message` call `get_account(..., for_share=True)` (`core/stores/accounts.py:74-95`, `SELECT ... FOR SHARE`) and `requester_manages_agent(..., lock_authority=True)` (`core/github_panel.py:97`, `core/channel_admins.py` `read_stored_admin`, which also locks the channel-admin rows). |
| `Commit` | `linked_grant` `:338-400`: `upsert_linked_agent_authorization`, `upsert_github_app` (`core/stores/github_app_installations.py:55-94`, rebuilds `repo_full_names` from the active `TenantGitHubRepo` rows), `enqueue_reconciliation`, `stage_grant`, `clear_exclusion`. |
| `RevokeManager` | `core/stores/accounts.py:98` `set_role` (one `UPDATE` of `Account`); channel-admin removal in `core/stores/channel_admins.py`. Policy is `core/github_panel.py` `requester_manages_agent`. |
| `Unlink`, `Rotate` | `core/stores/github_links.py` unlink and generation rotation. |
| `RemoveCoverage`, `ReconcileInsert` | `core/stores/github_installation_reconciliation.py` `finish` (`:232-342`) replaces the installation's repo set, suspension, and existence. |
| `RevokePermission` | External: GitHub changes the user's repo permission after the GET answered. |

## Bounds and scaling

| Quantity | Model | Why this size |
| --- | --- | --- |
| People | 2 (`manager`, `member`) | Smallest set where one can be a manager of one agent and not another. |
| GitHub users | 2 | A forwarded link needs a second identity. |
| Agents | 2 (`shared`, `own`) | Manager pairs differ per agent. |
| Repos | 2 | The removed repo and a bystander. Removal only targets the requested repo, so a third adds nothing. |
| Installation row | present or absent at the start (both initial states) | The late-snapshot code treats a missing row differently from a row that lost the repo. |
| Requests | 1 | Pending-batch behavior is in `formal/github_linked_pending/`. |
| Link generation | 0 to 2 | One unlink or rotation plus the callback's increment. |

The model collapses the account role, channel-admin rows, and the deployment default into one set `managers` of (person, agent) pairs. `Discover` folds the token read, the installation listing and the installation detail call into one step; the interleavings between those calls are not separate actions because everything they read is rechecked under the transaction's locks. Link, user, and installation rows are locked `FOR UPDATE` from `Begin` on, so `Unlink`, `Rotate`, `RemoveCoverage`, and `ReconcileInsert` cannot run in `begun` or `roleRead`; a writer would wait there. `RevokeManager` is enabled in `begun` always, and in `roleRead` only when the role read takes no lock.

## Properties

| Property | Meaning |
| --- | --- |
| `NoForeignLink` | A saved link belongs to the person who proved the chat identity. |
| `SingleUseLink` | The intent saves at most one link. |
| `GrantRestsOnCurrentAuthority` | At commit the requester is a manager now, the link and generation are the ones the lookup used, and the repo is covered. This is stricter than serializability: a demotion that committed before the grant counts against it, even though "grant, then demotion" is a valid serial order. |
| `NoGrantAfterCoverageRemoval` | No grant is saved for a repo whose coverage was removed after the lookup found it. |
| `CoverageNotRestoredByGrant` | A commit never turns a repo that the installation row did not cover into a covered one. |
| `NoStaleRoleRead` | The role the transaction reads is the requester's current role: no demotion committed between the snapshot and the read. |
| `NoStalePermission` | Checked only in `permission_gap`; GitHub permission is not re-read in the transaction. |
| `EveryRequestResolves` | Every started request ends `done` or `denied`. Weak fairness on the request's own steps (`ProgressStep`), no timeout shortcut, no fairness on the world's actions. |

## Results

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `clean` | clean | 25,926 | All properties above hold within the bounds: identity, single use, coverage, locked role read. |
| `no_platform_identity` | violates `NoForeignLink` | 233 | A forwarded link can attach another person's GitHub account. |
| `no_commit_recheck` | violates `GrantRestsOnCurrentAuthority` | 14,208 | A revoked manager, relink, or coverage change can be accepted. |
| `permission_gap` | violates `NoStalePermission` | 17,220 | GitHub permission can be revoked after its response and before commit. A real, accepted limit. |
| `late_snapshot_sets_only` | violates `NoGrantAfterCoverageRemoval` | 22,380 | The spike's bug: no repo-covered requirement at the late snapshot or under the lock. |
| `lock_check_only` | clean | 30,438 | Witness: the repo-covered check under the installation lock alone is enough. |
| `late_check_only` | clean | 25,926 | Witness: the late-snapshot check plus the lock's name-set comparison is also enough. Same count as `clean` because the lock's repo check adds no reachable behavior. |
| `role_read_unlocked` | violates `NoStaleRoleRead` | 12,519 | A plain role read returns the snapshot's old role after a demotion committed. |
| `role_lock_no_abort` | violates `NoStaleRoleRead` | 12,518 | Locking without PostgreSQL's abort on a row changed since the snapshot would not help. The fix leans on that rule. |

`GrantRestsOnCurrentAuthority` is also violated in both role configs and in `late_snapshot_sets_only`; the table names the first invariant listed in each config. The state counts of the original four configs changed when this model grew (5,435, 140, 1,388 and 1,644 before) because the request now has more stages and the installation row is modelled.

## Counterexamples in plain words

1. `late_snapshot_sets_only`: a manager asks to grant a repo. The lookup finds it covered. While the repo permission GET is outstanding, reconciliation drops the repo from the installation's repo set. The late snapshot reads the set without the repo and records it as the baseline. The transaction compares the baseline with the current set, finds them equal, and commits. `upsert_github_app` then rebuilds `repo_full_names` from active authorization rows, which now include the new one, so the removed repo is covered again. The grant exists and so does the coverage that was removed.
2. `role_read_unlocked`: the grant transaction takes its snapshot while the requester is a manager. The requester is demoted and the demotion commits. The transaction reads the role from its snapshot, still sees manager, and commits a grant for someone who was already demoted when it committed.
3. `role_lock_no_abort`: same, except the transaction holds a share lock on the role row after reading it. The demotion committed before the lock, so the lock changes nothing unless the database aborts the transaction for reading a row changed after its snapshot.

## Assumptions

Support: **supported** means a vendor document or the cited code says it; **unsupported** means it is assumed and no staging trace or contract test confirms it.

| Assumption | Basis | Support |
| --- | --- | --- |
| In a SERIALIZABLE (like REPEATABLE READ) transaction, `SELECT ... FOR SHARE` or `FOR UPDATE` on a row that another transaction updated after this transaction's snapshot fails with a serialization failure instead of returning the old row. | [PostgreSQL 16, transaction isolation, Repeatable Read](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-REPEATABLE-READ) ("would-be updater ... roll back with ... could not serialize access due to concurrent update") and [Serializable](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-SERIALIZABLE) ("works exactly the same as Repeatable Read except ..."). The account read uses `with_for_update(read=True)` (`core/stores/accounts.py:84-90`). | Supported by the documentation; not replayed against the project's database. `role_lock_no_abort` shows what breaks without it. |
| A row held `FOR SHARE` makes a later `UPDATE` of that row wait until the holder commits or rolls back (`UPDATE` takes `FOR NO KEY UPDATE`, which conflicts with `FOR SHARE`). | [PostgreSQL 16, explicit locking, row-level locks](https://www.postgresql.org/docs/16/explicit-locking.html#LOCKING-ROWS). | Supported by the documentation; not replayed. |
| Link, user, and installation rows are locked `FOR UPDATE` in the transaction and the writers that change them take the same row locks, so they wait. | `get_account_link_for_update`, `get_user_for_update`, `get_for_update` in `linked_grant`; writers in `core/stores/github_links.py` and `github_installation_reconciliation.py` were not read line by line for lock use. | Partly supported; writer-side locking unsupported. |
| Commit-time serialization failure only aborts. It never makes a commit succeed that the model forbids. | PostgreSQL SSI documentation (same page). | Supported. |
| `upsert_github_app` rebuilds `repo_full_names` from active authorization rows and leaves those rows as they were when coverage is removed. | `core/stores/github_app_installations.py:55-94`; whether reconciliation deactivates authorizations on removal was not checked. | Code supported for the rebuild; the "rows stay" half is unsupported. |
| A row present with the repo absent is rejected as `changed_try_again` even when the repo was simply never listed locally. | `linked_grant` late snapshot and lock check. | Supported; conservative. |

## Not modelled

Multiple requests, OAuth provider failure, token refresh internals, repo renames, read and write levels, the second-installation restriction (`stage_grant` raising), owner-wide grants (`formal/github_owner_grant/`), installation row deletion after the late snapshot, suspension separate from repo removal, a repo re-covered by reconciliation, serialization failures from rw-dependency cycles (they abort, so they only remove behaviors), and channel-admin versus account-role distinctions inside `managers`. GitHub permission is not re-read in the transaction (`permission_gap`). The platform's own group membership is outside the database and can change after the policy lookup.

## Replay status

Provisional. The model has not been replayed against PostgreSQL or GitHub. Counterexample tests in `packages/adapters/mcp/tests/tools/test_github_linked_grant.py` (names as of this change; re-check them if the file moves): `test_coverage_removed_while_the_repo_permission_get_is_outstanding` for counterexample 1; `test_unlocked_role_read_misses_a_demotion_after_the_first_select`, `test_locked_role_read_fails_the_grant_when_a_demotion_commits_after_the_snapshot` and `test_locked_role_read_waits_for_the_grant_when_the_demotion_comes_later` for 2 and 3, plus `test_direct_tool_demotion_after_the_first_select_changes_nothing` and `test_resume_demotion_after_the_first_select_changes_nothing` for the two entry points. Whether those tests fail before the fix was not checked here. Upstream staging contract tests for GitHub: 0 run. Real traces validated: 0.
