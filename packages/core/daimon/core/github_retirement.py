"""Operator-supplied exceptions for the GitHub App-only migration."""

from __future__ import annotations

import os
import uuid

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

# The migration and operator preview must use the same loss predicate.
# A live session's snapshot and short-lived token inventory
# are deliberately absent: the new worker replaces legacy sessions and renews
# App tokens from these live grants at the next turn boundary.
RETIREMENT_LOSS_SQL = f"""
    WITH affected AS (
        SELECT tenant_id, agent_id FROM agent_files
          WHERE key IN ('GH_TOKEN', 'GITHUB_TOKEN')
        UNION SELECT tenant_id, agent_id FROM agent_repo_binding
        UNION SELECT tenant_id, agent_id FROM agent_skill_repo_credentials
        UNION SELECT m.tenant_id, b.agent_id FROM agent_github_binding b
          LEFT JOIN agent_github_mode m ON m.agent_id=b.agent_id
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
