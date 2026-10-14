# Live GitHub session token

The models cover one agent, one live session, one old token, and one replacement. `SessionToken.tla` separates mint, resource update, vault update, inventory delivery, and retirement. Both the vault and repository resource must point to a live token while access is unchanged. A crash may occur while the replacement row is still provisional. A stale sweep may select an old token before a rollback restores it.

| Model action | Code boundary |
| --- | --- |
| `VersionBump` / unchanged re-grant | `packages/core/daimon/core/stores/github_access.py:403`; unchanged rows return without changing `version` |
| `Mint` | `packages/core/daimon/core/github_app_session.py:332` |
| `SwapResource`, `SwapVault` | `packages/core/daimon/core/github_app_session.py:802` and `:819` |
| `LockDelivery`, `Deliver` | `finish_app_delivery` takes the grant lock, checks token versions and writes delivery rows in one short transaction |
| `LockRetire`, `RetireOld` | `rotate_live_app_tokens` takes the grant lock, rechecks versions and marks old rows due in one short transaction |
| `UnsafeLongLock` | The former rotation wrapper held a transaction and grant lock across GitHub mint and MA calls |
| `Crash`, `UnsafeCrash` | Provisional cleanup in `packages/core/daimon/core/stores/github_issued_tokens.py:418` waits through GitHub expiry |
| `SelectSweep`, `RecheckSweep` | `packages/adapters/scheduler/daimon/adapters/scheduler/main.py:656` and `packages/core/daimon/core/stores/github_issued_tokens.py:555` |
| `RestoreOld`, `UnsafeRemoved` | `packages/core/daimon/core/github_app_session.py:850`; a hard-stale old token is not restored |
| `UnsafePartialCleanup` | `packages/core/daimon/core/stores/github_issued_tokens.py:358`; a resource-delivered replacement stays live when current grants permit it |
| `RemoveGrant` | Hard-change revocation in the scheduler sweep after `packages/core/daimon/core/stores/github_access.py:509` removes the grant |
| `LockA/B`, `WriteA/B` | `packages/core/daimon/core/stores/github_access.py:61` and `:403`; one transaction lock covers check and write |

`LiveSessionHasToken` requires live vault and resource credentials while access is unchanged. `RemovedAccessInvalid` requires old and new tokens to be invalid once the hard-change sweep completes. `NoNetworkUnderLock` requires mint and MA calls to run without a grant lock. `GrantInstall.tla` requires at most one installation. The safe configs are clean. Each unsafe config reproduces one ordering error: version-based revocation, revocation of either token after a resource-only swap, provisional cleanup after a vault swap, stale-sweep revocation after rollback, restoration of removed access, a network call under the grant lock, or two first-grant writes based on the same empty read.

TLC 2.19 explores 50 distinct states for `Safe`. `UnsafeLongLock` violates `NoNetworkUnderLock` after 5 distinct states. The remaining verdicts and counts are pinned in `formal/expected.tsv`.

Database replays are in `packages/core/tests/test_github_access_foundation.py`, `packages/core/tests/test_github_connect.py`, and `packages/adapters/scheduler/tests/test_main.py`. They cover parallel grant writes, a provisional token after crash, a sweep recheck after restore, active-turn partial delivery, grant-lock release during a blocked mint, external-session exclusion, and discovery when all token rows are revoked.

Bounds and assumption: GitHub mint and successful MA resource/vault updates are atomic individual effects. The removal action represents the completed hard-change sweep, not the interval between a grant commit and the scheduler's next sweep. The models do not prove immediate GitHub revocation at the database commit boundary; that would require synchronous external revocation in every grant-removal path.
