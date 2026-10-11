# Agent connections and work audiences

Discussion draft, 2026-10-11. This proposes an identity model; it does not authorize implementation. Shared threads remain a separate, unaccepted proposal.

## The proposed model, in questions

**Can a team agent carry the team's access, whoever asks?** Proposed answer: yes. Its shared connections serve everyone the agent is approved to serve, including readers of its results. Personal sign-ins stay in work only their owner can see. Start with the agent's approved channels as that audience; give narrower work a separately restricted agent. Widening where an agent works must review its connections too.

**Who is doing the work?** The agent uses the selected connection account. Record that account, the human requester and any authenticated action approver separately. A delayed call must not acquire the latest speaker's identity; unknown attribution stays unknown. Whether another member may approve a shared action remains open.

**Who can grant it?** A person grants their personal connection. An authorized manager records a shared connection's resources, operations and approved readers, including future members. Connecting an account and distributing its data require authority for both. Verify provider authority where available. A tenant is the administrative and billing boundary; its ownership of a key does not grant every team access.

**Where does a thread fit?** A thread is where work happens and results appear. Its audience includes people invited later who can read earlier history. A per-person session in a channel does not make posted answers private. A fresh shared workspace receives approved shared connections, files and memory; it never inherits the creator's personal session. Changing connections cannot make earlier files or history private.

**What do I see?** Setup names the selected connection, signed-in account and readers: "Team Docs", "Signed in as docs-bot", "Visible to Project team". Save the choice without silently falling back to another login. Existing confirmations remain; approval cannot expand the connection's access.

**Does a browser need another rule?** Its vendor API key opens browser capacity; website cookies and profiles carry the website identity. A personal login in a shared browser delegates its usable access. Calling it shared cannot authorize that. Isolate both the profile and the authority to open it.

**What enforces the choice?** Scope every available path: MCP, shell, browser state and background work. A label cannot constrain broader credentials. Fixed shared GitHub access names repositories and operations; refresh must preserve that scope without restoring caller uplift.

**What changes first?** Existing agent keys and GitHub baseline access already serve multiple callers. Make that sharing visible and intentional. Add no automatic sharing during migration; existing per-caller behavior remains until reviewed. The proposed private-only default for personal connections changes today's channel use and needs agreement. Unknown secrets stay out of new shared work.

**Where do credentials live?** Daimon keeps connection ownership, grants and secret references. Protected stores or provider vaults hold tokens; the browser provider holds scoped profiles/cookies. The sandbox receives only authority approved for its audience. Daimon's own token carries its creator's authority for the session; shared work needs a scoped service identity without those rights.

**What does removing access do?** Proposed promise: stop new dispatch when removal takes effect. Already-dispatched work may finish; issued credentials may remain usable until confirmed revocation or expiry. Show that remaining access and its bound; unknown or unbounded access needs resolution before sharing. Removal preserves the workspace and cannot erase disclosed results.

## How do people join, and which features need personal access?

