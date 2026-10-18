# GitHub session tokens across installations

A session with grants in installations A and B needs a separate exact-scope token for each. A repo-aware git credential helper must return B's token for B's repo, and nothing for an uncovered repo. A change to A must not revoke B's unaffected token. These are proposed behavior; the current implementation still rejects a second installation and delivers one `GH_TOKEN`.

## Models and code boundaries

| Model action or state | Code boundary / status |
| --- | --- |
| `GrantInstall.LockA/B`, `WriteA/B` | `packages/core/daimon/core/stores/github_access.py:406-415,444-470`; proposed removal of the cross-installation rejection at `:416-428` |
| `GrantInstall.UnsafeReadA/B`, `UnsafeWriteA/B` | Rejected split read/write of the same grant operation; `lock_agent_grants` at `github_access.py:406` prevents this |
| `MultiInstallation.Init`, `Eligible`, `ExactLiveScope` | Current token groups by installation and exact repository IDs at `packages/core/daimon/core/github_app_session.py:401-452`; inventory records installation, repo IDs, grant versions, and expiry at `packages/core/daimon/core/stores/github_issued_tokens.py:90-136` |
| `Read(r)`, `Return` | Proposed helper map read and output as separate actions. Current `github_app_session.py:504-513,629-632` gives every group the same name and delivers one `GH_TOKEN`; it cannot route across installations |
| `RevokeA` | Proposed per-installation hard-change invalidation; current grant deletion is `github_access.py:495-500`, with token-row stale recheck at `github_issued_tokens.py:550-566` |
| `ReassignA` | Proposed reassignment of a repo to another installation, preserving its URL. Current snapshot stores URLs only at `packages/core/daimon/core/session_snapshot.py:143-145` and compares only URL sets at `session_compat.py:173-177` |
| `AddRepoB` | Proposed mid-session B grant and exact B-token remint. Current grant write is `github_access.py:444-470`; mint groups repo IDs at `github_app_session.py:401-452`. The code still filters to one installation at `:289-309` |
| `Expire(i)`, `Rotate(i)` | Conservative 55-minute expiry at `github_app_session.py:428-440`; token rotation entry at `:1133-1164`; stale/expiry row check at `github_issued_tokens.py:550-566` |
| `SessionToken.VersionBump`, `Mint`, `SwapResource`, `SwapVault` | Existing single-token lifecycle: grant version update at `github_access.py:454-463`; mint at `github_app_session.py:428-452`; resource/vault swaps at `github_app_session.py:972-1008` |
| `SessionToken.LockDelivery`, `Deliver`, `LockRetire`, `RetireOld` | Grant lock and delivery rows at `github_app_session.py:703-728`; locked retirement at `github_app_session.py:1017-1034` |
| `SessionToken.SelectSweep`, `RecheckSweep`, `RestoreOld`, `RemoveGrant` | Token-row recheck at `github_issued_tokens.py:550-566`; grant removal at `github_access.py:495-500`; rollback restoration at `github_app_session.py:1036-1084` |
| `SessionToken.Crash`, `Unsafe*` | Existing crash, cleanup, rollback and long-lock fault witnesses; see `SessionToken.tla` and the prior single-token lifecycle paths above |

`GrantInstall` allows both installations and checks that completed grants persist. `MultiInstallation` has token scope, validity, expiry and generation per installation. `Read` captures the helper's token selection; `Return` may run after a revocation. A fresh read never selects an expired or revoked token. A token selected before revocation can still be returned, but `ResultScoped` requires the captured token to have belonged to the requested repository and installation. `NoStaleUsable` assumes GitHub invalidates the token at revocation, so the returned placeholder cannot authenticate. `AddRepoB` re-mints B with its new exact scope and leaves A unchanged. `SessionToken` retains the existing detailed mint/delivery/rollback race checks for one token. The multi-installation model treats each token's rotation as one atomic transition.

The model's token ID is an abstraction for a vault credential, not a secret value present in the sandbox. The proposed helper returns the selected vault variable's placeholder; GitHub request-header injection supplies the real token (`packages/core/daimon/core/github_app_session.py:649-664`; `packages/core/daimon/core/credential_env.py:151-157`). `NoStaleUsable` describes whether GitHub accepts that placeholder after substitution.

