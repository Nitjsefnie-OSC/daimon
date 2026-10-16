# Linked GitHub chat grant model

`GitHubChatGrant.tla` asks whether a chat request can save a grant after link identity, manager authority, or installation coverage changes. One intent and one request cross the browser, GitHub, and serializable database boundaries. Run `FORMAL_CHECK_FILTER=github_linked_grant formal/check.sh` from the repository root with the jar specified in [`formal/README.md`](../README.md).

## Action-to-code map

| Model action | Code boundary |
| --- | --- |
| `Mint`, `Open` | `packages/core/daimon/core/stores/github_personal_links.py:19` creates a one-use intent; `packages/adapters/mcp/daimon/adapters/mcp/oauth_github_personal.py:190` opens it. |
| `PlatformCallback`, `RejectForeign` | `packages/adapters/mcp/daimon/adapters/mcp/oauth_github_personal.py:262` verifies the chat identity before GitHub authorization. |
| `GitHubCallback`, `ReplayCallback` | `packages/adapters/mcp/daimon/adapters/mcp/oauth_github_personal.py:385` and `:467-490` save a verified link and consume the intent in one transaction. |
| `Request`, `CheckPermission` | `packages/adapters/mcp/daimon/adapters/mcp/tools/github_connect.py:297` checks manager policy; `packages/adapters/mcp/daimon/adapters/mcp/tools/github_linked_grant.py:90-252` uses the linked token to discover installation and repo permissions. |
| `Commit`, `RejectGrant` | `packages/adapters/mcp/daimon/adapters/mcp/tools/github_linked_grant.py:260-334` compares the generation paired with that token, installation coverage, and live manager policy inside a serializable transaction. |
| `RevokeManager`, `Unlink`, `Rotate`, `RemoveCoverage`, `RevokePermission` | `packages/core/daimon/core/github_panel.py:97` is the policy function; `packages/core/daimon/core/stores/github_links.py:90,239` rotates or unlinks; `packages/core/daimon/core/stores/github_installation_reconciliation.py:180` refreshes coverage. External permission revocation can happen between GitHub's answer and commit. |

The model uses two people, two GitHub users, two agents, two repos, one intent, and one request. These are finite bounds, not production sizes. The platform and GitHub calls are separate actions. The database recheck and commit are one atomic model action; live external authority can still change after the policy lookup answers but before the transaction commits.

| Config | Verdict | Distinct states | Meaning |
| --- | --- | ---: | --- |
| `clean` | clean | 5,435 | Link identity, single use, and commit authority hold within the bounds. |
| `no_platform_identity` | violates `NoForeignLink` | 140 | A forwarded link can attach another person's GitHub account. |
| `no_commit_recheck` | violates `GrantRestsOnCurrentAuthority` | 1,388 | A revoked manager, relink, or coverage change can be accepted. |
| `permission_gap` | violates `NoStalePermission` | 1,644 | GitHub permission can be revoked after its response and before commit. |

`EveryRequestResolves` uses weak fairness for permission checking and commit or rejection, with no timeout shortcut. The permission gap is a real limit: GitHub permission is not re-read inside the database transaction. The model does not cover multiple requests, OAuth provider failure, token refresh internals, repo renames, or read/write levels. Upstream staging contract tests and real trace validation remain unrun. Counterexample tests for relink and manager revocation are in `packages/adapters/mcp/tests/tools/test_github_linked_grant.py`; results are provisional for external services.
