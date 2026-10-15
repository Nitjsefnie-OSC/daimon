"""Retire legacy GitHub rows while keeping the schema readable by old processes.

downgrade: unsupported
"""

import sqlalchemy as sa
from alembic import op
from daimon.core.github_retirement import (
    RETIREMENT_LIVE_GRANTS_SQL,
    RETIREMENT_LOSS_SQL,
    accepted_loss_agent_ids,
    retirement_repo_name_sql,
)

revision: str = "0105_github_app_only"
down_revision: str | None = "0104_agent_tool_rules"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Hold the tables used by the loss query stable until retirement finishes.
    # Every writer to these tables must wait while this transaction decides
    # whether its current rows are covered by live App grants.
    op.execute(
        "LOCK TABLE agent_files, agent_github_binding, agent_github_grants, "
        "agent_github_mode, agent_repo_binding, agent_skill_repo_credentials, "
        "credential_requests, github_credentials, github_oauth_states, tenant_github_repos, "
        "github_app_installations "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    # Fail closed before deleting any row. Staged grants are excluded from
    # coverage because they never conferred access.
    loss_rows = op.get_bind().execute(sa.text(RETIREMENT_LOSS_SQL)).scalars().all()
    if set(loss_rows) - accepted_loss_agent_ids():
        raise RuntimeError(
            "GitHub App-only migration would retire unapproved access; "
            "run daimon github retirement-preview"
        )
    # Old workers can still create staged grants before their containers refresh.
    # New workers use app_visible and never read or write staged. The trigger
    # keeps both fields in sync and records App mode for later visible grants;
    # new inserts default staged to false.
    op.execute("ALTER TABLE agent_github_grants ALTER COLUMN staged SET DEFAULT false")
    op.add_column(
        "agent_github_grants",
        sa.Column("app_visible", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("UPDATE agent_github_grants SET app_visible = NOT staged")
    op.execute("""
        CREATE FUNCTION sync_github_grant_visibility() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' OR NEW.staged IS DISTINCT FROM OLD.staged THEN
                NEW.app_visible := NOT NEW.staged;
            ELSE
                NEW.staged := NOT NEW.app_visible;
            END IF;
            IF NEW.app_visible THEN
                INSERT INTO agent_github_mode (tenant_id, agent_id, mode)
                VALUES (NEW.tenant_id, NEW.agent_id, 'app')
                ON CONFLICT (tenant_id, agent_id) DO UPDATE SET mode = 'app';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER trg_github_grant_visibility
        BEFORE INSERT OR UPDATE OF staged, app_visible ON agent_github_grants
        FOR EACH ROW EXECUTE FUNCTION sync_github_grant_visibility()
    """)
    # Staged rows never conferred access. Delete them before carrying a working
    # repo marker so a legacy binding cannot turn one into a live grant.
    op.execute("DELETE FROM agent_github_grants WHERE staged")
    # An unusable grant's marker must not block carrying a legacy binding to a
    # usable grant. Otherwise retain the marker: a suspended installation can
    # recover without changing its grant, and the repo should mount again.
    op.execute(f"""
        WITH live_grants AS ({RETIREMENT_LIVE_GRANTS_SQL})
        UPDATE agent_github_grants g
        SET is_working_repo = false, version = g.version + 1
        WHERE g.is_working_repo AND NOT EXISTS (
            SELECT 1 FROM live_grants l
            WHERE l.tenant_id = g.tenant_id AND l.agent_id = g.agent_id
              AND l.repo_id = g.repo_id)
          AND EXISTS (
            SELECT 1 FROM agent_repo_binding b
            JOIN live_grants target
              ON target.tenant_id = b.tenant_id AND target.agent_id = b.agent_id
             AND target.repo_name = {retirement_repo_name_sql("b.repo_url")}
            WHERE b.tenant_id = g.tenant_id AND b.agent_id = g.agent_id)
    """)
    # A binding can designate the working repo only if the agent already has
    # exactly one matching, authorized App grant and has not selected another.
    op.execute(f"""
        WITH live_grants AS (
            {RETIREMENT_LIVE_GRANTS_SQL}
        ), matches AS (
            SELECT g.tenant_id, g.agent_id, g.repo_id,
                   count(*) OVER (PARTITION BY g.tenant_id, g.agent_id) AS match_count
            FROM agent_repo_binding b
            JOIN live_grants g USING (tenant_id, agent_id)
            WHERE g.repo_name = {retirement_repo_name_sql("b.repo_url")}
              AND NOT EXISTS (
                  SELECT 1 FROM agent_github_grants w
                  WHERE w.tenant_id = g.tenant_id AND w.agent_id = g.agent_id
                    AND w.is_working_repo)
        )
        UPDATE agent_github_grants g
        SET is_working_repo = true, version = g.version + 1
        FROM matches m
        WHERE m.match_count = 1 AND g.tenant_id = m.tenant_id
          AND g.agent_id = m.agent_id AND g.repo_id = m.repo_id
    """)
    # The deployed worker treats a missing mode row as legacy and would stage
    # a retained grant after its installation recovers. Give every agent with
    # an App-visible grant App mode, even when that grant is unusable today.
    op.execute("""
        INSERT INTO agent_github_mode (tenant_id, agent_id, mode)
        SELECT DISTINCT tenant_id, agent_id, 'app'
        FROM agent_github_grants WHERE app_visible
        ON CONFLICT (tenant_id, agent_id) DO UPDATE SET mode = 'app'
    """)
    # Retain the opaque MA credential IDs until the operator cleanup command
    # has removed external vault entries. No token value is copied here.
    op.create_table(
        "github_legacy_vault_cleanup",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("credential_id", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "credential_id"),
    )
    op.execute("""
        INSERT INTO github_legacy_vault_cleanup (tenant_id, agent_id, credential_id)
        SELECT tenant_id, agent_id, ma_secret_ref FROM agent_repo_binding
        WHERE ma_secret_ref NOT LIKE 'anon:%' AND ma_secret_ref NOT LIKE 'inline-pat:%'
          AND ma_secret_ref <> ''
        UNION
        SELECT tenant_id, agent_id, ma_secret_ref FROM agent_skill_repo_credentials
        WHERE ma_secret_ref NOT LIKE 'anon:%' AND ma_secret_ref NOT LIKE 'inline-pat:%'
          AND ma_secret_ref <> ''
    """)
    op.execute("DELETE FROM agent_files WHERE key IN ('GH_TOKEN', 'GITHUB_TOKEN')")
    op.execute("DELETE FROM credential_requests WHERE kind IN ('repo', 'skill_repo')")
    # Keep these tables for old readers and writers until every process has
    # switched images. Mode rows remain App throughout that overlap.
    for table in (
        "agent_github_binding",
        "agent_repo_binding",
        "agent_skill_repo_credentials",
        "github_credentials",
        "github_oauth_states",
    ):
        op.execute(f"DELETE FROM {table}")


def downgrade() -> None:
    raise NotImplementedError("Retired GitHub keys cannot be restored")
