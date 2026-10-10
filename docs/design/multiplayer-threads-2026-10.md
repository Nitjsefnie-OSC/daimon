# Shared threads and per-sender access

Proposal for review, 2026-10-10. This feature is not implemented.
This document requests a design decision, not permission to merge or deploy.
Application observations refer to commit `15da4c956`.

## 1. Recommendation and decision

Create an opt-in shared thread with a fresh sandbox. Everyone who can read that
thread must be allowed to read everything put in its sandbox and history.
For a later release, authorize external operations in Daimon's backend against
the recorded sender and an explicit team grant. Return the permitted result;
keep credentials in the backend.
The later external-action release starts with a small set of GitHub operations
against one explicitly shared working repo. Keep personal connectors and raw
CLI credentials out of this mode.

A shared sandbox is useful for a team that trusts each other with its working
files. It cannot preserve
private files, tool results, or instructions between those teammates. Correct
credential selection does not change that fact.

The approval requested is for this direction and the first slice in section 8:
a credential-free shared workspace, followed by a separate gate for external actions.
Implementation requires separate design approval. Personal sessions remain
available for private work. There is no automatic conversion of existing threads.

## 2. What current MA documentation establishes

All sources below were retrieved on **2026-10-10**. "Documented" means the
current official page states it. "Observed" below means local source inspection,
not a live MA contract test. "Proposed" and "unknown" are not vendor guarantees.
The product is Claude Managed Agents (MA), including its cloud environment;
Claude Code cloud and the Agent SDK are different products.