People need not be registered as a fixed participant list when a session starts. Proposed shared work follows an approved channel/thread access rule and checks the actual person when they interact. Someone arriving tomorrow continues the same shared files; someone stopping participation does not destroy them. A public Discord thread is visible to everyone who can view its parent channel, including people who have never posted. Thread subscription is not a confidentiality boundary. A change to channel access can expose history already there; approval must cover the people administrators are allowed to admit. [Discord thread permissions and membership](https://docs.discord.com/developers/topics/threads), checked 2026-10-11.

For example, an agent approved for a project channel can use that project's repo and shared data connection. Alice starts an analysis; Bob joins later and asks for a chart change. In the proposed shared mode Bob uses the same workspace and approved agent access, without loading his own service accounts. This does not authorize broadening the channel beyond the resources' approved audience. The multiplayer proposal's private-thread-first restriction is still a proposed rollout limit, not a need for a fixed roster; ordinary public-channel threads must be evaluated against their full channel audience.

Workspace sharing and credential ownership are independent. Existing agent-wide keys already serve separate per-caller sessions. "Team connection" means access intentionally shared through an agent; it need not be another account, team object or setup flow. Importing reviewed files transfers data, not another user's browser cookies, credentials or authority; copying is not continuous synchronization.

The following are supported code paths, not a deployment usage inventory:

| Existing workflow | Why a person's connection matters | Shared-work consequence |
| --- | --- | --- |
| Sign into an external MCP service | OAuth authorizes that person's service account; the grant is stored for that caller and agent. Other callers without a grant have the personal connector hidden unless an agent-wide credential also exists. | Shared work cannot inherit the starter's personal login. An agent service connection or separately authorized personal operation would be needed. |
| Connect Slack for reads/search | Optional user access reaches channels the person can read even when the bot was not invited, and supports search. One-to-one DM results have a private-destination guard. | Simply switching to shared bot access would reduce this feature's reach. Person-sensitive reads need their own authority and output-audience handling. |
| Link GitHub for extra repo permissions | The agent's baseline combines with the caller's GitHub permissions, up to the configured uplift ceiling. GitHub still receives an App token. | Fixed shared access deliberately excludes that extra permission. Personal-write workflows need an explicit replacement before retiring uplift. |
| Legacy account-bound GitHub PAT | Ordinary non-App chat can retrieve the caller's older account credential through the CLI-token tool. | Preserve existing use during migration; do not copy the token into shared work. App mode refuses this CLI-token path. |

Sources: [personal MCP sign-in](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/mcp_oauth/complete.py#L103), [per-caller connector filtering](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/mcp_personal_servers.py), [Slack read-client selection](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/adapters/mcp/daimon/adapters/mcp/tools/slack/_client.py#L79), [GitHub access formula](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/github_requester_access.py#L43), [CLI-token behavior](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/adapters/mcp/daimon/adapters/mcp/tools/cli_token.py#L40).

Agent environment keys, agent MCP bearer connections and GitHub baseline access already fit the shared-agent pattern. The [Google broker](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/broker/providers/gcloud.py) uses the Google identity bound to the agent, not whichever person asks. These paths do not demonstrate demand for switching personal accounts on every message.

Human permissions remain necessary even with agent credentials: who may edit setup, approve an action, stop a turn, read a channel or own a routine. Personal external credentials and those human authorization checks are different concerns. [Routines](../routines.md) remain separate runs with creator authorization and agent resources; App-mode routines use baseline GitHub access. Adding routines, wakes, agent handoff or personal Slack reads to a shared session needs explicit compatibility work. Shared files do not resolve those authority questions, and the current multiplayer proposal excludes some of those features from its first slice.

No evidence here establishes how often these personal paths are used. The next product decision should use non-secret adoption metadata and concrete workflows before removing supported features. Personal-only-in-private remains a proposed policy change, not current behavior or an accepted requirement.

## What is isolated at each scope?

Current source at `77ddd92f2`, for ordinary Discord/Slack chat. "Separate session" describes execution and sandbox files; it does not promise private results or private agent memory. The proposed shared mode would deliberately share a fresh session only after agreement.

| Scope | Sessions and files today | Agents, memory and credentials today | Billing today |
| --- | --- | --- | --- |
| Tenant | Session lookup and stored records include the tenant. Tenant isolation is enforced by Daimon; provider infrastructure is shared. | Tenant-scoped agents, memory bindings and credential records. A tenant-wide provider key can still reach more than one team's permitted resources. | Tenant balance/ledger; provider bill belongs to the deployment operator. |
| Channel | A channel supplies routing and read/write rules, not its own universal sandbox. Threads have caller-specific sessions. Posted results follow channel/thread readership. | Agents can serve several channels. Channel restrictions must be configured; changing channels alone does not separate the same agent's memory or keys. | Optional channel budget; thread usage follows its attributed parent channel. |
| User | Each account gets its own session in the same thread. Unpublished sandbox files should stay with that session; posted attachments are visible to thread readers. | Personal OAuth and caller authority differ by account. Agent memory, agent keys and GitHub baseline access can be shared. | Usage attributed to requester; optional personal cap. No separate personal balance is implied. |
| Thread | Lookup includes thread and account. Different threads have separate sessions; people in one thread still have separate sessions today. Messages and posted files are shared with its readers. | Same agent can share memory/connections across threads. A new thread does not reset those. | Usage records retain session/requester attribution; no independent thread balance. |

Agent memory is one store per `(tenant, agent)`, not per user/thread. Read-only mounting prevents new writes; it does not remove earlier contents. Channel rules constrain reads, posting and where agents run, with documented admin exceptions; they do not narrow arbitrary external credentials. Proposed private work therefore also needs memory approved for that private audience, not just a personal session.

Checked against [session lookup](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/stores/thread_sessions.py), [memory binding](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/memory_resource.py), the credential paths below, [channel and agent rules](../permissions.md), and [billing](../billing.md). This is a source-backed description, not an end-to-end isolation audit.

## Four cases that test the rule

1. **GitHub today.** Repositories are granted to an agent; at most one is its working repo, none by default. Eligible callers already receive the agent baseline, plus their linked GitHub permission capped by the agent ceiling for that uplift. App tokens use a per-session vault; Daimon's own MCP token names the creator. Lending that session to a teammate could lend the creator's effective access. Proposed shared work makes the baseline's audience explicit and excludes personal uplift; GitHub still sees the App, not the human requester. Later personal elevation would require a separately authorized backend action. Ordinary sessions keep current rules during migration. [Access formula](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/github_requester_access.py#L43), [session vault](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/github_app_session.py#L191).

2. **Shared thread proposal.** Alice and Bob use one fresh workspace with a declared Project team audience. The agent uses a reviewed team MCP connection and a GitHub baseline approved for that audience. Its own Daimon MCP access needs a scoped shared-service identity; Alice's account must not silently become that identity. Alice and Bob can see shared files and results. Neither gets the other's personal OAuth or extra GitHub rights. A delayed call still uses the approved service authority, never whichever human spoke most recently. This is proposed behavior; shared sessions are not implemented.

3. **Browser at a third-party site.** The Browser Use API key provisions browser capacity. The website sees a logged-out visitor, a person's login, or a team service login. Recommend a team-managed site account when the site supports one; its custodian handles sign-in, MFA, recovery and revocation through a private setup flow. Reuse it across authorized agents with the same scope. Choose a separate account per agent only when distinct permissions or provider audit require it. A site requiring an individual login uses a personal connection in private work. Never promote that login to shared because the site lacks service accounts. Both cookies and vendor access to profiles must be isolated; verify that a shared key cannot list or reopen personal profiles. This boundary is unverified. No account, browser session or spend is needed to discuss it.

4. **External MCP with a tenant-wide key.** Today an agent's stored MCP key is already mirrored into callers' vaults. The proposal records and checks the audience for that existing sharing. A key covering the organization may be too broad for one team. Approve its actual reach or enforce narrower access through the provider or a connector gate; a label or hidden token does not narrow it. A personal OAuth sign-in to the same server remains a distinct personal connection. [Current caller vault code](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/mcp_vault.py#L81).

If Browser Use cannot isolate personal profiles under shared vendor access, choose isolated provider access or a proven backend gate before supporting them. Otherwise defer personal browser logins. Those alternatives need verification; no separate account or billing requirement is assumed.

## Options and the cost of changing course

| Option | User rule | Judgment |
| --- | --- | --- |
| One account for each agent at each service | "The agent owns its logins." | Simple label, but ownership, recovery and audience still need separate rules; many unnecessary provider accounts. |
| Use the current speaker's accounts everywhere | "The agent becomes whoever asked last." | Attractive for private work, but a shared persistent sandbox cannot safely switch ambient credentials around delayed calls and surviving processes. |
| Named personal or shared connections, with declared work audiences | "The agent uses this connection for these readers." | Recommended. It matches service accounts and personal sign-ins without pretending a thread or tenant is a provider identity. Requires explicit grants and migration. |

Five implementation paths become one connection model plus Daimon's internal execution identity. This does not require one universal vault.

| Current path | What survives or collapses | Migration cost |
| --- | --- | --- |
| Per-agent `.env` secrets | Secret delivery survives; service keys become connection records after owner/scope review. Noncredential settings stay settings. | Medium: inventory, classify, bind grants; unknown secrets cannot enter shared work. |
| Managed Agents vaults and GitHub App tokens | Vaults remain storage. App tokens derive from the selected connection; shared access excludes caller uplift. | Medium for records; high for changed authorization and session isolation. Current App vaults are per session, alongside ordinary `(account, agent)` vaults. |
| Agent MCP credentials mirrored per caller | Collapse into the same connection records. Mirroring remains an implementation detail. Personal OAuth is a separate personal connection. | Medium: classify owners/audiences and remove implicit same-URL login precedence. Broad keys may require a proxy. |
| Daimon's own MCP JWT | Survives as internal execution identity. Shared mode needs a scoped nonhuman identity without creator authority. | High: authorization, attribution and tool compatibility must be proven. |
| Legacy PATs | Existing behavior survives temporarily, including agent-held PATs. Never infer shareability from storage location. | Low to preserve; manual review/reconnect to migrate. GitHub setup continues to offer App connection only. |

Costs are relative work estimates, not delivery dates. Start by recording identities and grants without widening access; migrate only after owner review. Keep existing sessions isolated and start shared work fresh. Stopping access must cover cookies, running browsers and usable tokens; revocation cannot retract viewed results. The connected shared runtime still needs the multiplayer design's contract checks and formal/replay evidence. A simpler user model does not remove that work.

Source checks at `77ddd92f2`: [environment mounting](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/credential_env.py), [caller vaults](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/mcp_vault.py), [MCP credential mirroring and personal OAuth precedence](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/agent_mcp_credentials.py), [per-caller session lookup](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/stores/thread_sessions.py). The [architecture](../architecture.md) supplies context; the source distinguishes current App vaults and per-caller lookup more precisely than its older summaries. No deployment credential inventory was read.

## Issue tests and disposition

[#790: define isolation](https://github.com/pymc-labs/daimon/issues/790) is covered by the scope table and distinction between execution, shared memory/credentials and result visibility. Keep it open while this model is a discussion draft. When the model is accepted and lands, link this document and close the documentation portion; re-scope any remaining code-vs-model verification with explicit examples. A proposed guarantee is not evidence that it is implemented.

[#788: files cross between users](https://github.com/pymc-labs/daimon/issues/788) remains an unresolved bug report. No reproduction has been performed in this design-only effort. The report gives no session IDs, file IDs, message IDs or deployed revision. Per-account lookup contradicts intentional cross-user sandbox sharing, but does not prove that every preparation or delivery path preserved it during the reported incident.

Current output delivery lists files by MA session ID and Discord/Slack schedule sweeps using the outcome's session ID. The Discord code explicitly permits a later turn's file to land on an earlier answer while the same session's sweep settles. That explains a possible same-session attribution error, not cross-account mixing by itself. [Core sweep](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/core/daimon/core/output_delivery.py#L162), [Discord scheduling](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/adapters/discord/daimon/adapters/discord/bot.py#L775), [Slack scheduling](https://github.com/pymc-labs/daimon/blob/77ddd92f23f1f6dc100aca665c83f3bf6bb06f05/packages/adapters/slack/daimon/adapters/slack/app.py#L2910).

The follow-up needs a two-user reproduction owner, not closure as intended sharing:

1. Use two distinct accounts, one agent and one thread on a recorded revision. Each creates a distinct synthetic file with known bytes; keep one file unpublished and publish another. Record account, turn, session, file and destination-message IDs, plus content hashes.
2. Verify the accounts resolve to different sessions. Inspect sandbox files separately from chat history, downloaded attachments, shared memory and shared repos. Repeat with overlapping output delivery, then with identical filenames and different contents.
3. Classify the result: an attachment visible to both thread readers is expected; unpublished bytes crossing sandbox boundaries violate the current model; bytes attached to another caller's answer violate delivery attribution even when both readers may see them. Session IDs and hashes must establish which occurred.

When this model lands, re-scope #788 around that evidence and the failing path, preserving the original report. Proposed multiplayer sharing neither resolves nor legitimizes an existing isolation bug. No live test, bug fix or successful reproduction is claimed here.

## Product choices still open

1. Can everyone an agent is approved to serve use its team connections, with results visible to its approved readers? Recommend yes, after the authorized manager records that sharing and its scope. Prefer one approved audience per agent over a new permission matrix for every tool.
2. Should personal connections default to private work, and stay absent from the first shared release? Recommend yes, with exact owner-authorized actions considered later. Today's per-caller channel sessions are not private conversations; inventory that usage before changing it.
3. May an eligible member authorize an exact shared-service call under their own identity? Current approvals are requester-only. Changing that rule needs an explicit decision; until then, approval-requiring shared calls remain unavailable where requester attribution cannot be proved.
4. Later, should ordinary GitHub sessions stop adding the caller's linked permission automatically? Recommend explicit connection selection as the eventual destination. It is not required to enable shared work and is outside its first build. Inventory uses and replace personal-write workflows before any migration. Today's baseline can exceed the agent ceiling; that ceiling limits only caller uplift.

No answers have been accepted. The [shared-thread proposal](https://github.com/pymc-labs/daimon/blob/671e5394a6169abaa0bbd7da1ddb7733cdba1a5e/docs/design/multiplayer-threads-2026-10.md) is input, and its three choices remain paused. Its proposed action card lets an eligible member authorize a specific service action as themselves; it never lends another person's rights. Existing personal approvals remain requester-only. Delayed-call attribution, credential bypass and revocation still need evidence before build; no formal result is claimed here.

## Consultation

One read-only Claude Opus 5.5 high consultation completed on 2026-10-11; the response metadata confirmed `claude-opus-5-5`. Question: does this simplify identity across the four cases without hiding authority or audience problems?

Verdict: the core works, but the draft introduced too many roles and understated sharing that exists today. The revision starts with one agent audience, identifies existing shared keys/baselines, makes creator authority explicit, and separates future GitHub changes from the shared release. It records manager responsibility and retains provider authority checks where available; a declaration alone is not proof of resource rights. Browser isolation alternatives are now explicit. No independent validation of implementation or provider contracts is implied.

Multiplayer input received 2026-10-11 was incorporated into the one-page questions: separate account/requester/approver, enforcement across credential paths, permission to disclose retained results, browser state and tenant-key scope, and a bounded revocation promise separate from workspace deletion. Supporting concurrency and recovery details remain in shared-thread sections 5.1-5.5 and 9. This input accepts no product choice and authorizes no build.

Issue intake received 2026-10-11: #790 and #788 are acceptance material for this model. Scope coverage and the outstanding reproduction are recorded above; issue closure/re-scoping is deferred until the model lands. No implementation mandate was added.

Discussion receipt, 2026-10-11: ordinary threads have changing participants; avoid a fixed start-time roster and explain effects on existing features. The clarification above separates shared files from credential ownership and inventories source-supported personal workflows without claiming observed adoption. No product decision or build authorization was inferred.
