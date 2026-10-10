# Discord startup replay and owner connection loss

Question: can another worker take a pending message or active card while its original worker can still execute after PostgreSQL drops only that worker's owner connection?

The model has one message, two workers, one crash, one connection loss, and one gateway redelivery. `connected` models the PostgreSQL advisory lock separately from `live`, which models a worker still able to run turns. `ConnectionLoss` removes only the lock. `Recover` reacquires it within the retry budget; `RetryExpired` enables fail-stop. With `GraceCheck = TRUE` and `ShortGrace = FALSE`, another worker cannot reclaim until the original worker has stopped and the heartbeat grace has elapsed. The real implementation uses a 180-second database-clock grace, not the model's logical `GraceElapsed` action.

| Model action | Code source |
| --- | --- |
| `Restart`, `Replay`, `Gateway` | `packages/adapters/discord/daimon/adapters/discord/bot.py:1695,1782,2176` |
| `Claim` | `packages/core/daimon/core/stores/discord_message_admissions.py:14-72`; `packages/core/daimon/core/stores/worker_ownership.py:13-37` |
| `Start` | `packages/adapters/discord/daimon/adapters/discord/bot.py:2451,2617`; `packages/core/daimon/core/stores/discord_message_admissions.py:75-96` |
| `Crash` | `packages/adapters/discord/daimon/adapters/discord/runtime.py:68-117`; `packages/core/daimon/core/stores/worker_ownership.py:23-47` |
| `ConnectionLoss` | `packages/adapters/discord/daimon/adapters/discord/runtime.py:130-146` (failed owner probe, lock may already be gone) |
| `Recover` | `packages/adapters/discord/daimon/adapters/discord/runtime.py:77-91,146-163` (check unexpired heartbeat before refreshing it) |
| `RetryExpired` | `packages/adapters/discord/daimon/adapters/discord/runtime.py:146-166` |
| `Stop` | `packages/adapters/discord/daimon/adapters/discord/__main__.py:29-38`; `packages/adapters/discord/daimon/adapters/discord/bot.py:797-847` |
| `GraceElapsed` | `packages/core/daimon/core/stores/worker_ownership.py:14-45` (database clock and persisted heartbeat) |

| Bound or assumption | Value and source |
| --- | --- |
| Workers | Two, minimum for takeover; production has no two-worker cap. |
| Message and redelivery | One each; enough for duplicate execution. |
| Crash and connection loss | At most one of each; `ConnectionLoss` does not imply `Stop`. Recovery and expiry are alternate outcomes. |
| Grace | Logical ordering in TLC. Production constants: 5 s probe interval, 5 s timeout, B = 30 s reconnect budget, 100 s hard exit, 40 s scheduling margin (`worker_ownership.py:6-20`). G = 5 + 5 + 30 + 100 + 40 = 180 s, so G > probe interval + timeout + B + stop = 140 s. The extra 40 s covers event-loop scheduling and teardown under a bounded scheduling assumption. |
| Replay | Enabled and covers the message; production default remains 0 minutes. |

| Config | Verdict | States | Meaning |
| --- | --- | ---: | --- |
| `ReplaySafe` | clean | 1020 | Admission and grace prevent duplicate starts and live-worker takeover; recovery cannot force a stop in the one-loss model. |
| `ReplayNoAdmission` | violates `NoDuplicate` | 83 | Redelivery starts a second turn without the ledger. |
| `ReplayNoGrace` | violates `NoTakeoverOfLiveWorker` | 94 | A connection dies, its worker remains live, and a second worker reclaims its pending message. |
| `ReplayShortGrace` | violates `NoTakeoverOfLiveWorker` | 234 | Grace expires during retry before stop, then another worker takes the pending message. |
| `ReplayRecoveryWitness` | violates `NoTransientRecovery` | 36 | A failed probe can recover with the original worker still serving. |

The `ReplayNoGrace` counterexample is replayed by `test_connection_loss_cannot_reclaim_live_pending_message` and `test_connection_loss_cannot_recover_live_card`, both against PostgreSQL using `pg_terminate_backend` on only the owner's backend. Both fail on `9e18f80dc` and pass with the heartbeat grace. `test_one_timed_out_heartbeat_recovers_without_stopping` fails at `2f43a2bac` and passes with retry; `test_reconnect_after_grace_fails_stop` checks the late-recovery guard. The short-grace model counterexample is not replayed at real clock duration; its timing bound remains an assumption. No staging Discord contract test or production trace replay was run. The clean result is provisional within these bounds.
