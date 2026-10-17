# Linked GitHub pending-request model

`LinkedPending.tla` asks one question: when a person finishes linking GitHub, can a chat grant request they made earlier disappear with no result in its thread, be answered twice while a claim is held, or leave a recovery half done? Requests are saved when Connect needs a link (`save_pending`), claimed by the link callback (`take`) with a token and a lease, renewed before each request, handled one at a time, answered in the originating thread, and then deleted (`resolve`). The OAuth intent is already `used` when the callback runs, so a lost request cannot be recovered by replaying the callback.

Run `FORMAL_CHECK_FILTER=github_linked_pending formal/check.sh` from the repository root with the jar specified in [`formal/README.md`](../README.md). The configs run with `-seed 1 -fp 0 -lncheck final` (one TLC worker). `-lncheck final` checks liveness once after the full search; the default checks at timed intervals and makes the count of a liveness counterexample vary between runs.

The model matches the code at the PR head: claim token, per-request renewal, resolve on id and token, atomic recovery, and expiry cleanup that removes a claimed row only once its lease lapsed `CLAIMED_GARBAGE_AFTER` (one hour) ago (`stores/github_linked_pending.py`, `take`). `safe` models that cleanup as deleting never-claimed expired rows only. The two agree under one timing assumption, marked unsupported below: a live holder is never an hour past its lease, because it renews before each request and every GitHub call has a timeout. The hour only clears rows a crashed holder left. `lapsed_cleanup` shows what happens without the grace window (counterexample 1).

## Action-to-code map

Paths are under `packages/adapters/mcp/daimon/adapters/mcp/` (`mcp/`) and `packages/core/daimon/core/` (`core/`). Line numbers are approximate and are those of `9441d430d`; the Python was being changed while this model was revised, so the lines in the last column of the re-check list below must be re-read. Function names are the contract.

| Model action | Code boundary at `9441d430d` | Design the model follows |
| --- | --- | --- |
| `Save(r)` | `core/stores/github_linked_pending.py` `save` (`:38-70`): row with `expires_at` ten minutes ahead (`:68`), no claim. `mcp/tools/github_linked_pending.py` `save_pending` (`:40`) from `github_connect.py` and from `_deliver_recovery` (`:113-160`). | unchanged |
| `Take(w)` | `take` (`:74-118`): selects the requester's rows with `expires_at > now` and `claimed_until` null or `<= now`, oldest first, `FOR UPDATE SKIP LOCKED` (`:105`), sets `claimed_until` (`:113`), then deletes **every** row with `expires_at <= now` (`:116`). `_take_pending` (`:73`) runs it in its own committed transaction; `resume_pending` (`:262`) calls it once, from `oauth_github_personal.py` `github_callback` (`:642`). | Also writes a fresh claim token. Cleanup deletes by the `ExpiryCleanup` rule: `"all"` is `9441d430d`, `"lapsed"` is the specified design, `"unclaimed"` is the fix the model supports. |
| `Start(w)` | none | Before each request: renew `claimed_until` conditional on the worker's token. No row or another token: skip, no delivery (`renew_skip_witness`). |
| `ProcessOk`, `ProcessRecovery`, `ProcessFail` | `resume_pending` loop (`:285-297`): `_outcome_message` (`:169`) looks up the agent, rechecks authority, runs `linked_grant`; `needs_install`, `needs_link`, `relink` call `_deliver_recovery` (`:245`). Any exception is caught (`:295`) and becomes `FAILED_MESSAGE` for that request only. | unchanged |
| `RecoveryTx(w)` | `_deliver_recovery` (`:113-160`) mints a link and calls `save_pending` in one `session.begin()`; the original is resolved later by the loop (`:303`). | One transaction saves the replacement and resolves the original on id and token; if the resolve matches nothing the transaction rolls back and the worker skips. The match requirement is an assumption (below). |
| `SaveReplacement(w)` | same lines | `9441d430d` shape (`nonatomic_recovery`): the replacement commits alone and the original waits for the loop's resolve. |
| `PrivateDeliver(w)` | `_post_personal_link` at the end of `_deliver_recovery` (`:155`); failure is caught (`:246-251`). | Attempted after the recovery transaction. |
| `Deliver(w, ok)` | `_deliver_result` (`:97`) inside `try/except Exception` (`:299-301`); a failed post is logged and not retried. | unchanged |
| `Resolve(w)` | `_resolve_pending` (`:92`) calls store `resolve` (`:121`), which deletes by id only; a failure is logged (`:303-306`). | Deletes on id and token. |
| `Crash(w)` | Process exit, cancellation or a `BaseException` anywhere in `resume_pending` after `_take_pending` committed. | The callback is awaited inline in the browser request (`oauth_github_personal.py:642`), so the person sees a failed page: `crashVisible`. **Code fact to re-verify** after the Python change, and against a real run. |
| `LeaseLapse(r)` | `claimed_until <= now`; `CLAIM_LEASE` two minutes (`core/stores/github_linked_pending.py:16`). | Renewal restarts it. |
| `Expire(r)` | `expires_at <= now`; ten minutes after the save (`:68`). | unchanged |

