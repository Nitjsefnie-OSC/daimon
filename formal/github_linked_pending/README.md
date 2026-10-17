# Linked GitHub pending-request model

`LinkedPending.tla` asks one question: when a person finishes linking GitHub, can a chat grant request they made earlier disappear with no result in its thread, be answered twice while a claim is held, or leave a recovery half done? Requests are saved when Connect needs a link (`save_pending`), claimed by the link callback (`take`) with a token and a lease, renewed before each request, handled one at a time, answered in the originating thread, and then deleted (`resolve`). The OAuth intent is already `used` when the callback runs, so a lost request cannot be recovered by replaying the callback.

Run `FORMAL_CHECK_FILTER=github_linked_pending formal/check.sh` from the repository root with the jar specified in [`formal/README.md`](../README.md). The configs run with `-seed 1 -fp 0 -lncheck final` (one TLC worker). `-lncheck final` checks liveness once after the full search; the default checks at timed intervals and makes the count of a liveness counterexample vary between runs. `safe` takes about a minute and `live` under three minutes; every other config is faster.

The model follows the design in the working tree on top of `ac782e183`: claim token, heartbeat of every row the callback holds (`renew_claim`), a per-request deadline (`PER_REQUEST_DEADLINE`, five minutes), resolve on id and token, atomic recovery, and expiry cleanup that removes a claimed row once its lease lapsed `CLAIMED_GARBAGE_AFTER` (one hour) ago (`stores/github_linked_pending.py`, `take`). `safe` uses exactly that cleanup rule. It needs no timing assumption about how long a live callback takes: the deadline and the heartbeat bound it (next section). `lapsed_cleanup` is the same cleanup with no grace window; `no_heartbeat` and `no_deadline` each remove one of the two things the grace window relies on.

## Why the one-hour grace is safe, and how the model counts time

A row's lease is `claimed_until`, set to now plus `CLAIM_LEASE` (two minutes) by `take` and by every renewal. Cleanup may delete an expired row once `claimed_until` is `CLAIMED_GARBAGE_AFTER` in the past, so a row is deletable at an *age* of `CLAIM_LEASE + CLAIMED_GARBAGE_AFTER` (62 minutes) since its last renewal. A live callback renews every row it still holds before each request, and a request cannot run longer than `PER_REQUEST_DEADLINE` (five minutes, plus the failure line's own `FAILURE_DELIVERY_DEADLINE` of 30 seconds). So no row a live callback still holds is more than about five and a half minutes old: far below 62. The module asserts the arithmetic at import (`PER_REQUEST_DEADLINE + FAILURE_DELIVERY_DEADLINE + CLAIM_LEASE < CLAIMED_GARBAGE_AFTER`).

The model counts time in ticks with the same ordering: `Lease` < `Deadline` and `Deadline + Lease` < `Garbage`, with `Lease = 1`, `Deadline = 2`, `Garbage = 4` (the smallest integers that fit the ordering). A row's `age` is the ticks since `claimed_until` was last set; it saturates at `Lease + Garbage = 5`. A request in flight cannot let more than `Deadline` ticks pass: the `Tick` action is disabled for that worker until it acts (finish, fail, or `Timeout`). No tick passes while a worker has a renewal pending (the take, the renewal and the start of the next request follow each other with only database calls between them). With `WithDeadline = FALSE` a request can take any number of ticks, which is the `no_deadline` and `no_heartbeat` configs. Expiry is not counted: `expires_at` can pass at any time, which over-approximates the ten-minute constant.

The clean result is therefore a statement about the design's arithmetic plus the orderings of take, renewal, expiry, cleanup, crash, skip and a second callback, not about a measured callback duration. What it still assumes is listed under Assumptions: the deadline is actually enforced, and the database calls between requests are not slow.

## Action-to-code map

