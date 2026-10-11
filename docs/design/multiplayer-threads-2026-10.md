# Shared threads with agent connections

Revised proposal, 2026-10-11. Design and research only; this feature is not
implemented. The earlier credential-free pilot was rejected as too limited to be
useful. This proposal keeps credentials and MCP access. Its product choices have
not been accepted, and it authorizes no implementation, merge or deployment.

Status, 2026-10-11: build decisions are paused, not rejected, pending a broader
one-page credential and identity model covering GitHub, shared threads, browser
site logins and tenant-wide MCP keys. This proposal is input to that model. The
three choices below remain open until that model exists; recommendations and
estimates here do not authorize a build.

## 1. Recommendation and decisions

Use one fresh shared sandbox per opted-in thread, retaining reviewed agent-wide
connections. Everyone in that thread receives the same approved service access.
Keep each person's private sign-ins and extra permissions out of the shared
sandbox. Sharing must not turn the session creator into everyone's identity.

Recommend the following first release:

1. Shared agent MCP connections, approved for the thread's durable reader policy.
2. One working repo mounted with a GitHub App token limited to the approved team
   baseline; no permission elevation from whoever created or last used the session.
3. A new shared-session identity for Daimon's own MCP, limited to shared context
   and artifacts. Management requests use authenticated human controls.
4. A new exact-call authorization card for shared operations that require approval.
   An eligible member authorizes the displayed operation as themselves. This is
   a proposed change to requester-only approval semantics, not an existing feature.
5. Personal OAuth connections stay in personal sessions initially. An optional
   later proxy could execute a specific operation after its owner authorizes it.

Three independent product choices need agreement:

1. Common team authority and audience delegation. Recommend reviewed agent access
   shared by everyone in the approved reader class. The connection grant requires
   agent-manager authority and resource-owner distribution authority; an ordinary
   repo reader cannot grant that access.
2. Personal sign-ins in the first release. Recommend keeping them in personal
   conversations initially. No production-use inventory has established that
   personal connectors are unused; a day-one requirement changes this scope.
3. Shared-action authorization. Recommend allowing an eligible member to authorize
   the displayed exact operation as themselves, without claiming they wrote the
   message that caused it. Existing personal approvals remain requester-only.

The corresponding CI actor invariant also needs review and agreement: team service
actor, execution requester and authenticated action authorizer are distinct.
If requester-only confirmation must remain mandatory for every shared call,
approval-requiring shared connectors remain gated on the unresolved causal evidence
in section 5.4. The rejected empty sandbox is not a fallback delivery.

## 2. Current credential behavior

Source inspection checked against application commit `084510af0` distinguishes
external credentials from the human identity used for Daimon's own authorization.

| Surface | Existing behavior | Shared-mode consequence |
| --- | --- | --- |
| Agent MCP bearer token | Stored for the agent and mirrored into each caller's vault. | Reuse the reviewed agent credential in a fresh shared vault, subject to an explicit audience grant. |
| Personal MCP browser sign-in | Belongs to the connecting caller and agent; can occupy that caller's vault slot. | Never import the creator's personal grant. An agent-wide server list is not an agent-wide sign-in. |
| Google broker | Uses the Google identity and scopes bound to the agent. | Eligible for shared access only with the same resource/audience review; it does not switch to the message sender's Google identity. |
| GitHub App | Combines the agent baseline with the caller's linked GitHub permissions. | Use baseline only in shared ambient credentials. Later personal elevation stays in the backend. |
| Daimon's own MCP | Its signed identity identifies the session creator and tools check that person's authority. | A new nonhuman shared-session identity and explicit tool authorization are required. |