Re-check after the Python change: `take` (claim token column, the cleanup `WHERE`), the new renewal call and where it sits relative to `_outcome_message`, `resolve` (token argument, row count use), `_deliver_recovery` (one transaction, what happens when the resolve deletes zero rows, order of private delivery and the thread line), the `resume_pending` loop (skip on failed renewal, whether a skipped request still posts anything), and `github_callback` (`:642`, that the exception reaches the browser).

## Constants and scaling

| Quantity | Model | Source and scaling |
| --- | --- | --- |
| Requests | 2 | Smallest batch where one request can fail, be skipped or expire while another runs. Order is the take order (`expires_at`, then `id`, `:104`), so request 1 runs first. |
| Workers (link callbacks) | 2, one `take` each | One callback crashes or runs slowly, a second one retakes. A worker's one take is one fresh token, so the token is the worker's name. Production has one callback per link completion. |
| Lease | `lease` none, live or lapsed; lapses at any time | Two minutes in code; time is not counted, so a slow worker and a crashed one both look like "lease lapsed". |
| Expiry | `expired` flag, set at any time | Ten minutes in code. Any ordering of expiry, lease and take is explored. |
| Replacement rows | a flag, `replSaved` | The replacement is a fresh pending row for the next link callback; its own life is not modelled. |
| Take per worker | 1 | A callback calls `take` once. |

`take`, `resolve`, the recovery transaction and `save` are each one atomic action: each is one SQL transaction in the code. `Start`, `Process*`, `PrivateDeliver`, `Deliver` and `Resolve` are separate because each is a separate `await` with network or database calls between them. The renewal and the start of processing are one action; a lease cannot lapse between them in production because the renewal just set it.

## Properties

| Property | Meaning |
| --- | --- |
| `NoSilentLoss` (invariant) | A row is never removed unless its request reached an outcome **and** a post to the person was attempted, **or** it expired without ever being claimed, **or** a worker that held it unfinished died or let an exception escape (the person saw a failed browser request), **or** the recovery transaction replaced it with a fresh pending row. One further clause is the state form of "will attempt": a live worker is past its renewal for the request and has not posted yet; `live` checks that this window closes (`HeldReachesOutcome`, `OutcomeReachesAttempt`). There is no clause for expiry in general. A row whose holder is alive but has not started it is **not** covered, because the renewal would fail and the worker would skip it. |
| `NoDuplicateDelivery` (invariant) | Two thread posts for one request only if its lease lapsed while a live worker was working on it, or after a post was tried (a crash or slow resolve between the post and the delete). Both are accepted and documented. A stale worker whose token no longer matches skips, and a second take never returns a live claim. |
| `CrashLeavesRetakeable` (invariant) | A worker's death or escaped exception never deletes the request it held: until the request expires, the row stays for a later take, or the recovery transaction replaced it, or it was answered. |
| `RecoveryAtomic` (invariant) | The original and its replacement are never both present, and the original is never gone with no replacement and no post attempt. |
| `HeldReachesOutcome` (temporal) | A request a worker holds reaches an outcome (a grant result, a recovery decision or a failure line) even when another request in its batch fails. Fairness: weak fairness on the workers' own steps (`WorkerStep`); none on `Save`, `Take`, `Expire`, `LeaseLapse`. Checked with `CrashEnabled = FALSE`, because a dead worker holds nothing. |
| `OutcomeReachesAttempt` (temporal) | An outcome is followed by a post attempt. Same fairness and bound. |

