# Offline catalog admin bindings

`daimon.testing.admin_bindings` translates frozen catalog admin steps into product service
calls on a disposable lane test database. It neither launches a shell nor reads
operator configuration/keys. Product `provision_tenant` creates the account and
tenant; config revisions, grants, rules, budgets, caps and routine cleanup use
product APIs. Separate DB sessions read the resulting state. Provider selection
is persisted by `configure_channel`, and the common operations run identically
for Anthropic, OpenAI and Gemini. The owned Anthropic SDK uses only
`ScriptedTransport` through `httpx.MockTransport` with an offline fixture key,
explicit empty webhook/header/query defaults, and no discovery. Any ambient
`ANTHROPIC_*` setting (including empty values) refuses before SDK/HTTP allocation
and provisioning; actual SDK `copy`/`with_options` also refuse late overrides.
The guard checks names only and never reads, logs or erases operator values.
Database guards require PostgreSQL on localhost/127.0.0.1 with no URL query
overrides and an exact `daimon_test_nc_*` lane name. The sole `daimon_test`
exception requires both `GITHUB_ACTIONS` and `CI` exactly `true`, plus
`build_test_engine` registration for a current-process worker/fresh schema
verified by `isolated_test_schema()`. Local shared/operator databases, plain
engines, forged execution-option metadata and foreign-process schemas refuse
before connection. Mismatched tenant/accounts and foreign destinations refuse
before writes. The runner must supply an explicit manifest of its owned
parent channels; this module does not infer platform channel creation or remap
threads.

N9 owns runner hooks, original operation order, deadlines, receipt capture and
all outcome assertions. N6 owns this module and
`tests/parity/test_qa_admin_bindings.py`. N2 owns the real credential-submit edge.
The pinned fixture retains 19 frozen source scenarios / 38 admin invocations,
source hashes and the frozen TARGET-53 digest. Source snapshots are not changed
to make an unsupported operation pass.

```python
binding = await provision_admin_run(
    sessions, workspace_id=owned_workspace, backend=selected_provider,
    channels=owned_parent_channel_ids, trace=scripted_anthropic_transport,
)
try:
    await binding.configure_channel(channel_id)
    receipt = await binding.execute(original_step, explicit_fixture_values)
finally:
    await binding.close()
```

An `AdminReceipt` is `executed`, `refused` or `pending`, with provider, operation,
observations and optional output fixture values. **Executed is operation evidence,
not a scenario PASS.** Unknown syntax/placeholders and missing edges return typed
pending; foreign scope errors propagate. The runner must stop at unbound setup
or step operations rather than run a turn against substitute state. Expansion
after tokenization prevents fixture data from adding options or shell commands.
Expected-failure handling belongs to the runner and cannot relabel an unrelated
refusal as success.

| Frozen scenario | Common admin binding, all three providers |
|---|---|
| QA-NEW13-WRITERS-NONE-NOT-SILENT | Rules set/restore; actual writers-none admission refusal |
| QA-NEW15-CHANNEL-BUDGET-EXHAUSTED | Budget set/clear with DB readback |
| QA-NEW6-TENANT-CAP-QUEUE | Tenant cap set/restore with product lookup |
| QA-NEW22-ROUTINE-OUTPUT-NOT-TRUNCATED | Tenant/channel-scoped routine teardown |
| QA-NEW23-ROUTINE-WITHOUT-DESTINATION | Tenant/channel-scoped routine teardown |
| QA-NEW35-ROUTINE-FAILURE-IS-REPORTED | Tenant/channel-scoped routine teardown |
| QA-D14-FORK-KEEPS-SKILLS | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-D5-UNBOUND-AGENT-EDIT-REFUSED | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-D7-BIG-WORKSPACE-CHECKPOINT | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-D8-GITHUB-CONNECT-OFFERED | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-ISO-TEAM-CHANNEL-ISOLATION | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-NEW12-AGENT-SWITCH-HANDOFF-COPY | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-NEW17-MCP-USABLE-NEXT-MESSAGE | Grant is bound; copy/cleanup edge is unbound |
| QA-NEW18-SKILL-ADD-YES-PLEASE | Grant is bound; copy/cleanup edge is unbound |
| QA-NEW19-SKILL-DELETE-WHILE-ATTACHED | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-NEW14-MISSING-THREAD-PERMISSION-COPY | ADMIN_PLATFORM_EDGE_UNBOUND |
| QA-NEW16-TENANT-CREDIT-DEPLETED-COPY | ADMIN_CREDIT_SOURCE_NOT_EXECUTABLE |
| QA-NEW27-MISTYPED-TOKEN-HEADLESS | ADMIN_TOOL_UNBOUND; N2 credential-submit edge |

Six scenarios × three providers have every **admin** invocation bound here,
including teardown. This removes 18 admin-bucket blockers when the runner binds
these APIs; it does not remove other source assertions, native fixtures, platform
observations or admission gaps. Resource operations require the pending neutral
catalog/resource-host dependencies, which this module does not copy or replace.
The optional `resource_edge(AdminCommand) -> AdminReceipt` is explicitly injected;
its absence is always pending. An injected edge must use the real product
resource services, retain scope and provider routing, and provide actual readback.
A callback's presence alone is not evidence or cell credit.

The frozen negative credit command contains `-<balance>`, which is not numeric;
the current CLI accepts only positive credits. This binding does not invent a
negative ledger mutation or silently choose an empty initial balance instead.
Native Gemini resource deletion also lacks a supported product edge; that
teardown cannot be certified as successful native deletion.
The current Gemini preparation edge refuses channel-scoped extra skills, and
the resource-host stack does not provide neutral channel-copy archive. Binding
an agent-scoped upload would not preserve the frozen channel-skill semantics.
These paths need their owning product edges before they can execute; the
optional callback does not waive those gaps.

Run the included focused tests on the lane DB. They also cover repeated product
provisioning/configuration, denied cross-tenant/channel operations, token/unknown
platform pending results, placeholder injection refusal, non-lane DB refusal,
and cleanup preserving another tenant and another channel's routines. There
are no live provider, Discord, Slack, Teams, tunnel or model calls.

Every sessionmaker destination is checked before I/O: the main bind and each per-model `binds` entry, including connection-bound engines, must satisfy the same owned-database and isolated-CI rule. An accepted main engine cannot authorize a foreign model route.
