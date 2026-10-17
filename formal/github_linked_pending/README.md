# Linked GitHub pending-request model

`LinkedPending.tla` asks one question: when a person finishes linking GitHub, can a chat grant request they made earlier disappear with no result in its thread, or can one failing request stop the others? Requests are saved when Connect needs a link (`save_pending`), claimed by the link callback (`take`), handled one at a time, answered in the originating thread, and then deleted (`resolve`). The OAuth intent is already `used` when the callback runs, so a lost request cannot be recovered by replaying the callback.

Run `FORMAL_CHECK_FILTER=github_linked_pending formal/check.sh` from the repository root with the jar specified in [`formal/README.md`](../README.md). The configs run with `-seed 1 -fp 0 -lncheck final` (one TLC worker). `-lncheck final` checks liveness once after the full search; the default checks at timed intervals and makes the count of a liveness counterexample vary between runs.

## Action-to-code map

Paths are under `packages/adapters/mcp/daimon/adapters/mcp/` (`mcp/`) and `packages/core/daimon/core/` (`core/`). Line numbers are approximate and drift; the function names are the contract.

| Model action | Code boundary |
| --- | --- |
| `Save(r)` | `core/stores/github_linked_pending.py` `save` (`:38-70`): inserts a row with `expires_at` ten minutes ahead (`:68`) and no claim. Called through `mcp/tools/github_linked_pending.py` `save_pending` (`:40`) from `github_connect.py` and from `_deliver_recovery` (a relink re-saves a fresh row, not modelled). |
| `Take(w)` | `core/stores/github_linked_pending.py` `take` (`:74-118`): selects this requester's rows with `expires_at > now` and `claimed_until` null or `<= now`, oldest first, `FOR UPDATE SKIP LOCKED` (`:105`), sets `claimed_until = now + CLAIM_LEASE` (`:113`), deletes rows with `expires_at <= now` (`:116`). `mcp/tools/github_linked_pending.py` `_take_pending` (`:73`) runs it in its own committed transaction. Called once per link callback by `resume_pending` (`:262`), from `oauth_github_personal.py` (`:642`). |
| `ProcessOk(w)`, `ProcessFail(w)` | `resume_pending` loop (`:285-297`): `_outcome_message` (`:169`) looks up the agent, rechecks authority and runs `linked_grant`. Any exception is caught and becomes `FAILED_MESSAGE` (`:35`, `:297`) for that request only. |
| `Deliver(w, ok)` | `_deliver_result` (`:97`) inside `try/except Exception` (`:299-301`); a failed post is logged and not retried. |
| `Resolve(w)` | `_resolve_pending` (`:92`) calls `resolve` (`core/stores/github_linked_pending.py:121`); a failure is logged and the row waits for its lease (`:303-306`). |
| `Crash(w)` | Process exit or cancellation anywhere in `resume_pending` after `_take_pending` committed. |
| `LeaseLapse(r)` | `claimed_until <= now`; `CLAIM_LEASE` is two minutes (`core/stores/github_linked_pending.py:16`). |
| `Expire(r)` | `expires_at <= now`; ten minutes after the save (`:68`). |

## Constants and scaling

| Quantity | Model | Source and scaling |
| --- | --- | --- |
| Requests | 2 | Smallest batch where one request can fail while another waits. Order is the take order (`expires_at`, then `id`, `:104`), so request 1 runs first. |
| Workers (link callbacks) | 2, one `take` each | One callback crashes or runs slowly, a second one retakes. Production has one callback per link completion. |
| Lease | `leaseLive` flag, lapses at any time | Two minutes in code; the model does not count time, so a slow worker and a crashed one both look like "lease lapsed". |
| Expiry | `expired` flag, set at any time | Ten minutes in code. Any ordering of expiry, lease and take is explored. |
| Take per worker | 1 | A callback calls `take` once. |

`take`, `resolve` and `save` are each one atomic action: each is one SQL transaction in the code. `ProcessOk`, `ProcessFail`, `Deliver`, and `Resolve` are separate because each is a separate `await` with network or database calls between them.

## Properties

| Property | Meaning |
| --- | --- |
| `NoSilentLoss` (invariant) | A row is never gone unless an outcome was reached and a post was attempted, except when it expired. |
| `NoDoubleProcessing` (invariant) | Two workers compute an outcome for one request only after its lease lapsed. A worker slower than the lease, or a crash between the post and the delete, can still produce a second run; the grant upsert is idempotent and the thread may get a second line. That is allowed and documented, not prevented. |
| `HeldReachesOutcome` (temporal) | A request a worker holds reaches an outcome (a grant result or a failure line) even when another request in the batch fails. Fairness: weak fairness on the worker's own steps (`WorkerStep`); no fairness on `Save`, `Take`, `Expire`, `LeaseLapse`. Checked with `CrashEnabled = FALSE`, because a crashed worker holds nothing and its requests wait for another callback. |