## Results

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `safe` | clean | 789,113 | Fixed design with cleanup limited to never-claimed rows: token claim and renewal, token resolve, atomic recovery, with crash, lease lapse, expiry and a second take. |
| `live` | clean | 262,137 | Same design, no crash: every held request reaches an outcome and then a post attempt under fairness. |
| `lapsed_cleanup` | violates `NoSilentLoss` | 245 | The cleanup rule as specified (expired and (unclaimed or lease lapsed)) deletes a request that a live slow worker still holds unstarted. |
| `expiry_deletes_claimed` | violates `NoSilentLoss` | 84 | Cleanup deletes every expired row, the code at `9441d430d`: a request under a live claim is destroyed. |
| `delete_on_claim` | violates `CrashLeavesRetakeable` | 26 | The old take: deletes the rows it returns and has no renewal, so a crash destroys the requests. |
| `abort_batch_on_exception` | violates `HeldReachesOutcome` | 529 | The old loop exactly: `DeleteOnClaim` plus an exception that leaves the loop. See the note below the table. |
| `resolve_before_delivery` | violates `CrashLeavesRetakeable` | 777 | Deleting as soon as the outcome is known, before the post, leaves a window where a crash loses the message. |
| `take_ignores_lease` | violates `NoDuplicateDelivery` | 16,877 | Without the lease check a second callback takes rows the first is still processing. |
| `renew_ignores_token` | violates `NoDuplicateDelivery` | 23,137 | Renewal by id lets a worker that lost its claim keep going and post a request another worker also holds. |
| `resolve_ignores_token` | clean | 898,425 | Not a violation: resolve by id alone does not cause a duplicate post or a loss in this model. See the note below the table. |
| `nonatomic_recovery` | violates `RecoveryAtomic` | 289 | The code at `9441d430d`: the replacement commits first and the original stays, so both exist. |
| `retake_after_crash_witness` | violates `RetakeAfterCrashNeverFinishes` | 4,538 | Witness: the same request a crashed worker held is answered (outcome and post) by a second take after the lease. |
| `failure_does_not_block_witness` | violates `FailureNeverUnblocksOthers` | 12,730 | Witness: after request 1 fails, request 2 in the same batch finishes. |
| `delivery_failure_witness` | violates `DeliveryFailureAlwaysVisible` | 617 | Witness of an accepted gap: a failed post is logged, not retried, and the row is deleted, so the thread gets no line. |
| `recovery_commit_witness` | violates `RecoveryNeverCommits` | 242 | Witness: the recovery transaction commits, leaving the original gone and the replacement present. |
| `renew_skip_witness` | violates `SkipNeverHappens` | 223 | Witness: a worker whose claim was lost skips the request. |

`abort_batch_on_exception` is the pre-claim code, not a claim-lease variant: `take` deleted the rows it returned (`DeleteOnClaim`), the loop had no per-request `try`, so an exception left it. The failing request gets no outcome and the rest of the batch is abandoned with its rows already deleted. The exception reaches the browser, so `NoSilentLoss` is not violated (the crash clause covers it); what breaks is that held requests never reach an outcome.

`resolve_ignores_token` is clean because a resolve always follows that worker's own post attempt. A stale worker that deletes a row another worker holds only removes a request that already has a post. The other worker then skips (its renewal finds no row) or has already started and posts a second time, exactly as it would with a token check. In this model the token on resolve changes which worker wins, not safety; the token check that prevents duplicates is on the renewal (`renew_ignores_token`). Keep the token on resolve if the design wants a stale worker unable to touch a new holder's row, but do not cite this model as the reason. If the design has another reason for it, the model does not cover it.

## Counterexamples in plain words

1. `lapsed_cleanup` (the specified cleanup rule): a link callback takes a batch at minute 9; request 2 in it is not started yet because request 1 is slow. Request 2 expires at minute 10 and its lease lapses at minute 11. A second callback's `take` deletes every expired row whose lease lapsed, including request 2. The first callback then reaches request 2, its renewal finds no row and skips it. No outcome, no post, no crash, no row. The state needs a slow request plus a second callback after the lease; it is rare and unlikely to show in tests. `safe` removes it by not deleting any claimed row in cleanup; the code gets the same effect by waiting an hour past the lease, which also clears rows a crashed holder left.
2. `expiry_deletes_claimed` (`9441d430d`): the same, without waiting for the lease: a callback that took a request near its expiry loses the row to any other `take` while the request is under a live claim.
3. `delete_on_claim`: two requests are saved. The callback takes them; `take` deletes both rows. The agent lookup raises or the process exits. No outcome, no row; the person's browser shows an error and the thread never hears back.
4. `abort_batch_on_exception`: two requests are taken and deleted. The first raises; the exception leaves the loop. The browser shows an error; the second request has no outcome and no row.
5. `resolve_before_delivery`: the grant for a request succeeds and its row is deleted. The process exits before the result is posted. The grant exists, the person sees a failed browser page and never gets the line, and nothing can retake the request.
6. `take_ignores_lease`: one callback takes a request and starts it. A second callback takes the same request while the first lease is still live, and both post.
7. `renew_ignores_token`: callback A takes requests 1 and 2 and is slow on 1. The lease lapses. Callback B takes both and starts request 2. A reaches request 2, its renewal by id succeeds on B's claim, and both post for request 2.
8. `nonatomic_recovery`: a request needs a fresh GitHub link. The replacement row commits, then the process exits before the original is resolved. Both rows exist; the next callback takes both and the person gets two recovery links and two lines.
9. `delivery_failure_witness`: a request succeeds, the post to the thread fails, the failure is logged and the row is deleted. The person sees nothing. This is the one gap the model shows and the design keeps.

## Assumptions