Separate per-caller sessions currently make that credential identity match the
requester. They do not implement sender switching inside a shared session.
[Agent-token sharing](https://github.com/pymc-labs/daimon/commit/b86f7443c) made
agent connections available to all callers; [personal-sign-in filtering](https://github.com/pymc-labs/daimon/commit/cee4b25ed)
kept unavailable personal connectors out of other callers' sessions. The
[Google broker documentation](https://github.com/pymc-labs/daimon/blob/084510af0/docs/architecture.md#google-tokens-in-ordinary-chat)
records its agent binding. These are code/history findings, not an inventory of
connections enabled in a deployment.

## 3. What current MA documentation establishes

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

## 4. Alternatives and costs

Estimates are rough engineering days for one implementer plus review, conditional
on the stated acceptance gates. They are not promises about calendar delivery.

| Option | Thread experience and authority | Migration and failure radius | Estimate and disposition |
| --- | --- | --- | --- |
| Repo handoff only | Separate sessions; share reviewed commits, not live files or history. Existing credentials and audit remain. | No session change. Wrong branches and accidental commits remain the main sharing risks. | 0.5-1 day for a tested guide; does not meet the shared-sandbox outcome. |
| Lend the creator's session | One existing workspace, all callers use the creator's connections and effective permissions. A sender label does not change that principal or token lifetime. | Exposes historic files and personal grants; failures can reach everything the creator can access. | 2-4 days for an unsafe prototype; reject. |
| Rotate credentials for each sender | One workspace, supposedly person-specific ambient tokens. Vault-ID swapping is unsupported and content rotation has no atomic sender boundary. | Old processes and delayed calls can retain another person's access until provider expiry. | No defensible safe estimate for literal rotation; reject that mechanism. |
| Backend operations for personal authority | Tokens stay server-side; a person independently authorizes an exact action. Shared context receives the approved result. | New operation journal, immutable payload and authorizer audit. A faulty proxy can reach stored credentials, so it needs a narrow operation/resource allowlist. | 4-7 days for bounded GitHub elevation after the shared base; arbitrary personal-MCP proxying needs its own estimate and design. |
| Shared agent connections plus human authorization (recommended) | Fresh shared workspace with common service credentials, baseline GitHub and scoped own-MCP tools. Human confirmation authorizes exact calls where policy requires it. | New shared principal/mapping; personal sessions remain isolated. Failure radius is the explicitly approved service scope and shared data, with human rights confined to later backend operations. | 13-20 days for the connected first release, including approval and lifecycle work. |

Credential lifetimes follow each provider. Stable agent MCP tokens may be
long-lived; Google and GitHub broker tokens use the provider-returned expiry.
Shared mode does not claim that every credential becomes short-lived. Rotation
and revocation stop future access where enforceable; they cannot erase results
already exposed in the shared history.

## 5. Proposed connected shared mode

### 5.1 Audience, connections and fresh start

A human authorized to manage the agent may request sharing in plain language.
Resolve the audience and resource grant first, then create a new shared mapping,
sandbox and vault. Never widen the lookup of an existing personal session or copy
its history, home directory, `.env`, personal grants or memory. Import only
reviewed files/resources intended for this audience. A clear authorized request
is sufficient; do not add a second generic confirmation for enabling the mode.

The audience includes all thread readers, including people who never send a
message. Start with verified private Discord threads and private Slack channels
without external/Slack Connect membership. Record the reader policy, resource
owner's distribution grant, connection scopes, allowed operations and policy
version. Today's intersection of reader permissions is insufficient: a platform
invite can reveal earlier chat before the bot notices. Approve the durable
reader policy, including who may invite future readers, the future-reader class
approved by the resource owner, and which platform controls enforce that boundary.
Reject an audience where the required restriction cannot be enforced. Verify the
policy on admission/dispatch/download and freeze new work or delivery when it
expands outside the grant or becomes unknown. Bot checks protect subsequent
work and delivery; they cannot prevent or undo historical disclosure by a platform
invitation that has already taken effect.

Construct the vault from reviewed agent credentials, not the creator's caller
vault. Personal OAuth grants and personal privilege elevation are absent.
Review each environment secret and memory source separately; neither is inherited
wholesale. A shell-visible credential must not permit an operation that the shared
policy requires a human to authorize: otherwise shell/network access bypasses the
MCP confirmation. Keep such credentials behind the connector or backend, with
provider-enforced scope and no raw export. GitHub ambient access is read-only in
the first release. Keep workflows, subagents, routines and automatic continuations
off for the first release. For each connector, review direct MCP, shell/network routes,
credential exchange, environment credentials and surviving background processes.
Name the restrictions enforced by the provider versus MA or Daimon. The first
release admits only paths with demonstrated operation/confirmation enforcement;
broader ambient authority would require a separate explicit scope decision, not
an inference from the token being hidden. Root repo skills and imported code
remain untrusted input.

At least one useful authenticated MCP connection and the approved working repo
must work in the acceptance demonstration. Preserve deployment and agent tool
confirmation policies. A connector that requires confirmation is eligible only
if its exact pending call can be displayed and authorized under section 5.4;
otherwise it is unavailable, never silently changed to automatic execution.

The shared-mode notice states the working repo, connection identities and scope:
"This thread uses the agent's shared connections. Everyone who can read it can
see the work and results. Your personal sign-ins are not connected here."
Access details distinguish team service authority from any later personal action.

### 5.2 Queue, ownership and the shared MCP identity

Use a durable FIFO queue keyed on `(tenant, session)`, one executing owner and
one driver stream. Persist the platform sender, message ID, session generation,
policy version and execution epoch, a number distinguishing each execution from
its replacement. Deduplicate platform deliveries. Batch only contiguous messages
from the same author and keep current admission limits. Bob's message remains in
Daimon's queue throughout Alice's active turn or approval pause; do not send it
to MA as steering or use a confirmation-related 400 to interrupt Alice.

Daimon's own MCP gets a proposed new nonhuman shared-session principal scoped to
that tenant, session, agent and approved place. This is not the existing restricted
`agent_id`/agent-chat token type. It has no creator role or account fallback.
Every exposed tool must support that principal explicitly; default denial and a
tag alone are not a resource-authorization implementation.

The first allowlist covers shared place/context, reads/searches within the approved
place and resources, and creation/access of this workspace's artifacts. Artifact
ownership is shared and audience-checked; do not reuse a creator's private notebook
namespace or expose a public link where authenticated delivery is required.
Preserve publish policy through section 5.4. Unreviewed tools, tenant-wide session
listing, admin/setup changes, connection management, routines, timers and raw
`get_cli_token` are unavailable to this identity. Human platform controls handle
management and recheck the actual human's rights.

A reviewed agent-bound Google connection may run through a narrowly scoped shared
backend operation. Do not expose the existing generic token-export tool to obtain
it. Direct agent MCP connections use their approved common service credential.

### 5.3 GitHub and personal access

Mount at most one working repo using a GitHub App installation token restricted
to the explicit shared baseline. Do not add the creator's or current sender's
linked permissions to ambient access. Recommend read/clone plus local edits for
the first release; intersect the shared baseline with that read-only profile even
if the agent's ordinary baseline permits writes. Backend writes follow in the
later slice. Other repo
grants do not become additional mounts or unreviewed ambient token exports.
Legacy personal PAT sessions continue unchanged; shared mode requires App access.

The ordinary permission rule remains
`max(baseline, min(agent ceiling, sender permission))`. The baseline may exceed a
person's rights and the agent ceiling; the ceiling only caps sender elevation.
Using a baseline therefore requires explicit authority for both the audience and
the operation. A repo being attached to the agent does not itself grant that.

Later GitHub elevation uses a backend operation with immutable repo, branch and
payload/diff hash. An eligible person authorizes that exact action through the
platform. Recheck their live rights, the resource distribution grant and the
operation allowlist, then mint a narrow installation token server-side and execute
the approved bytes. GitHub records the App. Daimon separately records the actual
human authorizer, effective scope, authority source and policy version. Never
return elevated tokens to the sandbox or select a person from tool arguments.

Personal MCP sign-ins remain outside the first shared release. This is a proposed
scope choice, not a finding that they are unused or impossible. A later proxy
could obtain the owner's independent authorization for a specific operation;
that would require connector-specific schemas and output/audience restrictions.
Do not replace a private OAuth grant with the agent's token inside existing
personal sessions, or copy either person's existing vault into the shared one.

### 5.4 Exact-call authorization and the attribution limit

MA's reviewed contract does not identify the human message that caused every
MCP/custom-tool call. In particular, a previously unseen old call discovered after
a new epoch begins must not acquire the new sender's personal authority.
Serializing turns or passing a few traces does not establish that causal link.

The proposed alternative is an independently authenticated human action. For a
shared operation requiring confirmation, show the exact tool, target, operation
and immutable arguments/payload. Store the authentic upstream pending-call ID or
backend operation ID and bind the platform card to it. The card says
"Authorize this action as yourself", identifies the service principal, and does
not claim that an unverified earlier speaker requested the call.

Only an eligible current thread member with the required live authority may
activate it. For a common service action this means membership plus the explicitly
approved team operation grant. Personal elevation also requires that person's
own rights. The platform click creates the human authorization; no mutable
`current_account` or session creator supplies it. A changed payload, expired card,
revoked grant or changed audience invalidates it. Duplicate clicks do not execute
again. Keep one card per call and existing expiry/observed-result rules.

This **changes approval-ux constraint 2 for the new shared-action card**. It does
not let Bob approve an operation using Alice's personal credential. It allows Bob
to authorize the displayed team action, or a later action using Bob's own rights.
Existing personal-mode approvals remain requester-only. Shared chat Stop and
status cards still belong to the recorded chat requester; the action authorizer
is a distinct field. Product approval of this amended interaction is required.

For direct third-party MCPs, require verified availability of the exact pending
call and faithful forwarding of its confirmation. If the call is opaque, changes
between display and execution, cannot be bound to its card, or its result cannot
be reconciled, that connector does not pass acceptance. Do not turn its approval
policy off. Human-authorized backend operations use fixed schemas and sanitize
results/errors; no generic shell/HTTP proxy obtains personal authority.

Audit team credential use under the shared service principal. Record the known
chat requester as execution context, not as a proven causal author of every call.
Record `authorized_by` only from authenticated human action. Calls needing proven
original-requester identity remain unavailable until there is upstream evidence
or another accepted design. The model must not assume that identity into existence.

### 5.5 Recovery, migration and lifecycle

Use a separate shared mapping and policy history; retain personal account/NULL
isolation and tenant/thread checks. Active shared mode takes precedence for new
messages in that thread. Rollback freezes it rather than reopening an earlier
personal session. A later personal start uses a fresh private conversation.

Stop closes new dispatch admission, resolves pending calls and reconciles upstream
interruption before admitting another sender. Only the chat requester may use its
ordinary Stop; an authorized manager can freeze sharing without borrowing another
person's approval. `requires_action`, silence or disconnect does not release the
owner. A stale worker cannot clear its successor or rebind old work.

After a crash, reconcile session history, owner/generation and every pending call.
A proven-orphan interruption cancels every pending confirmation in the session:
retire the affected cards and notify the original chat requester and any recorded
authorizer. Unknown ownership stays frozen. Keep current drain settings; accepted
queue items survive restart or receive an explicit terminal outcome.

Persist external effects as prepared/dispatched/succeeded/failed/unknown. Use
provider idempotency where supported and reconcile unknown outcomes before retry.
Revocation is defined per operation, not per chat turn. Chat admission does not
authorize later calls. Separate chat admission, exact-call authorization, external
dispatch and provider credential validity in the journal/model. Backend calls
recheck the durable grant/generation before dispatch; calls not yet dispatched
are denied after revocation. A specific already-dispatched request may finish.
A confirmation already delivered upstream may also race revocation; reconcile
that exact pending call rather than treating the whole turn as allowed to finish.

Direct MCP and shell calls may bypass the backend's dispatch boundary. Revoking
queue admission alone cannot stop them. The per-connector record must specify how
new service calls stop: confirmed provider credential revocation or an enforced
proxy/gate, plus interruption and retirement of the shared session. Where only
expiry bounds access, record the provider-backed exposure interval and obtain
explicit acceptance; an unbounded reusable token is not an acceptable implicit
limit. Reusing one agent token can prevent revoking just this session without
impacting other users; use a dedicated revocable credential or controlled proxy
when that isolation is required. Do not silently rotate every personal caller's
token. Preserve/export permitted work and create a fresh replacement only after
reconciliation. Acceptance tests include late calls and surviving processes, not
just new chat messages. Preserve accurate audit rather than claiming revocation
undid a completed effect.

Grant changes re-evaluate live permissions and policy version. They cannot silently
replace the session and erase shared work. Freeze affected operations and preserve
or export work before explicit replacement. Never rely solely on a fingerprint
that omits grant changes. Token expiry uses provider-returned information.

The first release includes authenticated artifact delivery, stale-download tests,
reader-policy changes, expiry warnings/export, stop-sharing and rollback. Charge
the tenant under existing policy and retain the actual execution requester; a
creator's billing exemption must not cover all teammates. Already viewed history
and downloaded files cannot be retracted.

## 6. Acceptance and implementation sequence

Before committing to an implementation estimate, pass a connector feasibility
gate. Select one real agent connection and a useful read operation; record its
non-secret server/tool identity, request/result schema, permitted resource and
reader class, grant owner, credential scope and all access paths. Inspect the
actual pending-call/confirmation/result contract and the authenticated artifact
delivery path. A fixture alone does not establish real connector compatibility.

The demonstration plan is: Alice requests that operation against the approved
resource; Bob continues with the returned data in the same workspace and mounted
repo; the required shared-call confirmation authorizes exactly the recorded call;
Daimon's own MCP creates an artifact available only to the approved audience.
Repeat with a denied member, changed payload, revoked grant and late/background
call. One concrete connector has not yet been selected or validated; shipped
agent defaults alone do not identify a deployed personal/team connection.

This gate uses read-only metadata/schema research first. Any live staging calls
require the separately stated authority and budget. If a new connector proxy or
artifact gateway is required, estimate that work explicitly and revise the range
before committing to a build. Failure returns a scoped alternative for decision,
not the rejected credential-free substitute.

| Slice | Estimate after feasibility gate | Acceptance |
| --- | --- | --- |
| Connected shared workspace and shared-call authorization | 13-20 engineering days | Fresh Discord/Slack shared sandbox, reviewed authenticated MCP read and baseline working repo, new own-MCP principal and authorized artifacts, exact-call confirmation preserving existing tool gates, all queue/lifecycle controls above, model and tests below. Requires the product decisions in section 1. |
| Personal GitHub elevation through backend actions | 4-7 additional days | Immutable payload, independently authenticated authorizer, live rights, App-principal audit, revocation/idempotency/unknown-outcome checks and staging contract evidence when authorized. No elevated token enters the shared sandbox. |
| Personal MCP proxy expansion | Separately estimate per connector | Owner consent, exact operation/result schema and audience policy. No promise that arbitrary personal MCPs work unchanged. |

Demonstrate these examples before calling the first slice useful or safe:

1. Alice starts an approved shared thread; Bob uses the same files and an approved
   authenticated agent MCP connection. The sandbox also reads the mounted working
   repo with the same baseline for both people.
2. If that MCP requires confirmation, its exact call gets a shared-action card.
   An eligible member can authorize it under the amended policy; an outsider
   cannot. A different or opaque call never executes on that authorization.
3. The creator is an administrator and Bob is not. Bob gains no creator admin
   tools, personal OAuth, GitHub elevation or private session/artifact access.
4. Bob messages during Alice's pending approval. His message queues without an
   MA send or interrupt. Duplicate deliveries/clicks do not duplicate work/effects.
5. A late call carries only approved team authority, never the next sender's
   personal rights. Its human causal origin is not fabricated in audit.
6. Restart retires orphaned controls with notices. Unknown ownership or write
   outcome freezes; revoke/change-reader/stop tests deny new dispatch and downloads.
7. Rollback never falls through to old personal rows. Private account and NULL-row
   tests, tenant boundaries, billing and existing Slack/Teams behavior still pass.

Before implementation, build a bounded TLA+ model under `formal/` for two callers,
two workers, private/shared modes, generations, delayed events and revocations.
Follow `discord_card_lifecycle`, including action-to-code/await mapping, safe and
unsafe witnesses, pinned `formal/expected.tsv` verdicts/counts and actual-code
replay. The revised actor invariant separates chat requester, shared service actor
and independently authenticated action authorizer. This refinement needs agreement
with CI; it must not assert that every delayed call has a known human origin.

Preserve the named suites and gates in section 9. No TLA+ run or live MA contract
test is claimed by this document. Proposed staging validation remains bounded,
credential-controlled and Haiku-only, with a separately authorized budget; the
previous proposed $2/six-turn experiment has not been run. It can falsify assumed
behavior, not create an upstream causal guarantee.

## 7. Limits and open choices

This is a workspace for people allowed to share the same data and service scope.
It does not isolate them from each other's code, files or prompt injection. A team
credential can permit writes outside the sandbox; its reviewed scope, operation
policy and confirmation gates are the boundary. Personal rights never become
ambient merely because a person sends a message or clicks one action.

The exact-call design is a proposal requiring implementation and connector contract
checks. Some MCPs may not expose enough confirmation detail to qualify. If the
required connectors fail those checks, revise the scope with concrete evidence;
do not deliver an empty workspace or promise compatibility that was not tested.

Shared team delegation and the new authorization card are consequential product
choices. Personal OAuth exclusion also needs agreement. No requester-only rule,
CI invariant, or existing deployment approval policy is silently relaxed here.

## 8. Review and revision record

The [earlier proposal](https://github.com/pymc-labs/daimon/blob/4ba1d4974/docs/design/multiplayer-threads-2026-10.md)
was reviewed by Claude Opus 5.5 and independently by Astra on 2026-10-10. It
proposed a credential-free first slice; product feedback rejected that slice as
not useful. Those reviews are historical and do not approve this revision.

A read-only Opus 5.5 high consultation on 2026-10-11 recommended common agent
credentials, a new nonhuman own-MCP identity and independent human authorization
for exact actions. It explicitly identified the requester-only approval conflict.
The revision incorporates that conflict as a product decision and preserves
existing third-party confirmation gates, including on reads. It does not adopt
an unrestricted token-export tool, automatic baseline writes, or a claim that
personal OAuth is impossible without MA causal attribution; an owner-authorized
proxy remains a later option. A bounded drafting worker supplied the connected
slice and acceptance examples; the design lead integrated them.


An independent Astra medium check on 2026-10-11 found the revision suitable for a
conditional product decision, with no P0 established. It identified three P1 gaps:
credential paths could bypass confirmation, revocation was ambiguous between a
turn and an operation, and no qualifying real connector had been selected. The
revision adds explicit path-enforcement review, operation-level revocation with
credential-validity limits and background-call tests, and a preimplementation
connector feasibility gate. Two P2 clarifications specify future-reader admission
controls and separate the three product decisions. The design lead checked these
edits; no second independent review is claimed. The proposal is not build-ready
until its product choices and technical gates are settled.

## 9. Explicit integration-constraint dispositions

All three peer lists were reread in full after the window restart. Each numbered
item below corresponds to the same numbered item in its source list. The revised
approval rule and actor-model refinement are proposed amendments; receipt of peer
advice does not authorize changing their behavior or implementation.

### 9.1 GitHub access constraints

1. **Working repo and connection flow.** Keep repo grants scoped to the agent,
   at most one working repo, and none by default. The proposed shared session
   mounts that repo
   only after the audience check in section 5.1, using baseline-only App access;
   agent ownership alone does not authorize redistribution to every reader.
   Other repo grants do not become shared mounts or token exports. The ordinary
   product's token-only access paths and credential-group limits remain unchanged.
   Shared mode uses the App Connect flow without requesting or suggesting PATs.
   Human management requests retain the clear-ask and undo behavior; model tools
   in the shared session cannot change grants. Legacy personal sessions continue
   under their existing rules.
2. **Caller identity and token strength.** Treat a session credential's identity
   as its owner, never as the current requester. Shared execution must not inherit
   the creator's permission elevation. Section 5.3 preserves the permission
   formula for personal mode. Shared ambient access uses an explicit baseline
   grant without sender elevation. Later elevation uses the independently
   authenticated authorizer's live rights in the backend. This adopts the peer's
   baseline-only suggestion without adopting a mutable sender lookup.
   A broker lookup of the currently running sender is insufficient for an unseen
   delayed call. Section 5.4 proposes an independent exact-action authorization
   path; it requires acceptance before enabling personal authority. Original-requester
   attribution remains unproven. Audit records distinguish requester, provider
   principal, effective scope and source of authority. Provider-returned expiry
   governs backend tokens; a local refresh interval is not a vendor guarantee.
3. **Management, refresh and invitations.** Shared management uses the verified
   human requester and cannot fall back to the session creator's running turn.
   Connect cards and private invitations stay bound to their requester. Grant
   changes must not silently replace a shared session and discard teammates'
   files. Freeze affected operations, re-evaluate live grants and policy version,
   and preserve/export work before any explicit replacement. No shared-mode
   decision may depend solely on an existing session fingerprint omitting grant
   changes. An in-place credential refresh remains a separate capability to prove;
   unsupported vault replacement is not a solution. Existing read-only repo
   panels and the no-PAT-hint behavior remain unchanged.

### 9.2 CI constraints

1. **Model and evidence.** Place the bounded model in `formal/` and follow
   `discord_card_lifecycle`: action-to-code/await mapping, safe and deliberately
   unsafe configurations, counterexample witnesses, pinned verdicts and state
   counts in `formal/expected.tsv`, then replay against actual code. Cover two
   callers, private/shared modes, simultaneous first creation, queueing,
   cancellation/retry, approval waits, restart/replacement and membership or
   credential revocation. Require one executing owner, no stale owner clearing
   or overwriting its successor, and immutable actor/principal/session-generation/
   policy binding for each credential use and approval. For the revised proposal,
   team calls act as the shared service principal, and human authority comes from
   a verified action authorizer. The known chat requester is separate context.
   This refinement requires CI agreement; it cannot claim a proven human cause
   for every delayed call. Preserve private account, tenant and thread isolation, fresh opt-in without private history import,
   explicit shared-data visibility, and correct audit/billing without duplicate
   charges. State fairness and upstream assumptions for progress. Before code
   exists, model results are provisional; actual replay and MA contract evidence
   are required for implementation acceptance.
2. **Suites and scenarios.** Extend core tests in
   `stores/test_thread_sessions.py`, `turn/test_session_identity.py`,
   `test_sessions_isolated.py`, `test_session_preparation.py`,
   `test_session_fence_retry.py`, and the snapshot/refresh and broker/credential
   tests. Retain account/NULL isolation; add real-database concurrent first join,
   legacy-row migration, stale-owner, revocation and actor-versus-billing checks.
   Extend parity cases `test_thread_queue_equivalence.py`, `test_dead_session.py`,
   `test_session_refresh.py`, `test_turn_billed.py` and relevant continuation,
   participation and card cases. A two-user Discord scenario must cover opt-in,
   shared files, policy-authorized actions when enabled, outsider refusal,
   cancellation/restart and private-thread regression. Keep existing Slack/Teams
   private behavior; no Teams feature is proposed. Preserve every core/MCP shard,
   Discord/Slack/Teams/parity suite and lint/build gate. Add new binding/broker
   paths to both push and PR formal-workflow filters.
3. **Defaults and deployment gates.** Preserve strict defaults verification and
   only its existing narrowly scoped staging exception. Do not reseed test tenants
   or alter shipped defaults to make a check pass. Changes to defaults or settings
   require matching tests, documentation and generated configuration. Preserve
   migration-before-refresh, usage evidence, the existing Haiku staging smoke and
   promotion gates. A single-caller smoke does not prove multiplayer isolation:
   use fake coverage first, then a separate bounded two-caller contract scenario
   with controlled credentials if authorized. These constraints confer no merge,
   staging or production deployment authority on this design effort.

### 9.3 Approval and recovery constraints

1. **Ownership and queue.** Key shared `ThreadQueue` processing and pending work
   on `(tenant, session)`, with a durable owner and generation, not on the caller.
   Exactly one driver stream runs per session. Keep FIFO order and the existing
   configured wait/cap limits; queued teammates count toward those limits and
   receive the ordinary timeout/refusal outcome. A teammate's message stays in
   Daimon's queue throughout an approval pause. It must not be sent to MA: the
   driver's confirmation-related 400 recovery sends `user.interrupt`, which would
   cancel the active requester's approval. Only a proven orphan may enter that
   recovery path. Mentions, Stop and summaries address the turn's requester; Stop
   refuses other callers. Process-local queue flags do not replace durable
   ownership across workers or restarts.
2. **Approval and origin: explicit amendment proposed.** Preserve requester-only
   Approve/Deny, `Requested by @A`, the refusal footer and Slack behavior for
   existing personal-mode approvals. The proposed shared-action card in section
   5.4 instead lets an eligible member authorize an exact operation under their
   own identity. It cannot execute using a different person's rights. This is a
   conflict with the peer's requester-only rule, requiring product agreement;
   without that agreement, approval-requiring shared tools remain blocked on
   causal attribution. Keep one card/call, configured expiry denial and observed
   tool result before acceptance. Preserve ask-before-publishing and
   `tool_safety.PUBLISH_TOOLS`, including the deployment's external-tool gates.
   Do not forge a human turn-origin row for the shared service principal. Personal
   rows remain per turn and sender; a human-authorized shared operation records
   its separate platform authorization and does not borrow the creator's origin.
3. **Restart and orphan recovery.** Keep personal row lookup and replacement
   notices scoped to the personal caller. Shared recovery instead reconciles the
   session's durable owner, generation and all affected turn-card intents. A
   restart may leave approval cards with dead controls; retire them with a plain
   notice to each original chat requester and recorded action authorizer before
   admitting new work. Interrupting a proven orphan affects every pending confirmation in that MA session, so record
   an outcome for each call and notify each requester. An uncertain owner or
   incomplete reconciliation keeps the session frozen. Retain existing drain and
   deployment timeout settings. Dropped mentions during drain are an existing
   follow-up, not a promised fix in this design; accepted shared queue items must
   survive restart or receive an explicit terminal outcome. A teammate's first
   message must never trigger silent cancellation as a side effect of admission.

## 10. Recommendation and first slice

Recommend one fresh shared sandbox with reviewed agent connections, baseline
GitHub access, a scoped own-MCP identity and independently authorized exact calls
where confirmation is required. The first connected slice is estimated at
13-20 engineering days only after the connector feasibility gate, conditional
on agreeing the three choices in section 1. Required new proxy/gateway work must
be estimated before that range can be treated as a build plan.

This replaces the rejected credential-free pilot. The connected proposal remains
open, with build decisions paused pending the broader identity model. No build,
merge, deployment or live experiment is authorized.
