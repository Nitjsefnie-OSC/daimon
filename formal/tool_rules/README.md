# Agent tool rules

Question: can a gated third-party tool call run without the requester's approval when no
admin-set allow rule was in the store at the moment the call was decided, and can an
"Always allow" click undo a revoke that committed after the click?

This models the approval-card path. Per agent, a workspace admin can mark a
third-party tool `allow`, either from the CLI or with an "Always allow" button on its
approval card. A workspace admin may save a rule for the card's parent channel.
The decider reads the rule on every call; the card's click approves the
pending call at once, and the rule save is a compare-and-set on the revision captured
before the card was posted.

| Action | Code |
| --- | --- |
| `Decide` | `turn/approvals.py` `interactive_decider` after `decide_tool_call`, reading the rule store |
| `Approve`, `Refuse` | `posted_controls/lifecycle.settle_confirmation` (requester Approve, Deny, expiry) |
| `ClickStart`, `ClickFinish` | adapter `tool_confirmation.py` handlers: awaited live admin check, then `claim_admin` or `settle_local_admin_confirmation` |
| `CommitSave` | `posted_controls/tool_rule_confirmation.py` `save_always_allow`, then the store's compare-and-set write |
| `PanelAllow`, `Clear` | CLI or daimon-mcp rule edit; a clear bumps the revision even when no rule exists |

Scaling: two calls, one requester and one admin, two panel edits. Settling the card is one
step because `PendingConfirmations` resolves an in-process future; the admin check and the
save are separate steps because each awaits I/O.

Properties: `RunAuthorized` (a call runs on the requester's Approve, an admin's Always
allow, or a rule that was really in the store when it was decided), `RuleByAdmin`,
`NoResurrection` (a click from before a clear cannot restore the rule),
`SaveFollowsSettlement` (a click that lost to expiry or Deny saves nothing).

| Config | Verdict | States | Shows |
| --- | --- | ---: | --- |
| `ToolRulesSafe` | clean | 8906 | per-call read, admin-only click, save only after a won settle, compare-and-set |
| `ToolRulesRuleWitness` | violates `NoRuleRun` | 7 | a call does run on a rule without a card |
| `ToolRulesSnapshot` | violates `RunAuthorized` | 37 | rules read once at turn start: a call after a clear still runs without a card |
| `ToolRulesAnyClicker` | violates `RunAuthorized` | 61 | a non-admin's Always allow runs the call |
| `ToolRulesBlindUpsert` | violates `NoResurrection` | 553 | a click before a clear saves after it and the rule comes back |
| `ToolRulesSaveIfLost` | violates `SaveFollowsSettlement` | 534 | the admin check returns after expiry and still saves a rule |

Defined, not modelled: a call already decided is not recalled by a later clear. Managed
Agents batches several paused calls, and the driver decides each before sending the
confirmations, so call A can be allowed by a rule, a clear can commit while call B's card
waits, and A's allow is still sent. Also not modelled: explicit `deny` and `ask` rules and
server/tool precedence (pure, for unit tests), expiry timing, batching itself (see
`approvals/`), persistence failure and restart. Checked within these bounds, not proved for
production numbers.

Replay: `ToolRulesSnapshot` is
`test_live_lookup_sees_clear_on_next_call`; `ToolRulesBlindUpsert` is
`test_store_binding_history_and_clear_cas` (a stale compare-and-set after a clear fails);
`ToolRulesAnyClicker` is `test_any_clicker_cannot_claim_a_rule_scope` and adapter
non-admin click tests. `ToolRulesSaveIfLost` is
`test_save_if_lost_admin_check_returns_after_settlement`; `ToolRulesBlindUpsert`
also has `test_blind_upsert_and_scoped_saves` against the card snapshot and save path.
`RuleByAdmin` is `test_only_human_admin_may_edit` and
`test_agent_credentials_cannot_set_or_clear_even_with_admin_signal`. They are in
`packages/core/tests/test_agent_tool_rules.py` and
`packages/adapters/mcp/tests/test_agent_tool_rule_tools.py`. Card race tests are in
`packages/core/tests/posted_controls/test_tool_rule_confirmation.py`; interaction tests are in
each adapter's `test_tool_confirmation.py`. No staging trace has been checked yet.
`test_approved_click_without_save_has_no_rule_to_replay` checks the in-process
restart boundary; it does not reproduce a real process crash.
