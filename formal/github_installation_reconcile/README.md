# GitHub installation reconciliation ordering model

The user-visible risk is a cached installation with the wrong repositories,
suspension flag, or existence after webhook traffic stops. This model checks
whether duplicate and reordered deliveries, changes during a refresh, and a
lease takeover can strand stale cache data or allow an old worker to finish.
It maps to the implementation on `fix/app-installation-reconcile`.

Run `python3 formal/github_installation_reconcile/run_models.py` from the repo
root. The runner uses one TLC worker, a 30-second limit and 64 MiB metadir cap
per case, removes each metadir, and pins distinct-state counts in
`expected.tsv`. Terminal states are allowed because TLA's stuttering step
represents a system at rest.

## Action-to-code map

| Model action | Implementation boundary |
| --- | --- |
| `GitHubChange` | External GitHub event; webhook routing in `packages/adapters/mcp/daimon/adapters/mcp/webhooks.py:182-192`, installation handler at `:208-269` accepts `suspend`/`unsuspend`, repository handler at `:271-325`. Connect saves at `packages/adapters/mcp/daimon/adapters/mcp/oauth_github.py:1925-1941,2143-2159` upsert and enqueue a synthetic refresh. |
| `Duplicate` | A second delivery with the same ID enters the handler at `webhooks.py:245-253` or `:301-309`; GitHub redelivery behavior is unverified. |
| `Enqueue` | `packages/core/daimon/core/stores/github_installation_reconciliation.py:28-102`: receipt conflict check at `:38-51`, generation and pending/running update at `:53-83`, deletion side effect at `:84-101`. |
| `Claim` | `github_installation_reconciliation.py:117-155`: pending or expired running row, `FOR UPDATE SKIP LOCKED`, owner and claimed generation. Worker transaction at `packages/core/daimon/core/github_installation_reconcile.py:44-55`. |
| `ReadGitHub` | `github_installation_reconcile.py:68-95`: installation lookup (including suspension), token mint, complete repository listing. These calls are separate in real execution. |
| `LeaseExpire` | `github_installation_reconciliation.py:124-132`, `:149-151`; lease is two minutes at `github_installation_reconcile.py:22` with renewal every 30 seconds at `:23,136-165`. |
| `Finish` | `github_installation_reconciliation.py:180-290`: owner/claim check `:195-208`, generation check `:210-219`, deletion `:221-240`, live snapshot and suspension write `:241-281`, done `:283-290`. Connect's `upsert_github_app` at `packages/core/daimon/core/stores/github_app_installations.py:55-94` writes suspension only on insert; reconciliation owns subsequent writes. `LegacyOnlyWrite` represents the old behavior that skipped App truth updates; the remaining `app == "legacy"` guard at `:278` protects Connect's confirmed set. Worker finish transaction is `github_installation_reconcile.py:97-111`. |

The model treats each enqueue, claim, and finish database transaction as
atomic. The read and finish are separate, so GitHub can change and a new
delivery can arrive between them. A successful GitHub read is represented as
one complete snapshot; the separate API calls above need replay or a contract
test before that abstraction can be trusted.

## Bounds and properties

| Quantity | Model | Production source or scaling |
| --- | --- | --- |
| Installations | 1 | Per-installation queue row at `github_installation_reconciliation.py:53-64`; independent IDs are omitted. |
| Repositories | 2 | Smallest set showing distinct add/remove choices; no repository cap is assumed. |
| Reconcilers | 2 | Smallest count showing lease takeover; each worker creates an owner UUID at `github_installation_reconcile.py:47`. |
| GitHub changes/delivery IDs | 2 | Smallest sequence showing order reversal and a generation race. Production is unbounded; receipts are retained 30 days at `github_installation_reconciliation.py:25,310-316`. |
| Duplicate deliveries | 1 extra per ID | Enough to exercise receipt deduplication at `github_installation_reconciliation.py:38-51`; production duplicates are unbounded. |
| Lease expiry | 1 | Enough for a takeover. Actual lease and heartbeat are two minutes and 30 seconds at `github_installation_reconcile.py:22-23`. |

`NoStaleOverwrite` requires every completed write to be made by the current
lease owner, to keep the highest version previously written, and to have read
at or after the newest *enqueued* event. The version is a model-only witness
for read order; the database does not store it. `EventuallyConsistent` says
that, under weak fairness for each queued delivery and for Claim, ReadGitHub,
and Finish, the cache eventually equals GitHub truth forever once the bounded
environment stops changing. There is no fairness assumption for GitHub
changes, duplication, or lease expiry. The bounded model permits at most one
expiry and assumes each successful read and write can finish. The witness
config negates a reachable state where delivery 2 is received first, a
duplicate is consumed, all deliveries drain, and the cache equals truth.

## TLC results

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `Safe` | clean | 40,493 | Both properties hold within the bounds. |
| `NoGenerationCheck` | violates `NoStaleOverwrite` | 1,577 | An old read can complete after a newer event was enqueued. |
| `LegacyOnlyWrite` | violates `EventuallyConsistent` | 24,851 | A `github_app` row never receives the refreshed snapshot. |
| `NoLeaseOwnerCheck` | violates `NoStaleOverwrite` | 1,606 | A worker whose lease was replaced can write. |
| `ReorderDuplicateWitness` | witness reached | 4,358 | Reordered delivery and duplicate processing can still converge. |

Counterexamples in domain terms:

1. **No generation check:** GitHub changes, worker A claims and reads, then
   another GitHub change is enqueued. A finishes its earlier snapshot and
   marks the job done, losing the newer refresh signal.
2. **Legacy-only write:** GitHub changes a repository or installation state;
   the worker fetches current truth and finishes, but the `github_app` cache
   row is skipped. With no more deliveries, it remains stale.
3. **No lease-owner check:** A claims, its lease expires, and B takes over.
   A can finish while B owns the row. The owner guard is what rejects A's
   result; a later B finish may otherwise be rejected after A marks it done.
4. **Reordered duplicate witness:** Event 2 is delivered before event 1,
   one ID is redelivered, and the receipt check suppresses its second
   generation bump. A later complete GitHub read brings the cache to truth.

## Assumptions and replay status

| Assumption | Evidence/status |
| --- | --- |
| Each GitHub change eventually emits a webhook that this service receives; duplicate IDs refer to the same change; deliveries can be delayed/reordered. | **UNVERIFIED GitHub webhook delivery semantics.** No vendor contract or staging trace was checked. Liveness depends on eventual delivery. |
| Installation lookup and complete repository pagination behave like one consistent snapshot, including suspension. | **UNVERIFIED.** Actual calls are separate at `github_installation_reconcile.py:74-95`; a change between pages or calls can yield a mixed view. |
| Database enqueue/claim/finish transactions commit atomically and are serialized on the queue row. | Code uses transaction blocks at `webhooks.py:245-253,301-309` and `github_installation_reconcile.py:48-55,91-99`; SQL row lock at `github_installation_reconciliation.py:141,201`. No database replay was run. |
| Workers eventually run and successful GitHub API reads/DB writes finish. | Fairness assumption, not an operational guarantee. API failure/retry (`github_installation_reconcile.py:108-125`), renewal, receipt expiry, process crash, and account changes are not modeled. |

The results are **provisional**: no counterexample was replayed against the
store, no GitHub contract test was run, and no real traces were checked
(0 of 0). The model checks these finite bounds, not production-scale behavior.
In particular, the clean verdict does not establish correctness for mixed
multi-call or multi-page GitHub reads, missing webhooks, or suspension until
the implementation and upstream behavior are verified.