Paths are under `packages/adapters/mcp/daimon/adapters/mcp/` (`mcp/`) and `packages/core/daimon/core/` (`core/`). Line numbers are approximate; the Python was being changed while this model was revised, so the re-check list below must be re-read. Function names are the contract.

| Model action | Code boundary |
| --- | --- |
| `Save(r)` | `core/stores/github_linked_pending.py` `save`: row with `expires_at` ten minutes ahead, no claim. `mcp/tools/github_linked_pending.py` `save_pending` from `github_connect.py` and from `_deliver_recovery`. |
| `Take(w)` | `take` (store): selects the requester's rows with `expires_at > now` and `claimed_until` null or `<= now`, oldest first, `FOR UPDATE SKIP LOCKED`; sets `claimed_until` and a fresh `claim_token`; then deletes expired rows that are never claimed or whose `claimed_until` is older than `CLAIMED_GARBAGE_AFTER`. `_take_pending` runs it in its own committed transaction; `resume_pending` calls it once, from `oauth_github_personal.py` `github_callback` (near `:642`). `ExpiryCleanup`: `"grace"` is that rule, `"lapsed"` deletes at the lease, `"all"` is `9441d430d`. |
| `Start(w)` | `resume_pending` loop, top of each iteration: `_renew_claim` calls store `renew_claim`, one `UPDATE ... WHERE claim_token = :token RETURNING id` that extends every row this claim holds. `RenewException`: the call raises, the loop logs `renew_failed` and goes to the next request. Otherwise a request whose id is not in the returned set is skipped (`claim_lost`). `Heartbeat = FALSE` is the earlier `renew` (current row only). |
| `Tick`, `Timeout(w)` | `asyncio.timeout(PER_REQUEST_DEADLINE)` around `_answer_request` in `resume_pending`; on `TimeoutError` the loop posts the failure line (`FAILED_MESSAGE`) under `FAILURE_DELIVERY_DEADLINE` and goes on to `_resolve_pending`. A deadline that fires while the result is being posted is `Deliver`. |
| `ProcessOk`, `ProcessRecovery`, `ProcessFail` | `_outcome_message`: looks up the agent, rechecks authority, runs `linked_grant`; `needs_install`, `needs_link`, `relink` call `_deliver_recovery`. Any exception in `_answer_request` becomes `FAILED_MESSAGE` for that request only. |
| `RecoveryTx(w)` | `_deliver_recovery`: mints a link, saves the replacement with `save_pending` and calls `resolve(session, pending.id, pending.claim_token)` in one `session.begin()`; a resolve that deletes nothing raises `ClaimLostError` and rolls the transaction back, and the loop skips the request. |
| `SaveReplacement(w)` | The shape at `9441d430d` (`nonatomic_recovery`): the replacement commits alone and the original waits for the loop's resolve. |
| `PrivateDeliver(w)` | `_post_personal_link` at the end of `_deliver_recovery`; failure is caught. |
| `Deliver(w, ok)` | `_deliver_result` inside `_answer_request`; a failed post is logged and not retried. |
| `Resolve(w)` | `_resolve_pending` calls store `resolve`, which deletes on id and token. A failure is logged. |
| `Crash(w)` | Process exit, cancellation or a `BaseException` anywhere in `resume_pending` after `_take_pending` committed. The callback is awaited inline in the browser request (`oauth_github_personal.py` near `:642`), so the person sees a failed page: `crashVisible`. |
| `Expire(r)` | `expires_at <= now`; ten minutes after the save. |

Re-check after the Python change: `take` (cleanup `WHERE`, token written), `renew_claim` (which rows it matches: only rows with the token, including rows this callback skipped; whether it checks `expires_at`), the loop in `resume_pending` (order of renewal, the skip on `claim_lost` and on a renewal exception, that a skipped request posts nothing, that the failure line and the resolve follow a timeout), the constants `PER_REQUEST_DEADLINE`, `FAILURE_DELIVERY_DEADLINE`, `CLAIM_LEASE`, `CLAIMED_GARBAGE_AFTER` and the import-time assertion, `_deliver_recovery` (one transaction, `ClaimLostError` on a zero-row resolve, order of private delivery and the thread line), `resolve`, and `github_callback` (that an exception reaches the browser).

