"""Operator-supplied exceptions for the GitHub App-only migration."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable

from daimon.core.ma_identity import derive_agent_uuid

ACCEPTED_LOSS_ENV = "DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS"


def retirement_repo_name_sql(column: str) -> str:
    """SQL mirror of normalize_owner_repo: remove .git before trailing slashes."""
    return (
        "lower(rtrim(regexp_replace(regexp_replace("
        f"{column}, "
        r"'^(https://github\.com/|http://github\.com/|github\.com/)', ''), "
        r"'\.git$', ''), '/'))"
    )


# Select the confirmation before testing its status, as repo_for_agent does.
# The installation checks mirror list_live_grant_repositories. Keep this CTE
# shared by the guard, the working-repo carry, and preview diagnostics.
RETIREMENT_LIVE_GRANTS_SQL = """
    SELECT g.tenant_id, g.agent_id, g.repo_id, g.is_working_repo,
           lower(r.repo_full_name) AS repo_name, r.repo_full_name
    FROM agent_github_grants g
    JOIN LATERAL (
        SELECT r.repo_full_name, r.status, r.installation_id
        FROM tenant_github_repos r
        WHERE r.tenant_id = g.tenant_id AND r.repo_id = g.repo_id
          AND (r.scope_agent_id = g.agent_id OR r.scope_agent_id IS NULL)
        ORDER BY r.scope_agent_id IS NULL
        LIMIT 1
    ) r ON true
    JOIN github_app_installations i ON i.installation_id = r.installation_id
    WHERE NOT g.staged AND r.status = 'active' AND i.suspended_at IS NULL
      AND r.repo_full_name = ANY(i.repo_full_names)
"""

# Use the same credential inventory for the loss guard and the sessions that
# must stop carrying retired credentials. A binding has no tenant column; its
# owner is resolved only from a scoped row or an exact known live session.
RETIREMENT_AFFECTED_AGENTS_SQL = """
    SELECT tenant_id, agent_id FROM agent_files
      WHERE key IN ('GH_TOKEN', 'GITHUB_TOKEN')
    UNION SELECT tenant_id, agent_id FROM agent_repo_binding
    UNION SELECT tenant_id, agent_id FROM agent_skill_repo_credentials
    UNION SELECT m.tenant_id, b.agent_id FROM agent_github_binding b
      LEFT JOIN agent_github_mode m ON m.agent_id=b.agent_id
"""

# Each tenant-scoped source contributes independently for an affected agent.
# In particular, mode and grant rows must be considered even without a legacy
# binding; a disagreement is unsafe to resolve by choosing either tenant.
RETIREMENT_TENANT_EVIDENCE_SQL = f"""
    WITH scoped AS (
        SELECT tenant_id, agent_id FROM agent_github_mode
        UNION SELECT tenant_id, agent_id FROM agent_files
          WHERE key IN ('GH_TOKEN', 'GITHUB_TOKEN')
        UNION SELECT tenant_id, agent_id FROM agent_repo_binding
        UNION SELECT tenant_id, agent_id FROM agent_skill_repo_credentials
        UNION SELECT tenant_id, agent_id FROM agent_github_grants
    ), affected_agents AS (
        SELECT DISTINCT agent_id FROM ({RETIREMENT_AFFECTED_AGENTS_SQL}) a
    )
    SELECT scoped.tenant_id, scoped.agent_id
    FROM scoped JOIN affected_agents USING (agent_id)
"""

RETIREMENT_LIVE_SESSIONS_SQL = """
    SELECT id, tenant_id, ma_agent_id FROM thread_sessions WHERE status = 'live'
"""

RETIREMENT_POST_MARK_TURNS_SQL = """
    SELECT a.thread_session_id, a.ma_session_id, a.marked_at,
           o.id AS turn_outcome_id, o.started_at, o.reason
    FROM github_retirement_session_audit AS a
    JOIN turn_outcomes AS o
      ON o.session_id = a.ma_session_id AND o.started_at > a.marked_at
    ORDER BY a.thread_session_id, o.started_at
"""

RETIREMENT_PENDING_SESSIONS_SQL = """
    SELECT a.thread_session_id, a.ma_session_id, a.marked_at
    FROM github_retirement_session_audit AS a
    JOIN thread_sessions AS s ON s.id = a.thread_session_id
    WHERE s.status = 'live' AND s.ma_session_id = a.ma_session_id
    ORDER BY a.thread_session_id
