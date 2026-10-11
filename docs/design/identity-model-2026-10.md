# Agent connections and work audiences

Discussion draft, 2026-10-11. This proposes an identity model; it does not authorize implementation. Shared threads remain a separate, unaccepted proposal.

## The proposed model, in questions

**Can a team agent carry the team's access, whoever asks?** Proposed answer: yes. Its shared connections serve everyone the agent is approved to serve, including readers of its results. Personal sign-ins stay in work only their owner can see. Start with the agent's approved channels as that audience; give narrower work a separately restricted agent. Widening where an agent works must review its connections too.

**Who is doing the work?** A person asks for work and remains the requester. An agent performs it. A connection names the outside identity it uses, such as a GitHub App installation, a team website login, or an MCP server key. The provider sees that connection's identity, which may differ from the requester. Daimon should show both. An agent does not need its own account at every provider.

**Who can grant it?** A person grants their personal connection. An authorized manager adds a shared connection, records its account, permitted operations/resources and readers, and takes responsibility for having permission to share. Check provider authority where available, as GitHub setup already does. A pasted key alone cannot prove distribution rights. A tenant is the administrative and billing boundary; channel membership can define a team without another team-management system.

**Where does a thread fit?** A thread is where work happens and results appear. Its audience includes people invited later who can read earlier history. A per-person session in a channel does not make posted answers private. A fresh shared workspace receives approved shared connections, files and memory; it never inherits the creator's personal session. Membership and future-reader rules must fit those grants.

**What do I see?** Setup names the selected connection, signed-in account and readers: "Team Docs", "Signed in as docs-bot", "Visible to Project team". Save that choice; do not ask on every call or silently fall back to another login. Personal work starts in a private conversation. Sharing a result needs permission for its destination too. Existing tool confirmations remain; an approval never grants more access than the connection permits.

**Does a browser need another rule?** Its vendor API key opens browser capacity; website cookies and profiles carry the website identity. Treat the site login as a personal or shared connection. Isolate both the profile and the authority to open it.

**What changes first?** Existing agent keys and GitHub baseline access already serve multiple callers. Make that sharing visible and intentional. Add no automatic sharing during migration; existing per-caller behavior remains until reviewed. The proposed private-only default for personal connections changes today's channel use and needs agreement. Unknown secrets stay out of new shared work.

**Where do credentials live?** Daimon keeps connection ownership, grants and secret references. Protected stores or provider vaults hold tokens; the browser provider holds scoped profiles/cookies. The sandbox receives only authority approved for its audience. Users need not manage those stores. Daimon's own token currently carries its creator's authority; shared work needs a separate scoped service identity, with no creator rights.

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

## Product choices still open

1. Can everyone an agent is approved to serve use its team connections, with results visible to its approved readers? Recommend yes, after the authorized manager records that sharing and its scope. Prefer one approved audience per agent over a new permission matrix for every tool.
2. Should personal connections default to private work, and stay absent from the first shared release? Recommend yes, with exact owner-authorized actions considered later. Today's per-caller channel sessions are not private conversations; inventory that usage before changing it.
3. May an eligible member authorize an exact shared-service call under their own identity? Current approvals are requester-only. Changing that rule needs an explicit decision; until then, approval-requiring shared calls remain unavailable where requester attribution cannot be proved.
4. Later, should ordinary GitHub sessions stop adding the caller's linked permission automatically? Recommend explicit connection selection as the eventual destination. It is not required to enable shared work and is outside its first build. Inventory uses and replace personal-write workflows before any migration. Today's baseline can exceed the agent ceiling; that ceiling limits only caller uplift.

No answers have been accepted. The [shared-thread proposal](https://github.com/pymc-labs/daimon/blob/671e5394a6169abaa0bbd7da1ddb7733cdba1a5e/docs/design/multiplayer-threads-2026-10.md) is input, and its three choices remain paused. Its proposed action card lets an eligible member authorize a specific service action as themselves; it never lends another person's rights. Existing personal approvals remain requester-only. Delayed-call attribution, credential bypass and revocation still need evidence before build; no formal result is claimed here.

## Consultation

One read-only Claude Opus 5.5 high consultation completed on 2026-10-11; the response metadata confirmed `claude-opus-5-5`. Question: does this simplify identity across the four cases without hiding authority or audience problems?

Verdict: the core works, but the draft introduced too many roles and understated sharing that exists today. The revision starts with one agent audience, identifies existing shared keys/baselines, makes creator authority explicit, and separates future GitHub changes from the shared release. It records manager responsibility and retains provider authority checks where available; a declaration alone is not proof of resource rights. Browser isolation alternatives are now explicit. No independent validation of implementation or provider contracts is implied.