## Results

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `safe` | clean | 173,161 | Fixed design, crash enabled: no silent loss, no double processing under a live lease. |
| `live` | clean | 75,697 | Fixed design, no crash: every held request reaches an outcome under fairness. |
| `delete_on_claim` | violates `NoSilentLoss` | 8 | The old code: `take` deletes rows before any work. |
| `resolve_before_delivery` | violates `NoSilentLoss` | 102 | Deleting as soon as the outcome is known, before the post, leaves a window where a crash loses the message. |
| `take_ignores_lease` | violates `NoDoubleProcessing` | 424 | Without the lease check a second callback takes rows the first is still processing. |
| `abort_batch_on_exception` | violates `HeldReachesOutcome` | 63,969 | An exception ends the loop; later requests get no outcome until something else retakes them. |
| `retake_after_crash_witness` | violates `RetakeAfterCrashNeverFinishes` | 6,760 | Witness: a request claimed by a crashed worker is retaken after the lease and finished. |
| `failure_does_not_block_witness` | violates `FailureNeverUnblocksOthers` | 5,023 | Witness: after request 1 fails, request 2 in the same batch finishes. |
| `delivery_failure_witness` | violates `DeliveryFailureAlwaysVisible` | 303 | Witness of an accepted gap: a failed post is logged, not retried, and the row is deleted, so the thread gets no line. |

## Counterexamples in plain words

1. `delete_on_claim`: two requests are saved. The link callback takes them; `take` deletes both rows. The agent lookup for the first raises, or the process exits. Neither request has an outcome, no row remains, and the thread never hears back.
2. `resolve_before_delivery`: the grant for a request succeeds and its row is deleted. The process exits before the result is posted. The grant exists and the person is never told.
3. `take_ignores_lease`: one callback takes a request and starts processing. A second callback takes the same request while the first lease is still live, and both compute an outcome.
4. `abort_batch_on_exception`: two requests are taken. The first raises. It gets its failure line, but the loop ends; the second stays claimed with no outcome, and nothing else is scheduled to run it.
5. `delivery_failure_witness`: a request succeeds, the post to the thread fails, the failure is logged and the row is deleted. The person sees nothing. This is the one gap the model shows and the design keeps.

## Assumptions

Support: **supported** means a vendor document or the cited code says it; **unsupported** means it is assumed and no staging trace or contract test confirms it.

| Assumption | Basis | Support |
| --- | --- | --- |
| `take` is atomic: two concurrent calls cannot both claim a row; the second skips rows the first locked, or sees `claimed_until` after the first commits. | `FOR UPDATE SKIP LOCKED` at `core/stores/github_linked_pending.py:105`; [PostgreSQL 16, SELECT, locking clause](https://www.postgresql.org/docs/16/sql-select.html#SQL-FOR-UPDATE-SHARE) ("any selected rows that cannot be immediately locked are skipped"). | Supported by the documentation; not replayed against the project's database. |
| Every exception raised while handling one request is caught and converted to a failure line for that request. | `resume_pending` `try/except Exception` (`mcp/tools/github_linked_pending.py:286-297`). `BaseException` (cancellation, `SystemExit`) is not caught and behaves as `Crash`. | Supported by the code. |
| A crash drops all in-flight state and leaves `claimed_until` as written. | Take commits before processing (`_take_pending`, `:73-89`). | Supported by the code. |
| A failed `resolve` behaves like a crash between the post and the delete: the row is retaken after the lease and the request runs again. | `:303-306` comment says the retry is idempotent. | Code supported; idempotence of the whole grant path including the thread message is unsupported (a second line is possible). |
| The agent lookup, the authority recheck and `linked_grant` are one step. | Abstraction; the grant commit is its own serializable transaction (see `formal/github_linked_grant/`). | Unsupported as an abstraction; a crash between the grant commit and the failure/outcome line is treated like a crash after `ProcessOk`. |
| Time is not counted: a lease or expiry can pass between any two actions. | Over-approximation of the 2 and 10 minute constants. | Unsupported as exact timing; sound as an over-approximation. |
| A failed post is never retried. | `_deliver_result` is logged only (`:299-301`). | Supported by the code (`delivery_failure_witness`). |

## Not modelled

Request content, agent identity checks, `_deliver_recovery` and the relink re-save, multiple requesters, more than two requests or callbacks, a callback that calls `take` twice, clock skew between database and process, the browser page text (`REQUEST_CHANGED_MESSAGES`), and the second-installation result. A request that expires while claimed is allowed to be lost: nothing resumes it, and `NoSilentLoss` excludes expiry on purpose.

## Replay status

Provisional. The model has not been replayed against PostgreSQL or the chat platforms. Counterexample tests in `packages/adapters/mcp/tests/tools/test_github_linked_grant.py` (names as of this change; re-check them if the file moves): `test_one_failed_request_does_not_lose_the_rest_of_the_batch` (counterexamples 1 and 4), `test_failure_after_the_grant_keeps_it_resolves_the_row_and_runs_the_next`, `test_a_crash_after_the_claim_leaves_requests_retakeable_after_the_lease` (`retake_after_crash_witness`), `test_expired_requests_are_deleted_not_claimed`. No test covers counterexamples 2 and 3 directly. Whether those tests fail before the fix was not checked here. Upstream staging contract tests: 0 run. Real traces validated: 0.
