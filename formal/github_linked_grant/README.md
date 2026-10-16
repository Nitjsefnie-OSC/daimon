# Linked grant commit fence (disposable spike)

Question: can an agent grant commit after management, link generation, installation coverage, or tenant association changes between a GitHub permission check and the database write?

| Model action | Code boundary |
| --- | --- |
| `GitHubCheck` | `github_linked_grant.py:125-193` fetches GitHub permissions and reaches `_before_commit`. |
| `RevokeManager` | `github_connect.py:382-404` supplies the live manager check; `github_linked_grant.py:221` applies it. |
| `Unlink` | `github_linked_grant.py:197-214` locks and checks the account link and generation. |
| `Suspend` | `github_linked_grant.py:199-220` locks and checks installation coverage. |
| `Disassociate` | `github_linked_grant.py:202-218` checks a tenant association. |
| `Commit` | `github_linked_grant.py:196-268` writes authorization and grant in one serializable transaction. |

All state variables are Boolean except link generation (0 initially, 1 after unlink) and phase (`start`, `checked`, `done`). These are bounded stand-ins for the real row values. No timeouts or network retries are modelled. `Safe` means a successful grant had all four prerequisites at its commit check. It does not claim that GitHub permission remains valid after the check. The model assumes the serializable transaction sees or conflicts with concurrent writes to the checked rows; this is supported by the PostgreSQL isolation level and row locks in the code, but an external concurrency stress run is UNVERIFIED.

`Safe.cfg`: 33 distinct states, clean. `NoRecheck.cfg`: 11 distinct states before `Safe` fails. Counterexample: GitHub check succeeds, manager rights are revoked, then a commit without rechecks grants the repo. The same manager-revocation sequence is replayed by `test_revocation_between_lookup_and_commit[manager]`; link unlink is replayed separately by `[link]` in `test_github_linked_grant_spike.py:385`. GitHub is `httpx.MockTransport` in those tests; no live GitHub contract or trace replay was run. The model result is provisional.

Run with `timeout 30s java -Xmx256m -cp /home/derick/.cache/daimon/tla/tla2tools.jar tlc2.TLC -workers 1 -deadlock -metadir /tmp/gh-linked-safe -config Safe.cfg Grant.tla` (use a different metadir and `NoRecheck.cfg` for the unsafe check). TLC 2.19. No CI job is added for this disposable spike.
