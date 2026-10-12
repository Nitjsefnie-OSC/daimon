# Discord test database connection ownership

This small model captures the test fixture constraint: the background tenant
provisioning task and foreground message-admission completion may both own a
database transaction at once, so they must not share one connection.

| Model action | Code boundary |
| --- | --- |
| `Acquire("background")` | `packages/adapters/discord/daimon/adapters/discord/bot.py:1092` enters `set_provision_status` from `_seed_tenant_defaults`; `packages/core/daimon/core/stores/tenants.py:127-128` opens the transaction and executes the tenant update |
| `Acquire("foreground")` | `packages/adapters/discord/daimon/adapters/discord/bot.py:2496-2514` calls `_finish_admitted_messages`, then `finish_messages` and commits |
| `Release(task)` | Exit from each transaction context / foreground commit |

`DiscordTestConnectionsShared.cfg` intentionally violates `AtMostOneOwner`:
both logical tasks acquire `shared`. `DiscordTestConnectionsSafe.cfg` maps each
task to its own connection and checks cleanly. The regression test gates the
real background tenant update while the real foreground `finish_messages` path
runs, then verifies the two SQLAlchemy connections differ. With the old shared
fixture it fails that ownership assertion; with `db_nullpool_engine` it passes.

| Config | Verdict | Distinct states |
| --- | --- | ---: |
| `DiscordTestConnectionsShared.cfg` | Violates `AtMostOneOwner` | 4 |
| `DiscordTestConnectionsSafe.cfg` | Clean | 4 |

The model abstracts SQLAlchemy sessions to logical connection ownership. It
does not model PostgreSQL lock scheduling or claim that every shared-connection
overlap produces the CI's exact commit assertion. CI #816 observed that
assertion while the same test used the shared fixture; the deterministic replay
proves the test's real operations shared one connection before the fix and are
separate after it.
