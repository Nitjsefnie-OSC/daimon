"""Make existing GitHub grants and pending choices write access.

downgrade: unsupported
"""

from alembic import op

revision: str = "0097_one_github_token"
down_revision: str | None = "0096_keep_legacy_working_repo"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE agent_github_grants SET baseline_access = 'write', "
        "ceiling_access = 'write', version = version + 1 "
        "WHERE baseline_access <> 'write' OR ceiling_access <> 'write'"
    )
    op.execute(
        "UPDATE agent_github_grant_drafts SET baseline_access = 'write', "
        "ceiling_access = 'write' WHERE operation = 'upsert'"
    )
    op.execute(
        "UPDATE tenant_github_repos SET max_access = 'write', version = version + 1 "
        "WHERE max_access <> 'write' AND EXISTS ("
        "SELECT 1 FROM agent_github_grants AS grant_row "
        "WHERE grant_row.tenant_id = tenant_github_repos.tenant_id "
        "AND grant_row.repo_id = tenant_github_repos.repo_id "
        "AND (tenant_github_repos.scope_agent_id IS NULL "
        "OR tenant_github_repos.scope_agent_id = grant_row.agent_id))"
    )
    op.execute(
        "UPDATE github_access_requests SET required_ability = 'write' "
        "WHERE required_ability <> 'write'"
    )


def downgrade() -> None:
    raise NotImplementedError("Cannot restore prior GitHub access levels")
