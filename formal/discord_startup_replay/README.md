# Discord startup replay admission

Question: after a mention arrives during downtime, can two workers plus a gateway redelivery start two turns, or can replay leave it unanswered?

The model has one message within the configured replay window, two workers, one worker crash, and one gateway redelivery. It explores the interleaving between a committed admission claim, owner exit, a replay scan, and starting a turn. A claimed input can be reclaimed only after its owner exits. The safe config checks `NoDuplicate` and `EventuallyAnswered` with weak fairness for worker restart, replay observation, and turn start. The liveness verdict assumes at least one replacement worker restarts, the channel is in the scan, and the message passes policy gates.

| Model action | Code source |
| --- | --- |
| `Init` (missed addressed message in the window) | `packages/adapters/discord/daimon/adapters/discord/bot.py:1722-1730`; `packages/core/daimon/core/config.py:381-389` |
| `Restart` | `packages/adapters/discord/daimon/adapters/discord/bot.py:1518-1519`, `:1643`, `:1721-1729` |
| `Claim`, `Replay`, `Gateway` | `packages/adapters/discord/daimon/adapters/discord/bot.py:2115-2130`, `:2267-2278`; `packages/core/daimon/core/stores/discord_message_admissions.py:14-72` |
| `Start` | `packages/adapters/discord/daimon/adapters/discord/bot.py:2290-2350`, `:2447`; `packages/core/daimon/core/stores/discord_message_admissions.py:75-96` |
| `Crash` (owner advisory lock disappears) | `packages/adapters/discord/daimon/adapters/discord/runtime.py:61-78`, `:92-137`; `packages/core/daimon/core/stores/worker_ownership.py:7-23`; `packages/core/daimon/core/stores/discord_message_admissions.py:67-71` |

| Bound or constant | Value and source |
| --- | --- |
| Workers | Two, the minimum to expose competing admissions; production has no two-worker cap (`claim_message` uses a row lock and a process owner key). |
| Message and gateway redelivery | One each, enough for a duplicate-turn witness; `bot.py:2116-2130` handles each gateway message. |
| Worker crash | At most one, to bound recovery interleavings; `runtime.py:61-78,92-137` owns the lock and `worker_ownership.py:7-23` detects its release. |
| Replay window | Enabled and covering the message in this model; code defaults to 0 minutes in `config.py:381-389` and the maximum is 60 minutes. |
| Turn count | Capped at two: a second turn is already the safety failure. |

| Config | Verdict | States | What it shows |
| --- | --- | ---: | --- |
| `ReplaySafe` | clean | 118 | A covered message eventually starts one turn under the stated fairness, even if its first owner exits. |
| `ReplayNoAdmission` | violates `NoDuplicate` | 50 | Removing the ledger check permits two turns after gateway redelivery. |

Counterexample: (1) Worker 1 sees the replayed mention and starts a turn. (2) Discord redelivers the same mention. (3) Without the admission check, worker 1 claims it again and starts a second turn. `packages/core/tests/stores/test_discord_message_admissions.py::test_two_startup_workers_admit_a_missed_message_once` replays concurrent claims from two real PostgreSQL owner sessions, a pending duplicate, and redelivery after completion. The existing `test_pending_followup_is_reclaimed_only_after_its_process_exits` covers the crash and reclaim path. No staging Discord contract test or production trace was run, so the liveness result remains provisional.

Unsupported upstream assumption: Discord returns the missed message in history when the replay window and channel limits cover it. The model excludes permission changes, protected channels, provisioning denial, transport failures, and a crash after thread creation but before card intent commit. It checks the stated bounds, not arbitrary production numbers.