## Constants and scaling

| Quantity | Model | Source and scaling |
| --- | --- | --- |
| Requests | 2 | Smallest batch where one request can fail, be skipped or expire while another runs. Order is the take order (`expires_at`, then `id`), so request 1 runs first. |
| Workers (link callbacks) | 2, one `take` each | One callback crashes or runs slowly, a second one retakes. A worker's one take is one fresh token, so the token is the worker's name. Production has one callback per link completion. |
| `Lease`, `Deadline`, `Garbage` | 1, 2, 4 ticks | `CLAIM_LEASE` 2 min, `PER_REQUEST_DEADLINE` 5 min (plus 30 s failure line), `CLAIMED_GARBAGE_AFTER` 60 min. Only the ordering is kept: `Lease < Deadline` and `Deadline + Lease < Garbage`. The ratios differ from production (the model's grace is two deadlines, production's is about twelve); the argument depends on the inequality only. |
| Expiry | `expired` flag, set at any time | Ten minutes in code. Any ordering of expiry, lease and take is explored. |
| Replacement rows | a flag, `replSaved` | The replacement is a fresh pending row for the next link callback; its own life is not modelled. |
| Take per worker | 1 | A callback calls `take` once. |

`take`, `renew_claim`, `resolve`, the recovery transaction and `save` are each one atomic action: each is one SQL transaction in the code. `Start`, `Process*`, `PrivateDeliver`, `Deliver` and `Resolve` are separate because each is a separate `await` with network or database calls between them. The renewal and the start of processing are one action; a lease cannot lapse between them in production because the renewal just set it.

## Properties

| Property | Meaning |
| --- | --- |
| `NoSilentLoss` (invariant) | A row is never removed unless its request reached an outcome **and** a post to the person was attempted, **or** it expired and no live worker still intended to run it (never claimed, or its holder died, skipped it or finished with it), **or** a worker that held it unfinished died or let an exception escape (the person saw a failed browser request), **or** the recovery transaction replaced it with a fresh pending row. One further clause is the state form of "will attempt": a live worker is past its renewal for the request and has not posted yet; `live` checks that this window closes (`HeldReachesOutcome`, `OutcomeReachesAttempt`). A row whose holder is alive but has not yet started it is **not** covered, because the renewal would fail and the worker would skip it: deleting such a row is the violation `lapsed_cleanup`, `no_heartbeat` and `no_deadline` show. |
| `NoDuplicateDelivery` (invariant) | Two thread posts for one request only if its lease lapsed while a live worker was working on it, or after a post was tried (a crash or slow resolve between the post and the delete). Both are accepted and documented. A stale worker whose token no longer matches skips, and a second take never returns a live claim. |
| `CrashLeavesRetakeable` (invariant) | A worker's death or escaped exception never deletes the request it held: until the request expires, the row stays for a later take, or the recovery transaction replaced it, or it was answered. |
| `RecoveryAtomic` (invariant) | The original and its replacement are never both present, and the original is never gone with no replacement and no post attempt, unless it expired and was cleaned after its holder left it. |
| `HeldReachesOutcome` (temporal) | A request a worker holds reaches an outcome (a grant result, a recovery decision or a failure line) even when another request in its batch fails, unless its holder skips it. Fairness: weak fairness on the workers' own steps (`WorkerStep`); none on `Save`, `Take`, `Expire`, `Tick`. Checked with `CrashEnabled = FALSE` and `RenewException = FALSE` (a dead or skipping worker holds nothing; the skip path is covered by `safe`). |
| `OutcomeReachesAttempt` (temporal) | An outcome is followed by a post attempt, unless a worker skipped the request afterwards (its claim passed to another callback). Same fairness and bounds. |

