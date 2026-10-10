# Discord startup replay and owner connection loss

Question: can another worker take a pending message or active card while its original worker can still execute after PostgreSQL drops only that worker's owner connection?

The model has one message, two workers, one crash, one connection loss, and one gateway redelivery. `connected` models the PostgreSQL advisory lock separately from `live`, which models a worker still able to run turns. `ConnectionLoss` removes only the lock. With `GraceCheck = TRUE`, another worker cannot reclaim until the original worker has stopped and the heartbeat grace has elapsed. The real implementation uses a 120-second database-clock grace, not the model's logical `GraceElapsed` action.

| Model action | Code source |
| --- | --- |
| `Restart`, `Replay`, `Gateway` | `packages/adapters/discord/daimon/adapters/discord/bot.py:1695,1782,2176` |
| `Claim` | `packages/core/daimon/core/stores/discord_message_admissions.py:14-72`; `packages/core/daimon/core/stores/worker_ownership.py:13-37` |
| `Start` | `packages/adapters/discord/daimon/adapters/discord/bot.py:2451,2617`; `packages/core/daimon/core/stores/discord_message_admissions.py:75-96` |
| `Crash` | `packages/adapters/discord/daimon/adapters/discord/runtime.py:53-80`; `packages/core/daimon/core/stores/worker_ownership.py:13-37` |
| `ConnectionLoss` | `packages/adapters/discord/daimon/adapters/discord/runtime.py:98-115` (failed owner probe, lock already gone) |
| `Stop` | `packages/adapters/discord/daimon/adapters/discord/__main__.py:28-37`; `packages/adapters/discord/daimon/adapters/discord/bot.py:797-847` |
| `GraceElapsed` | `packages/core/daimon/core/stores/worker_ownership.py:10-37` (database clock and persisted heartbeat) |

| Bound or assumption | Value and source |
| --- | --- |
| Workers | Two, minimum for takeover; production has no two-worker cap. |
| Message and redelivery | One each; enough for duplicate execution. |
| Crash and connection loss | At most one of each; `ConnectionLoss` does not imply `Stop`. |
| Grace | Logical ordering in TLC. Production: heartbeat every 5 s; failed probe has a 5 s timeout; owner-loss drain has a 100 s hard exit deadline. The last heartbeat can precede loss by 5 s, so stop occurs within 115 s of that heartbeat under the assumption that the event loop schedules the probe, timeout, drain, and exit timer on time. The 120 s grace leaves 5 s of margin. |
| Replay | Enabled and covers the message; production default remains 0 minutes. |

| Config | Verdict | States | Meaning |
| --- | --- | ---: | --- |
| `ReplaySafe` | clean | 732 | Admission and grace prevent duplicate starts and live-worker takeover. |
| `ReplayNoAdmission` | violates `NoDuplicate` | 78 | Redelivery starts a second turn without the ledger. |
| `ReplayNoGrace` | violates `NoTakeoverOfLiveWorker` | 88 | A connection dies, its worker remains live, and a second worker reclaims its pending message. |

The `ReplayNoGrace` counterexample is replayed by `test_connection_loss_cannot_reclaim_live_pending_message` and `test_connection_loss_cannot_recover_live_card`, both against PostgreSQL using `pg_terminate_backend` on only the owner's backend. Both fail on `9e18f80dc` and pass with the heartbeat grace. No staging Discord contract test or production trace replay was run. The clean result is provisional within the listed bounds and timing assumption.