## Bounds and assumptions

| Item | Model bound | Source / limit |
| --- | --- | --- |
| Installations and repos | Two installations; A starts with `a`, B with `b`; `c` can be granted in B and `a` can move to B | `github_app_session.py:401-408` groups arbitrary live rows; this bound is a model choice |
| Token generations | Two per installation | Model choice for one rotation or B re-mint; production has no two-generation cap |
| Expiry | A Boolean per token, with independent `Expire(A)` and `Expire(B)` | Production uses a conservative 55-minute expiry (`github_app_session.py:428-440`) and refreshes after 40 minutes (`session_compat.py:249-259`); wall-clock time is omitted |
| Repository inventory | At most three repos | Production accepts 1..500 IDs per token (`github_issued_tokens.py:108-109`) |
| Vault credentials | Omitted | Design uses one header-injected credential per token chunk plus a working-repo `GH_TOKEN` alias; the current cap is 15 (`github_app_session.py:409-410,649-664`) |
| Helper read and return | Two actions, with grant/revoke/expiry/rotation allowed between them | Proposed helper. Current delivery has only one `GH_TOKEN` (`github_app_session.py:629-632`) |
| Upstream revocation | `invalidated` gains the old token ID at the same model step as grant removal | Unsupported atomicity assumption. Real GitHub revocation is a network call (`github_app_session.py:312-315`); a failed call or a commit-to-sweep delay is not modeled |

`NoStaleUsable` is conditional on that upstream revocation assumption. The `MultiNoUpstreamRevoke` counterexample shows the gap: the helper reads A, A's grant is removed, and it returns A's placeholder while GitHub still accepts the underlying token. The result is provisional until a contract test and a real execution replay verify revocation timing. The repository-to-installation mapping is assumed unambiguous at lookup time. `UnrelatedSurvives` checks A revocation against B; the symmetric B case is assumed. The existing single-token race model does not re-prove full-set delivery and rollback.

## TLC results

Run `TLA2TOOLS_JAR=/home/derick/scratch/daimon-os-devenv/tla2tools.jar FORMAL_CHECK_FILTER=github_session_token formal/check.sh`. The checker uses one TLC worker, a timeout, and a metadir cap. `formal/expected.tsv` pins verdicts and distinct-state counts.

| Config | Verdict | States | User-visible failure shown |
| --- | --- | ---: | --- |
| `GrantSafe` | clean | 8 | Both concurrent grants persist |
| `GrantUnsafe` | `CompletedGrantsPersist` | 31 | A stale write loses a granted installation |
| `MultiSafe` | clean | 5,400 | Exact scope, independent expiry/rotation, B re-mint, and read/return race |
| `MultiSingleToken` | `GrantedReachable` | 3 | B repo cannot be reached after the guard is removed |
| `MultiFallback` | `ResultScoped` | 28 | Helper returns A's token for uncovered B repo `c` |
| `MultiRevokeAll` | `UnrelatedSurvives` | 5 | A revocation also invalidates B's token |
| `MultiOldFingerprint` | `FingerprintExact` | 5 | Reassignment keeps the old map fingerprint |
| `MultiExpiredLookup` | `FreshRead` | 55 | Helper reads A's token after A expires |
| `MultiExpireAll` | `ExpiryIsolation` | 8 | A expiry also invalidates B |
| `MultiRotationStaleB` | `RotationRefreshes` | 11 | B rotation leaves B's old token in place |
| `MultiGrantStaleScope` | `ExactLiveScope` | 7 | New B grant leaves B's live token without `c` |
| `MultiGrantOldFingerprint` | `FingerprintExact` | 7 | New B grant leaves the old mapping fingerprint |
| `MultiGrantTouchesA` | `GrantIsolation` | 7 | New B grant kills A's token |
| `MultiNoUpstreamRevoke` | `NoStaleUsable` | 94 | Read, revoke, return yields a token GitHub still accepts |

The existing `SessionToken` configs remain in `formal/expected.tsv`: `Safe` is clean at 50 states, and each unsafe config still witnesses its named lifecycle invariant. `GrantUnsafe` loses one completed grant when two writers act on stale reads. The new broken configs each exercise the property named in the verdict column.
