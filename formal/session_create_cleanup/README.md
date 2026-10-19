# Upstream session cleanup when `create_ma_session` fails after `sessions.create`

`SessionCreateCleanup` asks one question: can a turn leave a live upstream
Managed Agents session that no row names, and can a cleanup archive a session
that a row names or that another turn owns?

Before the fix, a cancel (turn Stop, the `asyncio.wait_for` ceiling) or a
database error in the personal-memory lookup that follows `sessions.create`
unwound with no `CreatedSession` ever reaching the caller. `create_session` had
already returned, so it no longer cleaned up, and the caller held nothing to
clean. The upstream session stayed live with no row naming it.

Ownership of one session, as modelled:

| From | Until | Owner |
| --- | --- | --- |
| `sessions.create` accepted | `create_session` returns | `create_session` (archives on any failure; assumed correct, not modelled) |
| `create_session` returns | `create_ma_session` returns `CreatedSession` | `create_ma_session` (fixed: archives on any failure) |
| `CreatedSession` returned | row committed | the caller (recovery, native worker; `create_fresh_session` has no cleanup) |
| row committed | | the row; nobody archives it |
| adopted from another turn | | that turn's row; this turn never archives it |

## Action to code

Line numbers are for the fixed code.

| Model action | Code |
| --- | --- |
| `StartCreate` | callers of `create_ma_session`: [`run.py:717`](../../packages/core/daimon/core/turn/run.py#L717) (dead-session recovery), [`prepare.py:871`](../../packages/core/daimon/core/turn/prepare.py#L871) (`create_fresh_session`), [`prepare.py:1541`](../../packages/core/daimon/core/turn/prepare.py#L1541) (native worker) |
| `StartAdopt` | [`run.py:641`](../../packages/core/daimon/core/turn/run.py#L641) finds another turn's live row and returns it at [`run.py:674`](../../packages/core/daimon/core/turn/run.py#L674) without calling `create_ma_session` |
| `Create` | [`prepare.py:671`](../../packages/core/daimon/core/turn/prepare.py#L671) `create(...)` returns through [`sessions.py:1313`](../../packages/core/daimon/core/sessions.py#L1313) `return created`; the first await after it is the lookup, so the return and the start of the lookup are one action |
| `FailBeforeCreate` | `create_session` archives its own session at [`sessions.py:1314`](../../packages/core/daimon/core/sessions.py#L1314) (native and app-mode paths) and the vault at [`sessions.py:1339`](../../packages/core/daimon/core/sessions.py#L1339); a `SkillUnavailable` retry loops at [`prepare.py:670`](../../packages/core/daimon/core/turn/prepare.py#L670) |
| `LookupDone` | [`prepare.py:707`](../../packages/core/daimon/core/turn/prepare.py#L707) `get_personal_memory_store_id` completes |
| `CloseDone` | [`prepare.py:707`](../../packages/core/daimon/core/turn/prepare.py#L707) `async with deps.sessionmaker()` exit awaits the connection close; the snapshot build and `return CreatedSession` at [`prepare.py:754`](../../packages/core/daimon/core/turn/prepare.py#L754) have no await |
| `FailInWindow` | fixed: the `except BaseException` at [`prepare.py:760`](../../packages/core/daimon/core/turn/prepare.py#L760) calls `_archive_orphaned_session` ([`run.py:476`](../../packages/core/daimon/core/turn/run.py#L476), shielded, fixed 5 s deadline) |
| `Insert`, `Commit` | recovery: [`run.py:720`](../../packages/core/daimon/core/turn/run.py#L720) `insert_mapping` inside `db.begin()` ([`run.py:612`](../../packages/core/daimon/core/turn/run.py#L612)), committed at the `async with` exit; `create_fresh_session`: [`prepare.py:880`](../../packages/core/daimon/core/turn/prepare.py#L880) and `session.commit()`; native worker: [`prepare.py:1565`](../../packages/core/daimon/core/turn/prepare.py#L1565) |
| `CallerFail` | recovery archives at [`run.py:744`](../../packages/core/daimon/core/turn/run.py#L744) when `created is not None`; the native worker archives only the span before its insert ([`prepare.py:1552`](../../packages/core/daimon/core/turn/prepare.py#L1552)); `create_fresh_session` ([`prepare.py:871`](../../packages/core/daimon/core/turn/prepare.py#L871)) has no cleanup |
| `CommitThenError` | a commit that lands but raises (lost acknowledgement) inside the same `except BaseException` blocks |
| `AdoptedOk`, `AdoptedFail` | [`run.py:674`](../../packages/core/daimon/core/turn/run.py#L674) returns the adopted session; there is no cleanup of it |
| `Retry` | `SkillUnavailable` loop in `create_ma_session`; `SessionBusyError` from the seal fence ([`prepare.py:574`](../../packages/core/daimon/core/turn/prepare.py#L574)) re-prepares; the user asking again |

## Constants

| Constant | Safe value | Meaning |
| --- | --- | --- |
| `MaxAttempts` | 2 | a failed attempt can be followed by one more, with a new session |
| `CleanupInCreate` | TRUE | `create_ma_session` archives on a failure in its window (the fix) |
| `CallerCleans` | TRUE | the caller archives a `CreatedSession` it holds when it fails (models the recovery caller) |
| `CallerArchivesAdopted` | FALSE | rejected design: a caller that archives whatever it holds |
| `AmbiguousCommit` | FALSE | a commit can land and still raise |
| `ReplayOfNamedSession` | FALSE | a replayed recorded create hands back a session an earlier attempt's row names |

One turn, one upstream session per attempt, one adopted session. More
sessions or turns add no ordering: no cleanup reads another session's state.

## Properties

| Invariant | Statement |
| --- | --- |
| `NoOrphan` | once an attempt is over, a live upstream session is one a row names |
| `NoArchiveNamed` | a session a committed row names is never archived |
| `NoArchiveAdopted` | the session another turn created is never archived by this turn |
| `SuccessNotArchived` | a successful turn's session is not archived |

## Results

TLC 2.19 (`tla2tools.jar` v1.7.4), one worker. `formal/check.sh` pins the counts.

| Config | Verdict | States | What it shows |
| --- | --- | --- | --- |
| `CleanupSafe` | clean | 48 | the fix, with the recovery caller and the adopt path, keeps all four invariants |
| `CleanupOldCode` | violates `NoOrphan` | 8 | pre-fix code: the leak |
| `CleanupArchivesAdopted` | violates `NoArchiveAdopted` | 7 | rejected design: the recovery caller archiving everything it holds archives an adopted session |
| `CleanupCallerNoCleanup` | violates `NoOrphan` | 15 | residual, not fixed here: `create_fresh_session` has no cleanup after `create_ma_session` returns |
| `CleanupAmbiguousCommit` | violates `NoArchiveNamed` | 22 | residual, not fixed here: a commit that lands but raises makes the caller archive a named session |
| `CleanupReplayOfNamed` | violates `NoArchiveNamed` | 8 | residual, reachable only if a recorded create is replayed after its row committed; see assumptions |
| `CleanupWitnessWindow` | violates `NoWindowCleanup` | 8 | witness: the window cleanup is reachable |
| `CleanupWitnessSuccess` | violates `NoCreatedSuccess` | 21 | witness: a created session reaches a committed row |

Checked within these bounds, not proved for the production code.

## Counterexample: pre-fix leak (`CleanupOldCode`)

1. A user's session dies upstream. Dead-session recovery takes the thread lock
   and calls `create_ma_session`.
2. `sessions.create` succeeds. `create_session` returns the new session and
   `create_ma_session` starts the personal-memory lookup.
3. The turn ceiling fires (or Stop is pressed, or the database drops the
   connection) while the lookup is awaiting.
4. The exception unwinds out of `create_ma_session`. Recovery's `created` is
   still `None`, so its cleanup skips the archive. The recovery transaction
   rolls back and no row names the session.
5. The upstream session is live and nothing will ever archive it.

## Assumptions

Unsupported until a contract test or a real log shows them.

| Assumption | Status |
| --- | --- |
| Archive succeeds when it is attempted. The real call is best effort: it logs and gives up after a 5 s wait ([`run.py:476`](../../packages/core/daimon/core/turn/run.py#L476)); a failed archive leaves the leak | upstream behaviour, not modelled; the tests answer 200 and 500 and check only that the attempt is made and the original error is kept |
| Archiving an already archived or deleted session is harmless and idempotent | not tested against staging |
| `create_session` cleans up its own session on every failure before `return created` | read from [`sessions.py:1314`](../../packages/core/daimon/core/sessions.py#L1314); read from the code: after creation only the app-mode and native paths await |
| A cancel is delivered only at an await | asyncio semantics |
| `CleanupReplayOfNamed`: a recorded `session_create` is replayed by a later attempt under the same operation key ([`persistence.py:214`](../../packages/core/daimon/core/turn/persistence.py#L214), [`persistence.py:234`](../../packages/core/daimon/core/turn/persistence.py#L234)) | true for two calls in one process ([`test_root_controls.py:264`](../../packages/core/tests/turn/test_root_controls.py#L264) `first == second`); whether production reaches a replay after the row committed is unknown. If it does, the replayed call's failure archives a named session, and the next turn finds it archived and recovers through the dead-session path |

## Not modelled

- `create_session`'s internal steps (vault, files, repository resources, app
  access); only its guarantee is used.
- The caller's later steps after the commit: the turn itself, billing, markers.
- Two turns racing for one thread. The per-thread advisory lock serialises
  recovery and preparation, and [`session_preparation/`](../session_preparation/README.md)
  models that.
- Process death: a crash runs no cleanup in any variant. The recorded
  `session_create` operation is what lets a restarted preparation find the
  session again on the prepared path; no other path has that.
- Archive failure, timeout and retry.

## Replay status

- `packages/core/tests/turn/test_create_ma_session_cleanup.py` reproduces
  `CleanupOldCode` against the real code with a fake upstream: it fails before
  the fix and passes after. It also covers the adopt path, the success paths,
  the prepared (recorded `session_create`) path and the replayed create.
- Mutations: removing the cleanup fails 8 of 11 tests; archiving a wrong id fails
  the same 8; archiving on success fails the 3 negative tests.
- No contract test against the real Managed Agents archive endpoint and no
  trace validation. The results above are provisional until both exist.

## Running

```sh
cd formal/session_create_cleanup
java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -config CleanupSafe.cfg SessionCreateCleanup.tla
```