Support: **supported** means a vendor document or the cited code says it; **unsupported** means it is assumed and no staging trace or contract test confirms it.

| Assumption | Basis | Support |
| --- | --- | --- |
| No live callback is more than `CLAIMED_GARBAGE_AFTER` (one hour) past its lease, so cleanup of claimed rows only removes rows a crashed holder left. If it fails, cleanup can delete a row a live holder still holds (`lapsed_cleanup`). | Renewal before each request (`resume_pending`) and GitHub call timeouts; nothing bounds batch length formally | unsupported |
| `take` is atomic: two concurrent calls cannot both claim a row; the second skips rows the first locked, or sees the new claim after the first commits. | `FOR UPDATE SKIP LOCKED` at `core/stores/github_linked_pending.py:105`; [PostgreSQL 16, SELECT, locking clause](https://www.postgresql.org/docs/16/sql-select.html#SQL-FOR-UPDATE-SHARE) ("any selected rows that cannot be immediately locked are skipped"). | Supported by the documentation; not replayed against the project's database. |
| The callback runs inline in the browser request, so a crash or an escaped exception shows the person a failed page. | `oauth_github_personal.py` `github_callback` awaits `resume_pending` (`:642`) before it renders the page. | **Code fact, to re-verify** after the Python change and with a real crash. Everything under `crashVisible` rests on it: with no browser signal, `delete_on_claim` and `abort_batch_on_exception` would also violate `NoSilentLoss`. |
| Every exception raised while handling one request is caught and becomes a failure line for that request. | `resume_pending` `try/except Exception` (`:295`). `BaseException` (cancellation, `SystemExit`) is not caught and behaves as `Crash`. | Supported by the code at `9441d430d`. |
| A crash drops all in-flight state and leaves the claim as written. | Take commits before processing (`_take_pending`, `:73-89`). | Supported by the code. |
| The recovery transaction commits only if its resolve deleted a row (id, and token). A mismatch rolls the transaction back and the worker skips. | Design, not in `9441d430d`. If it commits regardless, two workers that both reach recovery each save a replacement. | **Unsupported**; re-check in the new `_deliver_recovery`. |
| Renewal is conditional on the token and succeeds even when the lease has already lapsed, provided no one retook the row. It does not check `expires_at`. | Design, not in `9441d430d`. | **Unsupported**; re-check. |
| A failed `resolve` behaves like a crash between the post and the delete: the row is retaken after the lease and the request runs again. | `:303-306` comment says the retry is idempotent. | Code supported; idempotence of the whole grant path including the thread line is unsupported (a second line is possible, accepted). |
| The agent lookup, the authority recheck and `linked_grant` are one step. | Abstraction; the grant commit is its own serializable transaction (see `formal/github_linked_grant/`). | Unsupported as an abstraction; a crash between the grant commit and the failure or outcome line is treated like a crash after `ProcessOk`. |
| Time is not counted: a lease or expiry can pass between any two actions, except between a renewal and the start of processing. | Over-approximation of the 2 and 10 minute constants. | Unsupported as exact timing; sound as an over-approximation apart from the renewal gap. |
| A failed post is never retried. | `_deliver_result` is logged only (`:299-301`). | Supported by the code (`delivery_failure_witness`). |

## Not modelled

Request content, agent identity checks, `needs_install` recovery (no replacement row; it behaves like the plain path), the life of the replacement row after it is saved (a later callback taking it, its own expiry), multiple requesters, more than two requests or callbacks, a callback that calls `take` twice, clock skew between database and process, cancellation as a separate case from a crash, the browser page text (`REQUEST_CHANGED_MESSAGES`), the second-installation result, and a garbage collector for claimed rows that expired (under `safe` such a row stays; production needs a rule that cannot race a slow holder, which a time bound cannot give without a processing-time limit). Recovery is not explored for the legacy loop (`DeleteOnClaim`).

## Replay status

Provisional. The model has not been replayed against PostgreSQL or the chat platforms. Counterexample tests in `packages/adapters/mcp/tests/tools/test_github_linked_grant.py` (names as of `9441d430d`; re-check them if the file moves): `test_one_failed_request_does_not_lose_the_rest_of_the_batch` (counterexamples 3 and 4), `test_failure_after_the_grant_keeps_it_resolves_the_row_and_runs_the_next`, `test_a_crash_after_the_claim_leaves_requests_retakeable_after_the_lease` (`retake_after_crash_witness`), `test_expired_requests_are_deleted_not_claimed`. No test covers counterexamples 1, 2 and 5 to 8: they need tests for a claimed-and-lapsed expired row surviving another take, renewal by token, resolve by token and the atomic recovery transaction. Whether the existing tests fail before the fix was not checked here. Upstream staging contract tests: 0 run. Real traces validated: 0.