## Results

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `safe` | clean | 4,454,461 | The design: token claim, heartbeat, deadline, cleanup after the hour's grace, token resolve, atomic recovery, with crash, expiry, a renewal exception and a second take. |
| `live` | clean | 441,885 | Same design, no crash and no renewal exception: every held request reaches an outcome (or is skipped) and then a post attempt under fairness. |
| `lapsed_cleanup` | violates `NoSilentLoss` | 1,405 | Cleanup of expired rows whose lease lapsed, with no grace (the design as first specified): deletes a request a live slow worker still holds unstarted. |
| `expiry_deletes_claimed` | violates `NoSilentLoss` | 82 | Cleanup deletes every expired row, the code at `9441d430d`: a request under a live claim is destroyed. |
| `no_heartbeat` | violates `NoSilentLoss` | 33,209 | Renewal of the current row only, the hour's grace and no per-request deadline: one slow request lets the rows behind it age out and be deleted. |
| `no_deadline` | violates `NoSilentLoss` | 33,209 | Heartbeat and grace but no deadline: one request longer than the grace does the same. |
| `delete_on_claim` | violates `CrashLeavesRetakeable` | 26 | The old take: deletes the rows it returns and has no renewal, so a crash destroys the requests. |
| `abort_batch_on_exception` | violates `HeldReachesOutcome` | 529 | The old loop exactly: `DeleteOnClaim` plus an exception that leaves the loop. See the note below the table. |
| `resolve_before_delivery` | violates `CrashLeavesRetakeable` | 725 | Deleting as soon as the outcome is known, before the post, leaves a window where a crash loses the message. |
| `take_ignores_lease` | violates `NoDuplicateDelivery` | 20,177 | Without the lease check a second callback takes rows the first is still processing. |
| `renew_ignores_token` | violates `NoDuplicateDelivery` | 508,318 | Renewal by id lets a worker that lost its claim keep going and post a request another worker also holds. |
| `resolve_ignores_token` | clean | 4,124,183 | Not a violation: resolve by id alone does not cause a duplicate post or a loss in this model. See the note below the table. |
| `nonatomic_recovery` | violates `RecoveryAtomic` | 276 | The code at `9441d430d`: the replacement commits first and the original stays, so both exist. |
| `retake_after_crash_witness` | violates `RetakeAfterCrashNeverFinishes` | 4,807 | Witness: the same request a crashed worker held is answered (outcome and post) by a second take after the lease. |
| `failure_does_not_block_witness` | violates `FailureNeverUnblocksOthers` | 13,815 | Witness: after request 1 fails, request 2 in the same batch finishes. |
| `delivery_failure_witness` | violates `DeliveryFailureAlwaysVisible` | 575 | Witness of an accepted gap: a failed post is logged, not retried, and the row is deleted, so the thread gets no line. |
| `recovery_commit_witness` | violates `RecoveryNeverCommits` | 231 | Witness: the recovery transaction commits, leaving the original gone and the replacement present. |
| `renew_skip_witness` | violates `SkipNeverHappens` | 1,004 | Witness: a worker whose claim was lost skips the request (renewal exception off). |
| `renew_exception_witness` | violates `RenewExceptionNeverSkips` | 26 | Witness: a renewal exception skips a request and leaves its row claimed by the skipping worker. |
| `grace_cleanup_witness` | violates `GraceNeverCleans` | 17,878 | Witness: a claimed expired row is deleted once its lease lapsed the grace ago: the row a crashed holder left. |
| `deadline_witness` | violates `DeadlineNeverFires` | 584 | Witness: a request that hits its deadline gets a failure line in its thread. |

`abort_batch_on_exception` is the pre-claim code, not a claim-lease variant: `take` deleted the rows it returned (`DeleteOnClaim`), the loop had no per-request `try`, so an exception left it. The failing request gets no outcome and the rest of the batch is abandoned with its rows already deleted. The exception reaches the browser, so `NoSilentLoss` is not violated (the crash clause covers it); what breaks is that held requests never reach an outcome.