| ID | Status and finding | Primary source, retrieved 2026-10-10 | Design consequence |
| --- | --- | --- | --- |
| D1 | Documented: a session keeps conversation history across interactions; creation and sending work are separate operations. | [Start a session](https://platform.claude.com/docs/en/managed-agents/sessions) | Daimon can route admitted people to one stored session ID. This is an application design inference, not native human membership support. |
| D2 | Documented: a cloud environment can serve many sessions, each with a separate container. | [Cloud environment setup](https://platform.claude.com/docs/en/managed-agents/environments) | Reusing an environment does not share working files. |
| D3 | Documented: vault selection is per session, credentials are workspace-scoped, and values are write-only. Matching MCP URLs choose a credential; the first matching vault wins. Rotation is periodically re-resolved in running sessions. Environment credentials substitute opaque placeholders at egress; returned exchange tokens are not redacted. | [Authenticate with vaults](https://platform.claude.com/docs/en/managed-agents/vaults) | Neither a list of users' vaults nor periodic rotation selects the message sender. No atomic rotation barrier is documented. |
| D4 | Documented: the update API exposes `vault_ids` but explicitly rejects requests setting it. | [Update Session, request body](https://platform.claude.com/docs/en/api/beta/sessions/update) | Do not implement vault swapping from the SDK field's mere existence. |
| D5 | Documented: `tools` and `mcp_servers` can be replaced for an idle session; an open workflow run prevents that update. Model, system configuration and skills cannot be replaced this way. | [Session operations](https://platform.claude.com/docs/en/managed-agents/session-operations) | Tool changes are possible, but idle alone is not a safe human-turn boundary. |
| D6 | Documented: server configuration names a URL; session vaults supply authentication. MCP failures surface through session events and retry on a later idle-to-running transition. | [MCP connector](https://platform.claude.com/docs/en/managed-agents/mcp-connector) | The reviewed connector contract does not supply an authenticated Daimon sender per tool request. A prompt's username is insufficient. |
| D7 | Documented: custom tools emit requests for the application to execute. | [Tools, custom tools](https://platform.claude.com/docs/en/managed-agents/tools) | Backend execution can keep service credentials outside the shared sandbox. |
| D8 | Documented: custom results reference the custom tool event ID; repeated results for one call have no effect. `requires_action` is an idle pause, not completion. User events can be queued; `processed_at` distinguishes their processing. Sandbox checkpoints persist across interactions but expire 30 days after sandbox creation, without activity extending the window. | [Session event stream](https://platform.claude.com/docs/en/managed-agents/events-and-streaming) | Persist the call-to-work association and reconcile history. API result deduplication does not deduplicate our external side effect. Export important work. |
| D9 | Documented: multiagent threads share the sandbox, filesystem and vault credentials, despite separate conversational contexts. | [Multiagent orchestration](https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration) | These are agent execution threads, not isolated human identities. Disable subagents and workflows for the first shared profile. |
| D10 | Documented: session status aggregates activity across threads. Blocking events can be forwarded to the primary thread. | [Session threads](https://platform.claude.com/docs/en/managed-agents/session-threads) | "MA has one turn at a time" is unsafe as a general assumption. |
| D11 | Documented: uploaded files mount as read-only copies; resources can be added/deleted during a session. Outputs become downloadable after asynchronous publication. | [Adding files](https://platform.claude.com/docs/en/managed-agents/files) | Shared files are feasible; removing a mount does not promise erasure of copies or conversation content. |
| D12 | Documented: GitHub resources accept a clone authorization token, not echoed in responses; mounting a repo also loads its root skills at startup. | [Accessing GitHub](https://platform.claude.com/docs/en/managed-agents/github) | A clone mount is an additional access path; review it separately from MCP. Treat repository instructions as untrusted input. |
| D13 | Documented: custom tools are controlled by the application, outside MA permission policies. `auto` is not a human checkpoint; relayed `user.message` content counts as intent. | [Permission policies](https://platform.claude.com/docs/en/managed-agents/permission-policies) | Build authorization into backend dispatch. A tool confirmation cannot establish which person owns the credential. |
| D14 | Documented: memory stores can belong to users, teams or projects; mounting one store in multiple sessions shares its contents. Read-only mounts are supported. | [Using agent memory](https://platform.claude.com/docs/en/managed-agents/memory) | Existing agent memory must not silently seed a different audience. |

Unknown: a contractual causal link from every custom tool event to the exact
human message across interruption, delayed events and reconnect. Event IDs
identify calls, not human authors. The [Send Events request schema](https://platform.claude.com/docs/en/api/beta/sessions/events/send)
(retrieved 2026-10-10) has message content and tool-result references, but no
authenticated human actor field. Absence is limited to the reviewed contract.
No live request was made to test attribution behavior.

## 3. Current application boundary

Source inspection at `15da4c956` establishes that personal thread lookup matches
`(tenant, platform, thread, account)`. Teammates in one platform thread therefore
have separate session mappings and sandboxes. The proposed shared path preserves
that personal lookup and its account/NULL isolation tests.

Session ownership currently participates in credential setup, billing, recovery
and artifact access. Shared mode must distinguish the requester of each action
from the session creator and the provider principal. A provider principal is the
account or application that the external service records as performing an action.
The design below specifies that boundary without reusing personal session state.

## 4. Options and costs

Estimates are engineering days for one implementer plus review, not measured
benchmarks. Include tests, migration and Discord/Slack work; exclude calendar
waits for product acceptance. Teams remains out of scope.

| Option | Thread experience | Credentials, audit and data boundary | Invariants and migration | Failure radius | Estimate |
| --- | --- | --- | --- | --- | --- |
| 1. Share only through the working repo | Each person keeps a sandbox. Commit/push a checkpoint; teammate pulls it and continues. Unsaved files, installed state and conversation diverge. | Existing identity/lifetime behavior; GitHub records the actual credential principal. Only deliberately committed data is shared through the repo, though chat is already visible to its readers. | No session migration. Name a branch, checkpoint commit and author at each handoff; review before pulling. | Wrong branch, merge conflicts, accidentally committed private data; existing credential risks remain. | 0.5-1 day for a tested guide. Useful immediately, does not meet one-sandbox acceptance. |
| 2. Creator lends their session and credentials | Everyone works in the creator's existing workspace; permanent notice names the credential owner. | Every teammate can cause actions with that owner's effective rights. Audit distinguishes requester from owner, but cannot turn this into per-sender access. Existing token lifetimes remain; historic files and secrets are exposed. | Opt-in shared lookup replaces per-account isolation for that mapping. Would require owner consent, membership, revocation and migration controls. | Creator's repositories, personal connectors, historic sandbox contents and broadly scoped credentials. A notice does not constrain them. | 2-4 days prototype; 5-8 hardened. Reject. |
| 3. Shared session; switch credentials by current turn | One workspace, sender label on each turn. | Vault swap is unsupported (D4); rotating contents has no atomic boundary (D3). Raw short-lived tokens still survive in shared files/processes until expiry. A global current-account lookup can misattribute delayed requests. | New session sharing plus durable turn ownership, revocation and audit. Cannot reuse caller vaults. | Cross-person credential use and shared private data, including requests delayed into the next person's turn. | Naive 4-6 days is unsafe; proposed backend subset below is 12-20. Reject literal vault/token switching. |
| 4. Shared context; backend executes sender-bound operations (recommended) | One fresh workspace; queued work retains its sender. Team-safe files persist; private actions stay in personal sessions. | Backend checks the recorded sender, permitted operation and shared resource. Tokens remain server-side. Provider principal is the GitHub App installation; the sender determines permitted token scope and is a separate audit field. | Add explicit shared mode, audience and policy version; retain personal mappings. No in-place conversion or private history import. | Shared workspace and explicitly allowed resources. Backend bug could still reach stored credentials, so this dispatcher has a narrow operation allowlist. | 12-20 days in slices; 5-8 for the credential-free file pilot. |
| 5. Shared workspace under a dedicated team identity | Same workspace; tools visibly act as the team. | Fresh, least-privileged team credentials, never a teammate's personal grant. Audit logs requester plus team principal. Lifetimes follow each provider; revoke the shared grant to stop future use. | Same membership and migration rules; simpler execution identity, but requires an explicit team delegation policy. | All permitted team resources, accessible to any admitted teammate. | 5-9 days. Viable fallback if per-sender external actions are deferred; do not silently substitute it. |

For option 1, use one working repo and a named handoff branch. The sender commits
only reviewed deliverables, posts the commit SHA, and the next person pulls that
SHA after reviewing the diff. This preserves useful work now without claiming a
shared runtime. Never commit `.env`, caches, tokens or personal connector output.

## 5. Proposed shared mode

### 5.1 Audience and start

A person authorized to manage this thread's agent can request shared mode in
plain language. That clear instruction suffices; no extra "say yes" gate for
enabling it. Backend handlers bind the instruction to its platform sender.
Shared model tools cannot enable sharing, invite members, change policy, grant
repos or select a different agent. If the audience or repo is ambiguous, resolve
that ambiguity first. External write approvals remain separate.

Create a fresh shared session and fresh credential configuration. Keep old
personal sessions private. Bring across only named files or a reviewed repo
checkpoint after checking their destination audience; do not copy full history,
home directories, `.env`, memory stores, caches or arbitrary workspace bundles.

The security audience includes **all thread readers**, even people who never
send a message. Store the reader policy and verify current platform access on
admission, tool execution and artifact access. Never assume a private Discord
thread or Slack channel is readable only by the explicit participant roster.
The pilot supports private Discord threads with a verified parent-channel policy,
and private Slack channels without external/Slack Connect members. Unknown
visibility or unsupported membership models prevent admission. Record the
authorizing administrator or grant, repo ID, policy version and timestamp.
A verified repo administrator or an existing baseline grant that explicitly
authorizes this audience must permit distribution to the channel, including
its membership policy; ordinary permission to read a repo does not grant permission
to share it. The pilot accepts only resources approved for that durable audience.
An intersection of today's readers' personal rights is insufficient: a platform
invite can expose old chat before Daimon sees the change. If reader access expands
outside the approved policy or cannot be verified, freeze new work and delivery.
This cannot retract history the platform already revealed. Removal stops future
use; it cannot make a person forget downloaded files or previously visible chat.

Proposed notice once external operations pass their release gate:

> Shared workspace
>
> Everyone who can read this thread can see its work and files.
>
> Connected actions check the access of the person asking.
>
> Private connections stay in personal chats.

Controls: `Members and access`, `Stop sharing`. Show the working repo separately.
The acting card says `Working for Alice`; Bob's queued card says `Waiting for Alice`.
Access details state `Team access: <allowed operations>` and, where used,
`Extra access: Alice` on separate lines.
These lines describe application attribution, not a promise that GitHub records
Alice as the API principal. For an App operation, details say `Requested by Alice`
and `GitHub App: <app name>` on separate lines. While the first pilot has no
external actions, replace the two connection lines with `Connected actions are
not available in this shared workspace yet.`

### 5.2 Work ownership and execution

Persist each accepted message with tenant, platform message ID, sender account,
shared mapping ID, policy version and a monotonically increasing execution epoch.
An epoch is a number that distinguishes one execution from its replacement.
Unique platform message IDs prevent duplicate delivery from creating new work.

Use a durable shared-session admission record and a database uniqueness rule for
one active execution. Keep each message's arrival order; batch only contiguous
messages from the same author. Global/tenant admission limits still apply.
Do not send a teammate's message
to discover whether MA is busy. Shared mode must distinguish a live owner from
an orphan before recovery; recovery cannot cancel another person's live approval. A
pending Bob message stays in Daimon's queue while Alice's execution is active;
it is not sent to MA as steering for Alice's work.

A dedicated shared agent profile has subagents, workflows, routines and automatic
continuations disabled. It exposes a minimal custom-tool set and approved local
file/compute tools. Do not inherit an arbitrary existing agent's connectors,
long-lived credentials, memory or multiagent configuration. Session creation
must assert the effective configuration before sending any user event: zero
session/caller vaults, zero MCP servers (including Daimon MCP), no inherited
environment credentials or `.env`, no `github_repository` resource, no existing
memory, and the exact tool/skill allowlist. Upload a reviewed repo snapshot as
files instead of supplying a clone token. Unexpected configuration stops creation.
Repository snapshots exclude git configuration, hooks, credentials and executable
setup; imported archives reject unsafe paths and symlinks. Approved source files
remain untrusted code.

For each custom-tool event, the controller persists the immutable association
`(tenant, session, MA event ID) -> execution epoch -> sender` before dispatch.
A tool-supplied account, username, session ID or header cannot select identity.
The backend rechecks current membership, sender authorization, resource audience,
operation allowlist, epoch validity and any required approval. Deny if any fact
is missing or stale. Recheck immediately before the external request.

The sender account is attribution, not proof that the model's proposed action
matches that person's intent. Teammates can leave hostile files or instructions.
The first external write tool therefore presents the exact repo, branch and
patch/action to the sender under existing approval policy. The backend computes
the diff from an immutable snapshot, records its hash, and sends exactly that
approved payload; it does not trust a model-written summary. Any payload change
invalidates approval. Never authorize a
write just because a shared file requests it. Read tools are restricted to
resources approved for this shared audience.

### 5.3 Credentials and tools

| Surface | Pilot (slice 1) | External operations and later expansion |
| --- | --- | --- |
| Daimon configuration/admin tools | No ambient admin identity, ordinary MCP surface or authenticated custom operations. Management comes from human platform controls. | Reuse core authorization with explicit sender and place; prevent agent/account switching and permission expansion through shared context. |
| GitHub | App flow only. One working repo; backend imports an approved snapshot at creation, without exposing its credential. Local edits stay shared; no external operation tool. | Slice 2 adds backend operations. Push/PR creation checks live access and immutable reviewed payload. Other repos stay token-only in the ordinary product; shared mode does not expose those tokens and needs a backend operation instead. |
| `get_cli_token`, PAT/Google raw token, personal OAuth | Disabled and unavailable to shared profile. No service-default fallback. | Add operation proxies, not token export. Unsupported personal work goes to an isolated personal session. |
| Agent-shared external MCP | Off for the pilot. | Allow only with an explicit service identity, resource scope and audience policy. The fact that a token is agent-scoped is not enough. Label service principal in audit. |
| Agent `.env` and memory | No inherited `.env` or existing agent memory. | New thread-scoped, explicitly shared resources only; review each connector and secret before enabling. |
| Files, notebooks and reports | All imports/exports check tenant and current audience. | Preview URLs, background notebook kernels and published reports must preserve the same audience; a private session file ID is not authorization. |

The chosen GitHub credential is an **App installation token**, restricted to the
one requested repo and the operation's required permissions. For slice 2, choose
**team baseline authority plus sender-dependent elevation**, preserving the
existing `max(baseline, min(agent ceiling, sender permission))` rule. Baseline
permissions can exceed the sender's personal rights and the agent ceiling; the
ceiling caps only sender-derived elevation in today's rule. Shared mode does not
silently change that policy. A baseline grant must explicitly authorize both the
thread's durable audience and the operation; otherwise it contributes no shared
permission. Intersect the resulting scope with the shared operation allowlist.
An authorized teammate without linked GitHub rights may therefore request an
operation covered by the approved team baseline. Record `authorization_source`
(baseline grant or sender-derived scope), grant/policy version, effective
permissions, requester and provider principal for every dispatch. GitHub records
the App, not the human. User-to-server tokens acting as the human are a different
product choice and are not proposed here. A linked user's permission can raise
only that request's scope; it never raises the sandbox's ambient access.

Tokens stay server-side and use provider-specific expiry/revocation rules.
The dispatcher uses the returned provider expiry and revokes a single-use token
when practical. It cannot assume that all credential types have short lifetimes.
Use a durable call record and fencing checks directly, without
inventing an internal bearer grant. A later remote executor would need its own
short-lived scoped authorization design.

Return fixed output schemas for approved resource IDs. Do not offer generic HTTP,
SQL, shell or arbitrary URL proxying. Redact provider errors and exclude token
exchange endpoints and credential-bearing fields. No refresh token or service
token is returned in tool results, errors or logs.

### 5.4 Completion, approval, crash and cancellation

`requires_action`, disconnect and stream silence do not release ownership.
An approval remains attached to its original sender, tool event and exact
operation. Only that sender can approve an operation. Ordinary Stop remains requester-only;
an authorized manager can freeze sharing through the management surface, but
cannot borrow the sender's authorization or approve that sender's pending call. Stop closes the epoch to new dispatch
first, denies unresolved calls, requests upstream interruption, and reconciles
all outstanding executions before admitting the next sender. Start/disallow
dispatch and revocation serialize on the same durable record. A request admitted
before revocation may still reach the provider; do not promise to cancel it.

A timeout does not prove an external request failed. Store `prepared`,
`dispatched`, `succeeded`, `failed` or `unknown` per side effect. Use a provider
idempotency key where supported; otherwise reconcile before retrying. An unknown
write outcome freezes that operation rather than blindly replaying it.

Release the epoch only after the controller has reconciled the event history,
terminal state, pending custom calls, approvals and in-flight external requests.
No open workflows or child threads are allowed. A crashed worker cannot acquire
a new sender's identity: old work remains bound to its old epoch and is denied
once fenced out. After restart, unresolved provenance keeps the session frozen.
Proposed attribution uses exclusive execution windows: one submitted user event,
its persisted acknowledgment and processed state, reconciled history, terminal
`end_turn`, no outstanding calls and no child/workflow work before the next event.
This is an application hypothesis requiring upstream evidence, not a documented
causal ID. If the boundary remains ambiguous, per-sender operations do not ship
in a persistent shared session. In particular, an old, previously unseen call
first discovered after a new epoch starts must never acquire the new sender.
The release gate requires a documented upstream causal/order boundary that
classifies that call, or a revised design with independent platform authorization
of each exact operation by its human authorizer. That alternative changes the
interaction and needs a design amendment. Passing six traces is insufficient.
Any remaining upstream assumption must be stated and accepted in that
amendment before slice 2 is enabled. The credential-free file pilot can proceed.

### 5.5 Data and migration invariants

1. Personal lookup still requires exact account equality, including the old NULL-row rule.
2. Shared lookup requires tenant, platform, thread, explicit mode, live membership,
   verified reader policy and the configured agent. Never use an unscoped fallback.
3. One live shared mapping and one active epoch per thread; no new private work
   silently joins it. Historic personal rows remain separate and auditable.
4. Every action has one immutable requester and provider principal. Approvals,
   retries, cost attribution and recovery retain them.
5. No personal bearer credential enters shared state. Results use fixed schemas
   from resources authorized for the channel audience. This enforces provenance,
   not a claim that software can recognize all sensitive content.
6. Revocation fences new dispatch admissions after its durable commit. An
   already admitted external effect may finish; report it accurately.
7. Disabling sharing stops queued work and future shared access. New personal
   sessions start fresh; do not copy shared/private histories automatically.

Add a separate shared-thread mapping, membership/policy history and execution
journal rather than overloading NULL `account_id`. Create the schema with the
feature off. Existing rows require no backfill. App-session vault registration,
repo removal/rebinding, billing sweeps, file downloads, deletion and orphan
recovery must be audited for assumptions that session owner equals requester.
When shared mode is live, all new messages in that platform thread use the shared
route. There is no parallel personal fallback in that thread. Feature rollback
freezes shared mappings instead of resuming pre-share personal rows. A later
explicit personal start creates a fresh mapping; private work moves to a personal
conversation. Preserve history for audit and export shared work before retirement.

Usage is charged to the tenant under its current policy, with the actual requester
recorded per execution; one creator's billing exemption must not cover teammates.

## 6. Threats and limits

The safety claim is access control and accurate attribution among collaborators
who share a data audience. It does not promise isolation from another teammate's
code or prompt injection. A teammate can alter a shared script before someone
runs it; shell children may continue mutating shared state. Their lifecycle is
not established here. They have no backend credential or custom-tool ingress,
but can poison files that affect later model decisions. Keeping tokens server-side limits credential theft, but authorization
still needs exact resource and operation restrictions and review of writes.

Worst failures are cross-tenant mapping reuse, a stale event rebound to the next
sender, use of an inherited personal connector, public artifact delivery, and
recovery that repeats a write. These are release blockers. A shared-state leak
cannot be repaired by rotating a credential after the data has been read.

## 7. Verification plan, not a proof

No TLA+ result or runtime safety claim is asserted in this design. Before wiring
per-sender operations, model admission, dispatch, approval, stop, expiry, replay,
reconnect and worker replacement. Map each action to actual awaits/transactions
in `prepare.py`, `driver.py`, `thread_queue.py` and the new dispatcher.

Use two users, two workers, two tenants, two epochs and duplicate/delayed events.
Require: no cross-tenant result; no call changes sender; stale epochs cannot
start effects; at most one active shared execution; no duplicate external effect
when the provider supports reconciliation; every queued item completes or gets
an explicit terminal outcome under stated fairness assumptions.

Keep broken variants that deliberately use a mutable `current_account`, rotate
a shared vault, release on `requires_action`, and retry an unknown write. Replay
each counterexample against real application tests. Any MA behavior assumed by
the model requires a staging contract test before calling the result verified.
Model checking alone cannot establish how MA behaves.

## 8. Slices and acceptance

| Slice | Estimate | Acceptance before proceeding |
| --- | --- | --- |
| 1. Credential-free shared compute/files pilot | 5-8 days | Fresh opt-in Discord/Slack session with the explicit configuration assertions above, verified audience, one approved working-repo snapshot, ordered queue and visible sender. Alice writes a file; Bob edits the same file. Outsider, tenant mismatch and unknown audience are denied. Personal isolation still passes. Model simultaneous creation, ownership replacement, restart and stop; replay local counterexamples. No provider credentials or external operation tools. Stop/freeze, rollback without personal fallback, safe restart (freeze unresolved work), authenticated artifact/export delivery, reader-policy change and stale-download tests are mandatory. |
| 2. MA attribution contract and sender-bound GitHub operations | 4-7 days, conditional on upstream evidence | Resolve the previously unseen delayed-event case under section 5.4; get the amended design accepted. Use the bounded staging experiment as falsification evidence, never as sole release evidence. Then App dispatcher, immutable payload approvals, live permission checks, replay/unknown-outcome handling, sender/provider audit and billing. Any ambiguous attribution blocks this slice, not the file pilot. |
| 3. Lifecycle and controlled connector expansion | 3-5 days | Reboot/orphan recovery, artifacts, repo removal, stop-sharing and rollback checked. Visible sandbox-expiry notice and export path; test expiration with fakes. Add only reviewed shared connectors; personal connectors remain outside shared mode. |

Recommended first slice is **1**, after design approval. Its safety claim
is limited: the system provisions no credentials or authenticated external tools
into the shared workspace. It does not claim teammates' imported files can never
contain a secret. The first release includes export/expiry warnings, stop/freeze, safe restart,
rollback without personal fallback, and authenticated artifact delivery.
Slice 3 may automate recovery further; the pilot freezes unresolved sessions
and reports the outcome instead of guessing how to resume.

Proposed later MA experiment: one disposable staging agent with workflows and
subagents disabled, one fresh credential-free session, one synthetic custom tool
executed as a no-op by the controller, and `claude-haiku-5-5` only. At most six
human-message turns test alternating senders, delayed custom result, interruption,
reconnect through event history, denial during stop, and a deliberately queued
message. Cap at **$2 total**, stop at $1.50 observed usage to leave room for in-flight
work, and stop sooner if the model or API shape is unavailable. Record experiment
IDs in restricted evidence and clean up the throwaway resources. This is a proposal;
no paid run has occurred and its budget is not approved by this document. A live
trace can falsify the boundary; passing examples do not create a vendor guarantee.

The TLA+ model remains required for implementation acceptance. Run it
with safe and deliberately broken configs, bounded resources, pinned state counts,
and action-to-code mapping. A local model must label MA assumptions as provisional;
it cannot substitute for the contract evidence above.

## 9. Review record

Claude Opus 5.5 reviewed the design and its MA evidence on 2026-10-10 in one
read-only consultation with high reasoning effort. It supported backend execution
and rejected the claim that local tests could establish MA causality. The revision
puts the credential-free pilot first, asserts its effective configuration, fixes
result schemas, requires backend-computed immutable approvals, and defines mode
precedence, rollback and expiry/export behavior. It also separates the later
contract gate from the pilot and names the App installation as the GitHub principal.
Its suggestion to make TLA+ optional was not adopted; implementation acceptance
requires a bounded model. Reads still need both caller authorization and permission
to distribute results to the shared audience.

An independent `gpt-6-astra` review on 2026-10-10, at medium reasoning effort,
found the direction and credential-free pilot conditionally ready. It reported
no critical issue and three high-priority corrections: include minimum lifecycle
controls in the pilot; distinguish team baseline authority from personal rights
in policy and audit; and require evidence beyond a few passing traces for delayed
unseen events. All three corrections are incorporated above. The design owner
checked the edits; a second independent review has not been performed. This
review does not approve enabling external operations.

The design incorporates these integration constraints:

1. GitHub access: one working repo, App-only setup, explicit baseline plus sender
   elevation, and requester-bound connection invitations. Shared access must not
   bypass current resource policy or personal isolation checks.
2. Approval behavior: one driver per shared session; retain queued messages during
   an active approval; requester-only Stop, Approve and Deny; one card per call.
   A confirmation counts as accepted only after the corresponding tool result is
   observed. Restart retires stale controls with a notice to the original requester
   before admitting new work. Use configured queue and approval durations.
3. CI: preserve existing suites and gates, including defaults verification and
   staging smoke checks. Add real-database concurrent creation, stale owner,
   revocation, adapter parity, recovery and billing checks. Extend formal-check
   path filters for new dispatcher files and retain unsafe witnesses with replay
   against actual code.

## 10. Decision requested

The cited MA findings come from current official documentation, checked on
2026-10-10. Application observations come from source inspection. No new live MA
experiment or model-checking run was performed for this design; runtime behavior
and upstream assumptions remain subject to the acceptance gates above.

Recommend **option 4**, starting with **slice 1: a fresh credential-free shared
workspace** (5-8 engineering days). Approve that direction and pilot scope first;
sender-bound external actions require the separate evidence and design gate above.
