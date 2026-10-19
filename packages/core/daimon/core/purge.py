"""GDPR purge orchestrator.

Single core entry point that deletes every row referencing a principal (or all
principals under an account) across stores, in FK-safe order, inside a single
transaction. Idempotent on re-run; rolls back fully on any helper raise.

Registry note: the per-store delete sequence is hardcoded inside
`_purge_principal_in_session`. Adding a new principal-scoped table means
appending one helper call here plus one int field on `PurgeReport`. Current
sequence: user_skills -> github_credentials -> agent_github_binding ->
github_oauth_states (both kinds where the table permits) -> credential_requests
(both kinds, platform-user-scoped like github_oauth_states) -> wizard_session
(both kinds, platform-user-scoped like credential_requests) -> message_feedback
(platform only, platform-user-scoped) -> routines (platform only) ->
principal_links -> principal row -> platform_user_names (platform only,
platform-user-scoped, after the principal row on purpose: see the call) ->
teams_messages and microsoft_user_tokens (Teams principals only, keyed by (tenant,
Entra user)).
Account-level deletes (mcp_tokens, GitHub connect requests and click intents,
message_feedback, user_configs, accounts)
run in `purge_account` after all principal rows are gone; mcp_tokens and
message_feedback are keyed by account_id and are deleted before delete_account
so their CASCADE FKs to accounts.id are satisfied. message_feedback is deleted
by an account-id predicate OR'd with the account's (tenant, platform-user)
keys, because a vote cast before the person had an account row carries a null
account id.

message_feedback is the one table deleted on BOTH paths, because it is the
one table whose rows can carry either identity key. The principal path
deletes only the principal's own (tenant, platform-user) rows; the account
path then sweeps whatever the account-id predicate still reaches. The two
passes cannot double-count — the second only sees what the first left — so
the summed report still equals `collect_purge_preview`'s single count.

wizard_session rides the platform-user-scoped delete path rather than an
accounts.id cascade: the row's identity key is the requester's platform user
id (a Discord/Slack tap must resolve back to a form regardless of whether an
account row exists yet), and the nullable accounts FK on the table exists only
to make the schema-reflecting drift guard see the table, not to delete through.

credential_requests carries a Discord snowflake in requester_platform_user_id
with no cleanup sweep by design (the table is one row per credential request,
TTL-bounded relevance) — erasure therefore rides this purge path exactly like
the OAuth handshake table, rather than a scheduled job. Precedent sweepers
(`mcp_credential_sweep.py`, `pending_file_sweeper.py`) exist if volume ever
justifies one instead.

Divergent helper signatures: identity-store `delete_for_principal` is keyed by
UUID; routines `delete_for_principal` is keyed by `(platform, external_id)`
because routines reference platform users by their external id, not the
principal UUID. The orchestrator dispatches manually rather than via a unified
Protocol.

Deliberate carve-outs:
- Shared setup conversations retain their binding and other participants' work;
  account deletion removes creator attribution through ON DELETE SET NULL.
  Short-lived turn origins are removed through the account's ON DELETE CASCADE,
  including any still-active authority; neither needs an explicit delete helper.
- Hosted chart artifacts in operator-owned object storage are not deleted by
  account purge; operators must enforce retention with a bucket lifecycle rule.
- `usage_events` and `tenant_user_caps` rows are retained for billing integrity.
  A `channel_budgets` row belongs to the tenant; erasure only nulls the
  `set_by_account_id` of the admin who set it, through ON DELETE SET NULL.
  Their `delete_all_for_user` helpers exist and are deliberately uncalled here.
  `promo_redemptions` rows stay with the tenant too; account deletion only
  clears who redeemed the code, through ON DELETE SET NULL.
- Uploaded MA skill files (user_skills rows) are our DB ledger: the DB row is
  deleted as part of purge, but the uploaded file content inside Anthropic's
  Managed Agents is retained — guild-shared agents may still reference the
  underlying skill and the MA workspace is not under our delete authority.
  A skill added by hand is keyed by its agent, not the person who added it;
  erasure only nulls its `added_by_account_id`, through ON DELETE SET NULL.
- The user's GitHub-side OAuth grant is not revoked. No GitHub API client enters
  the purge path — we delete only our encrypted credential and oauth-state rows
  from our own DB.
- An agent's shared memory store is agent-scoped and shared across all
  workspace users, so account purge does NOT touch it — archiving it because a
  single member invoked erasure would destroy the guild's shared agent memory.
  It may retain information about the purged user. The person's own personal
  memory stores (one per agent they talked to, archived ones included) ARE
  deleted upstream after the DB transaction commits; their bindings carry no
  FK to `accounts`, so they survive the account row as the record of what is
  left, and `sweep_privacy_session_deletes` finishes any delete that failed or
  never ran.
- message_feedback rows cast in a tenant where the person has NO principal are
  out of reach of an erasure run from another tenant. This is the one table
  that records rows for someone with no principal anywhere: the reaction path
  deliberately never mints one for a bystander, so a vote (and any free text
  they then submit) lands with account_id = NULL, attributable only by
  (tenant_id, platform_user_id). An erasure invoked in guild A derives its
  platform-user keys from the account's own principals, which do not include
  (tenant_B, user), so guild B's row survives. Unlike the tenant-scoped
  oauth-state ghost rows above, these are durable and hold free text.
  Remediation is guild-local and self-service: running the privacy purge
  inside guild B mints a tenant-B principal, which brings (tenant_B, user)
  into the key set and erases those rows. Deliberately not closed by a
  platform-scoped, tenant-agnostic predicate: it would be correct only for
  platforms whose external ids are globally unique (Discord snowflakes, not
  Slack workspace-scoped user ids), so it would make erasure's blast radius
  depend on the platform — a worse contract than one documented, reachable
  remediation path.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Literal

import structlog
from anthropic import APIError, AsyncAnthropic
from daimon.core import platform_names
from daimon.core.ma import SessionDeletionReport, delete_sessions_for_account
from daimon.core.memory_resource import (
    delete_personal_memory_stores_for_account,
    sweep_erased_personal_memory,
)
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import agent_posts as agent_posts_store
from daimon.core.stores import channel_admins as channel_admins_store
from daimon.core.stores import credential_requests as credential_requests_store
from daimon.core.stores import direct_messages as direct_messages_store
from daimon.core.stores import gemini_runtime_state as gemini_state_store
from daimon.core.stores import github_connect as github_connect_store
from daimon.core.stores import github_issued_tokens as github_issued_tokens_store
from daimon.core.stores import github_links as github_links_store
from daimon.core.stores import identity as identity_store
from daimon.core.stores import lead_intake as lead_intake_store
from daimon.core.stores import mcp_tokens as mcp_tokens_store
from daimon.core.stores import message_feedback as message_feedback_store
from daimon.core.stores import microsoft_user_tokens as microsoft_tokens_store
from daimon.core.stores import platform_names as platform_names_store
from daimon.core.stores import privacy_session_deletes as privacy_deletes_store
from daimon.core.stores import routines as routines_store
from daimon.core.stores import security_audit as security_audit_store
from daimon.core.stores import slack_turn_contexts as slack_turn_contexts_store
from daimon.core.stores import slack_user_tokens as slack_user_tokens_store
from daimon.core.stores import support_escalation as support_escalation_store
from daimon.core.stores import teams_message_admissions as teams_admissions_store
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import user_skills as user_skills_store
from daimon.core.stores import watches as watches_store
from daimon.core.stores import wizard_session as wizard_session_store
from daimon.core.stores.domain import CliPrincipalRow, PlatformPrincipalRow
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

Principal = CliPrincipalRow | PlatformPrincipalRow


class PurgeReport(BaseModel):
    """Per-table rowcount summary returned by purge entry points."""

    model_config = ConfigDict(frozen=True)

    watches: int = 0
    routines: int = 0
    principal_links: int = 0
    cli_principals: int = 0
    platform_principals: int = 0
    user_configs: int = 0
    accounts: int = 0
    user_skills: int = 0
    github_connect_requests: int = 0
    github_connect_click_intents: int = 0
    github_user_links: int = 0
    mcp_tokens: int = 0
    gemini_records: int = 0
    slack_user_tokens: int = 0
    slack_turn_contexts: int = 0
    direct_message_conversations: int = 0
    credential_requests: int = 0
    wizard_sessions: int = 0
    message_feedback: int = 0
    support_escalations: int = 0
    channel_admins: int = 0
    agent_post_requesters: int = 0
    platform_user_names: int = 0
    teams_messages: int = 0
    microsoft_user_tokens: int = 0
    lead_intakes: int = 0

    def merge(self, other: PurgeReport) -> PurgeReport:
        return PurgeReport(
            lead_intakes=self.lead_intakes + other.lead_intakes,
            watches=self.watches + other.watches,