`resolve_ignores_token` is clean because a resolve always follows that worker's own post attempt. A stale worker that deletes a row another worker holds only removes a request that already has a post. The other worker then skips (its renewal finds no row) or has already started and posts a second time, exactly as it would with a token check. In this model the token on resolve changes which worker wins, not safety; the token check that prevents duplicates is on the renewal (`renew_ignores_token`). Keep the token on resolve if the design wants a stale worker unable to touch a new holder's row, but do not cite this model as the reason.

`no_heartbeat` and `no_deadline` show the pair of things the grace window needs, but on a two-request batch they are the same state graph (both pin 33,209 states): no tick passes between the take and the first renewal, so the heartbeat at the first request renews row 2 at the same moment the take set it, and the counterexample is a slow first request either way. With a two-request batch the heartbeat alone cannot be isolated: with a deadline on, the second row's age at its start is at most one deadline, below the grace, so a no-heartbeat config with the deadline on is clean here. The heartbeat matters through the batch size: without it the last row's age at its start is `(n - 1)` times `PER_REQUEST_DEADLINE`, and `take` selects an unlimited batch, so `n` of about thirteen or more at the real constants breaks the grace; with the heartbeat the age is one request long for any `n`. A three-request config at these ticks needs a larger tick scale than the one-hour-per-config limit allows, so it is not pinned; the claim rests on that arithmetic and on `test_without_the_heartbeat_the_stranger_cleanup_would_delete_the_second_row`.

## Counterexamples in plain words

1. `lapsed_cleanup` (cleanup with no grace): a link callback takes a batch; request 2 is not started yet because request 1 is slow. Request 2 expires and its lease lapses. A second callback's `take` deletes every expired row whose lease lapsed, including request 2. The first callback then reaches request 2, its renewal does not return the row and it skips it. No outcome, no post, no crash, no row. The grace window removes this: the row is deletable only an hour after its last renewal.
2. `no_deadline` and `no_heartbeat`: the grace window is only as good as the bound on how long a live callback goes without renewing. With no per-request deadline, request 1 hangs (an unpaginated list of installations and repositories, a slow GitHub, a lock wait) for longer than the hour. Request 2's row, never renewed since the take, expires and is cleaned by another person's callback; when request 1 finally ends, request 2's renewal returns nothing and it is skipped with no line in its thread, and no crash. The deadline cuts request 1 off at five minutes with a failure line.
3. `expiry_deletes_claimed` (`9441d430d`): the same, without waiting for the lease: a callback that took a request near its expiry loses the row to any other `take` while the request is under a live claim.
4. `delete_on_claim`: two requests are saved. The callback takes them; `take` deletes both rows. The agent lookup raises or the process exits. No outcome, no row; the person's browser shows an error and the thread never hears back.
5. `abort_batch_on_exception`: two requests are taken and deleted. The first raises; the exception leaves the loop. The browser shows an error; the second request has no outcome and no row.
6. `resolve_before_delivery`: the grant for a request succeeds and its row is deleted. The process exits before the result is posted. The grant exists, the person sees a failed browser page and never gets the line, and nothing can retake the request.
7. `take_ignores_lease`: one callback takes a request and starts it. A second callback takes the same request while the first lease is still live, and both post.
8. `renew_ignores_token`: callback A takes requests 1 and 2 and is slow on 1. The lease lapses. Callback B takes both and starts request 2. A reaches request 2, its renewal by id succeeds on B's claim, and both post for request 2.
9. `nonatomic_recovery`: a request needs a fresh GitHub link. The replacement row commits, then the process exits before the original is resolved. Both rows exist; the next callback takes both and the person gets two recovery links and two lines.
10. `delivery_failure_witness`: a request succeeds, the post to the thread fails, the failure is logged and the row is deleted. The person sees nothing. This is the one gap the model shows and the design keeps.

