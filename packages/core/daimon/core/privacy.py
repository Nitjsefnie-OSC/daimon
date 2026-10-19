"""Read-only snapshot of every daimon-side row that purge_account would remove.

Counterpart to `daimon.core.purge`: same FK-safe coverage, read-only. The
/privacy adapter renders this snapshot in the cascade-preview embed so the
user sees exactly what the subsequent `purge_account(...)` call will delete.
No Anthropic SDK calls — MA-side bodies, sessions, vault contents stay in
the Anthropic workspace and are not enumerated here.

Adding a new account- or principal-scoped table that `purge_account` learns to
delete MUST update this module too, or the preview undercounts. The schema-
reflecting drift-guard test in `tests/test_privacy.py` enforces this at CI time.

Scope of "every row": both this preview and the purge it mirrors are scoped to
the account's OWN principals. One residue class follows from that and is
documented in full under `daimon.core.purge`'s carve-outs — message_feedback
rows cast in a tenant where the person has no principal (a vote by a bystander
carries account_id = NULL and no principal is minted for it) are neither
previewed nor deleted by a run invoked from another tenant. The remediation is
guild-local and self-service: running this same privacy purge inside that guild
mints a principal there, which brings its (tenant, platform-user) key into
scope and erases those rows.
"""

from __future__ import annotations

import uuid
from typing import Final

from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import agent_posts as agent_posts_store
from daimon.core.stores import channel_admins as channel_admins_store
from daimon.core.stores import credential_requests as credential_requests_store
from daimon.core.stores import direct_messages as direct_messages_store
from daimon.core.stores import gemini_runtime_state as gemini_state_store
from daimon.core.stores import github_connect as github_connect_store
from daimon.core.stores import github_links as github_links_store
from daimon.core.stores import identity as identity_store
from daimon.core.stores import lead_intake as lead_intake_store
from daimon.core.stores import mcp_tokens as mcp_tokens_store
from daimon.core.stores import message_feedback as message_feedback_store
from daimon.core.stores import microsoft_user_tokens as microsoft_tokens_store
from daimon.core.stores import platform_names as platform_names_store
from daimon.core.stores import routines as routines_store
from daimon.core.stores import slack_turn_contexts as slack_turn_contexts_store
from daimon.core.stores import slack_user_tokens as slack_user_tokens_store
from daimon.core.stores import support_escalation as support_escalation_store
from daimon.core.stores import teams_message_admissions as teams_admissions_store
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import user_skills as user_skills_store
from daimon.core.stores import watches as watches_store
from daimon.core.stores import wizard_session as wizard_session_store
from daimon.core.stores.domain import CliPrincipalRow, PlatformPrincipalRow
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class PurgePreviewRow(BaseModel):
    """Per-category preview row: count plus a human-display example string."""

    model_config = ConfigDict(frozen=True)
    count: int
    # Human-display label; None when count == 0 or the category has no
    # human-meaningful label (principal_links, user_configs, account,
    # github_oauth_states set example=None at any count).
    example: str | None


class PurgePreview(BaseModel):
    """1:1 with PurgeReport categories — same FK-safe coverage as purge_account.

    `cli_principals` + `platform_principals` from PurgeReport are folded into
    `linked_principals` here for display. The drift-guard test maps every
    PurgeReport field to a PurgePreview target field; adding a new
    PurgeReport int without extending the mapping (and this model) breaks CI.
    """

    model_config = ConfigDict(frozen=True)
    linked_principals: PurgePreviewRow  # combined CLI + platform
    principal_links: PurgePreviewRow
    watches: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    routines: PurgePreviewRow
    user_configs: PurgePreviewRow
    account: PurgePreviewRow  # singular: 1 if row exists else 0
    user_skills: PurgePreviewRow
    github_connect_requests: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    github_connect_click_intents: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    github_user_links: PurgePreviewRow
    gemini_records: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    mcp_tokens: PurgePreviewRow
    slack_user_tokens: PurgePreviewRow
    slack_turn_contexts: PurgePreviewRow
    direct_message_conversations: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    credential_requests: PurgePreviewRow
    wizard_sessions: PurgePreviewRow
    message_feedback: PurgePreviewRow
    support_escalations: PurgePreviewRow
    channel_admins: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    agent_post_requesters: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    platform_user_names: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    teams_messages: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    microsoft_user_tokens: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    lead_intakes: PurgePreviewRow = PurgePreviewRow(count=0, example=None)


def summary_line(preview: PurgePreview) -> str:
    """The non-zero held-data categories, comma-joined, for the privacy panels."""
    categories = (
        (preview.linked_principals, "linked account"),
        (preview.routines, "routine"),
        (preview.lead_intakes, "lead intake"),
        (preview.watches, "page watch"),