"""


def resolve_retirement_tenants(
    affected: Iterable[tuple[uuid.UUID | None, uuid.UUID]],
    scoped_evidence: Iterable[tuple[uuid.UUID, uuid.UUID]],
    live_sessions: Iterable[tuple[uuid.UUID, str | None]],
) -> tuple[dict[uuid.UUID, uuid.UUID], set[uuid.UUID]]:
    """Resolve every retired credential to one tenant, or report its agent UUID.

    A credential with no tenant evidence at all, such as a saved key left behind
    by a deleted agent, has no session to mark when every live session's agent
    is known: any of its sessions would have named a tenant. It is left out of
    both results. While a live session's agent is unknown, it could be that
    agent's, so the credential stays unresolved.
    """
    candidates: dict[uuid.UUID, set[uuid.UUID]] = {}
    for tenant_id, agent_id in affected:
        tenants = candidates.setdefault(agent_id, set())
        if tenant_id is not None:
            tenants.add(tenant_id)
    for tenant_id, agent_id in scoped_evidence:
        if agent_id in candidates:
            candidates[agent_id].add(tenant_id)
    unknown_identity = False
    for tenant_id, ma_agent_id in live_sessions:
        if ma_agent_id is None:
            unknown_identity = True
            continue
        agent_id = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
        if agent_id in candidates:
            candidates[agent_id].add(tenant_id)
    resolved = {
        agent: next(iter(tenants)) for agent, tenants in candidates.items() if len(tenants) == 1
    }
    unresolved = {
        agent
        for agent, tenants in candidates.items()
        if len(tenants) > 1 or (not tenants and unknown_identity)
    }
    return resolved, unresolved


# The migration and operator preview must use the same loss predicate.
# A live session's snapshot and short-lived token inventory are deliberately
# absent: marked legacy-credential sessions start fresh, while other App
# sessions renew tokens from live grants at the next turn boundary.
RETIREMENT_LOSS_SQL = f"""
    WITH affected AS (
        {RETIREMENT_AFFECTED_AGENTS_SQL}
    ), live_grants AS (
        {RETIREMENT_LIVE_GRANTS_SQL}
    )
    SELECT DISTINCT a.agent_id FROM affected a
    LEFT JOIN agent_github_mode m
      ON m.tenant_id=a.tenant_id AND m.agent_id=a.agent_id
    WHERE m.mode IS DISTINCT FROM 'app'
       OR NOT EXISTS (
           SELECT 1 FROM live_grants g
           WHERE g.tenant_id=a.tenant_id AND g.agent_id=a.agent_id)
       OR (NOT EXISTS (
               SELECT 1 FROM agent_repo_binding b
               WHERE b.tenant_id=a.tenant_id AND b.agent_id=a.agent_id)
           AND NOT EXISTS (
               SELECT 1 FROM live_grants g
               WHERE g.tenant_id=a.tenant_id AND g.agent_id=a.agent_id
                 AND g.is_working_repo))
       OR EXISTS (
           SELECT 1 FROM agent_repo_binding b
           WHERE b.tenant_id=a.tenant_id AND b.agent_id=a.agent_id
             AND NOT EXISTS (
                 SELECT 1 FROM live_grants g
                 WHERE g.tenant_id=b.tenant_id AND g.agent_id=b.agent_id
                   AND g.repo_name={retirement_repo_name_sql("b.repo_url")}))
       OR EXISTS (
           SELECT 1 FROM agent_skill_repo_credentials sc
           WHERE sc.tenant_id=a.tenant_id AND sc.agent_id=a.agent_id
             AND NOT EXISTS (
                 SELECT 1 FROM live_grants g
                 WHERE g.tenant_id=sc.tenant_id AND g.agent_id=sc.agent_id
                   AND g.repo_name={retirement_repo_name_sql("sc.repo_url")}))
"""


def accepted_loss_agent_ids() -> set[uuid.UUID]:
    """Parse the comma-separated agent UUID allowlist, rejecting malformed input."""
    raw = os.environ.get(ACCEPTED_LOSS_ENV, "")
    if not raw.strip():
        return set()
    try:
        return {uuid.UUID(item.strip()) for item in raw.split(",")}
    except ValueError as exc:
        raise ValueError(f"{ACCEPTED_LOSS_ENV} must contain only agent UUIDs") from exc