## Assumptions

Support: **supported** means a vendor document or the cited code says it; **unsupported** means it is assumed and no staging trace or contract test confirms it.

| Assumption | Basis | Support |
| --- | --- | --- |
| A live callback never goes more than about `PER_REQUEST_DEADLINE + FAILURE_DELIVERY_DEADLINE` between two renewals, because the deadline cancels a request that overruns and the heartbeat renews every held row before the next one. This replaces the earlier "no live callback is an hour behind" assumption. | `asyncio.timeout(PER_REQUEST_DEADLINE.total_seconds())` in `resume_pending`; `renew_claim` (one statement, by token, all held rows); import-time assertion of the inequality. | Code supported. Two gaps: `asyncio.timeout` cancels only at an `await`, so a synchronous call that blocks the event loop is not bounded; and the database calls between requests (`take`, `renew_claim`, `resolve`) have no statement timeout in the code, so a stalled database connection adds time the model does not count (the margin is about 55 minutes). Neither is checked by a test or a trace. |
| The failure line after a deadline and the resolve take negligible time against the grace. | `FAILURE_DELIVERY_DEADLINE` (30 s) around the failure line; the resolve is a database call. | Failure line: code supported. Resolve: unsupported, as above. |
| `take` is atomic: two concurrent calls cannot both claim a row; the second skips rows the first locked, or sees the new claim after the first commits. | `FOR UPDATE SKIP LOCKED` in `take`; [PostgreSQL 16, SELECT, locking clause](https://www.postgresql.org/docs/16/sql-select.html#SQL-FOR-UPDATE-SHARE) ("any selected rows that cannot be immediately locked are skipped"). | Supported by the documentation; not replayed against the project's database. |
| The callback runs inline in the browser request, so a crash or an escaped exception shows the person a failed page. | `oauth_github_personal.py` `github_callback` awaits `resume_pending` before it renders the page. | **Code fact, to re-verify** after the Python change and with a real crash. Everything under `crashVisible` rests on it: with no browser signal, `delete_on_claim` and `abort_batch_on_exception` would also violate `NoSilentLoss`. |
| Every exception raised while handling one request is caught and becomes a failure line for that request; an exception from the renewal call skips the request. `BaseException` (cancellation, `SystemExit`) behaves as `Crash`. | `_answer_request` (`except Exception` to `FAILED_MESSAGE`); `resume_pending` (`except Exception: continue` around `_renew_claim`). | Supported by the code. |
| A crash drops all in-flight state and leaves the claim as written. | `take` commits before processing (`_take_pending`). | Supported by the code. |
| The recovery transaction commits only if its resolve deleted a row (id and token). A mismatch rolls the transaction back and the worker skips. | `_deliver_recovery` raises `ClaimLostError` when `resolve` returns false inside the transaction. | Supported by the code; confirmed by `test_recovery_by_a_callback_that_lost_the_claim_adds_nothing` as named, not rerun here. |
| Renewal is conditional on the token and does not check `expires_at`; it succeeds even when the lease already lapsed, provided no one retook the row. | `renew_claim`: `WHERE claim_token = :token`. | Supported by the code. |
| `renew_claim` renews every row carrying the token, including rows this callback skipped. Rows already deleted are not returned. | Same statement. | Supported by the code; the model renews rows the worker holds and rows it skipped alike, which only matters for a skipped row's age. |
| A failed `resolve` behaves like a crash between the post and the delete: the row is retaken after the lease and the request runs again. | The loop logs `resolve_failed`; the comment says the retry is idempotent. | Code supported; idempotence of the whole grant path including the thread line is unsupported (a second line is possible, accepted). |
| The agent lookup, the authority recheck and `linked_grant` are one step. | Abstraction; the grant commit is its own serializable transaction (see `formal/github_linked_grant/`). | Unsupported as an abstraction; a crash between the grant commit and the failure or outcome line is treated like a crash after `ProcessOk`. |
| Time is counted only as ticks of a lease, a deadline and a grace window; expiry can pass at any time; no tick passes while a renewal is pending. | Over-approximation of the 2, 5, 60 and 10 minute constants. | Unsupported as exact timing; sound as an over-approximation for expiry. The grace and deadline ratios are scaled down (see Constants). |
| A failed post is never retried. | `_deliver_result` is logged only. | Supported by the code (`delivery_failure_witness`). |

## Not modelled

Request content, agent identity checks, `needs_install` recovery (no replacement row; it behaves like the plain path), the life of the replacement row after it is saved (a later callback taking it, its own expiry), multiple requesters, more than two requests or callbacks (the batch-size argument for the heartbeat is arithmetic, see above), a callback that calls `take` twice, clock skew between database and process, cancellation as a separate case from a crash, the browser page text (`REQUEST_CHANGED_MESSAGES`), the second-installation result, a post that succeeded and was then cancelled by the deadline (the failure line is then a second post), the failure line's own 30-second deadline and the resolve as zero-tick steps, and a garbage collector for claimed rows that never expire (a request whose callback crashed keeps its row until it expires and a later `take` cleans it). Recovery is not explored for the legacy loop (`DeleteOnClaim`).

## Replay status

Provisional. The model has not been replayed against PostgreSQL or the chat platforms. Counterexample and witness tests, by name in `packages/adapters/mcp/tests/tools/test_github_linked_grant.py` (present in the working tree at the time of writing, some uncommitted; re-check the names if the file moves; not rerun here, and whether each fails without its fix was not checked):

| Model result | Test |
| --- | --- |
| `delete_on_claim`, `abort_batch_on_exception`, `failure_does_not_block_witness` (counterexamples 4, 5) | `test_one_failed_request_does_not_lose_the_rest_of_the_batch`, `test_failure_after_the_grant_keeps_it_resolves_the_row_and_runs_the_next` |
| `retake_after_crash_witness` | `test_a_crash_after_the_claim_leaves_requests_retakeable_after_the_lease` |
| expired never-claimed rows | `test_expired_requests_are_deleted_not_claimed` |
| `lapsed_cleanup`, `expiry_deletes_claimed` (counterexamples 1, 3) | `test_expiry_cleanup_keeps_a_claimed_request_until_its_lease_is_long_past` |
| `resolve_ignores_token`, token on resolve | `test_resolve_with_another_claims_token_deletes_nothing` |
| `renew_skip_witness`, `renew_ignores_token` (counterexample 8) | `test_a_callback_that_lost_its_claim_skips_the_request_without_delivering`, `test_a_row_another_callback_took_is_skipped_even_when_others_are_held` |
| `nonatomic_recovery` (counterexample 9) | `test_recovery_replaces_the_original_request_in_one_transaction`, `test_a_failure_between_saving_the_replacement_and_resolving_leaves_neither`, `test_recovery_by_a_callback_that_lost_the_claim_adds_nothing` |
| heartbeat, `no_heartbeat`, `no_deadline` (counterexample 2) | `test_a_slow_first_request_keeps_the_second_requests_lease_alive`, `test_without_the_heartbeat_the_stranger_cleanup_would_delete_the_second_row` |
| `deadline_witness` | `test_a_request_past_its_deadline_gets_the_failure_line_and_the_next_still_runs`, `test_the_failure_line_after_a_deadline_is_best_effort` |
| the inequality `PER_REQUEST_DEADLINE + FAILURE_DELIVERY_DEADLINE + CLAIM_LEASE < CLAIMED_GARBAGE_AFTER` | `test_the_lease_and_deadline_fit_inside_the_garbage_window` |

No test found by name for counterexamples 6 and 7 (`resolve_before_delivery`, `take_ignores_lease`), for a renewal exception skip, or for a synchronous call that outlasts the deadline. Upstream staging contract tests: 0 run. Real traces validated: 0.
