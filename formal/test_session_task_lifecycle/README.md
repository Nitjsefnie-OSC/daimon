# Discord test background-task cleanup

An `on_ready` test spawned replay and tenant-seed tasks, then let the fixture
roll back its database session without joining them. A task may still be using
the worker schema when the next test wipes it. The test now gives those tasks
engine-owned sessions and joins them in `finally` before fixture teardown.

| Model action | Code boundary |
| --- | --- |
| `Spawn` | `packages/adapters/discord/daimon/adapters/discord/bot.py:1711-1731` schedules replay and seed work with `_spawn` |
| `BeginDbWork` | `_replay_missed_messages` and `_seed_tenant_defaults` use `runtime.sessionmaker` |
| `UnsafeCleanup` / `JoinedCleanup` | `packages/testing/daimon/testing/db.py:466-472` rolls back the fixture connection; `test_bot.py` now joins `_bg_tasks` first |

| Config | Expected result | What it checks |
| --- | --- | --- |
| `Unsafe.cfg` | Violates `NoCleanupDuringTask` | Fixture cleanup can begin while spawned work is queued or using the DB. |
| `Safe.cfg` | Clean | Fixture cleanup is enabled only after spawned work finishes. |

The model has one bot task and one fixture cleanup. It omits SQLAlchemy and
PostgreSQL lock behavior; the targeted test plus a following DB-writing test
replays this boundary against PostgreSQL 18. The model checks ordering only,
not the production database implementation. This test leak is a concrete
isolation defect, but the available evidence does not establish it as the cause
of the CI lock stalls.
