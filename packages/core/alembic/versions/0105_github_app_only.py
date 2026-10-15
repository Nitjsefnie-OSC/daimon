"""Retire legacy GitHub rows while keeping the schema readable by old processes.

downgrade: unsupported
"""

import sqlalchemy as sa
from alembic import op
from daimon.core.github_retirement import (
    RETIREMENT_AFFECTED_AGENTS_SQL,
    RETIREMENT_LIVE_GRANTS_SQL,
    RETIREMENT_LIVE_SESSIONS_SQL,
    RETIREMENT_LOSS_SQL,
    RETIREMENT_TENANT_EVIDENCE_SQL,
    accepted_loss_agent_ids,
    resolve_retirement_tenants,
    retirement_repo_name_sql,
)
from daimon.core.ma_identity import derive_agent_uuid

revision: str = "0105_github_app_only"
down_revision: str | None = "0104_agent_tool_rules"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Hold the loss inputs and session mappings stable until retirement finishes.
    # Every writer must wait while this transaction decides coverage and marks
    # sessions whose credentials are about to be retired.
    op.execute(
        "LOCK TABLE agent_files, agent_github_binding, agent_github_grants, "
        "agent_github_mode, agent_repo_binding, agent_skill_repo_credentials, "
        "credential_requests, github_credentials, github_oauth_states, tenant_github_repos, "
        "github_app_installations, thread_sessions "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    bind = op.get_bind()
    affected = bind.execute(
        sa.text(f"SELECT DISTINCT tenant_id, agent_id FROM ({RETIREMENT_AFFECTED_AGENTS_SQL}) a")
    ).all()
    scoped_evidence = bind.execute(sa.text(RETIREMENT_TENANT_EVIDENCE_SQL)).all()
    live_sessions = bind.execute(sa.text(RETIREMENT_LIVE_SESSIONS_SQL)).all()
    tenant_by_agent, unresolved = resolve_retirement_tenants(
        ((row.tenant_id, row.agent_id) for row in affected),
        ((row.tenant_id, row.agent_id) for row in scoped_evidence),
        ((row.tenant_id, row.ma_agent_id) for row in live_sessions),
    )
    if unresolved:
        agents = ", ".join(str(agent) for agent in sorted(unresolved))
        raise RuntimeError(
            f"GitHub App-only migration cannot resolve tenant for agent(s): {agents}"
        )
    # Fail closed before deleting any row. Staged grants are excluded from
    # coverage because they never conferred access.
    loss_rows = bind.execute(sa.text(RETIREMENT_LOSS_SQL)).scalars().all()
    if set(loss_rows) - accepted_loss_agent_ids():
        raise RuntimeError(
            "GitHub App-only migration would retire unapproved access; "
            "run daimon github retirement-preview"
        )
    # Preserve the exact original session ID and marking time for the release
    # gate. Old processes can complete reads against this additive schema.
    op.create_table(
        "github_retirement_session_audit",
        sa.Column("thread_session_id", sa.UUID(), primary_key=True),
        sa.Column("ma_session_id", sa.Text(), nullable=False),
        sa.Column("marked_at", sa.DateTime(timezone=True), nullable=False),
    )
    # The old and new workers both honor this marker on the next turn. Read
    # the credential owners before deleting their rows, then mark their live
    # sessions and unknown-identity sessions that might belong to them. A mounted
    # .env containing only a GitHub key can otherwise survive backfill because
    # both the old and desired env hashes are None.
    if tenant_by_agent:
        credential_tenants = set(tenant_by_agent.values())
        known_matches: set[object] = set()
        for row in live_sessions:
            if row.ma_agent_id is None:
                continue
            agent_id = derive_agent_uuid(tenant_id=row.tenant_id, ma_agent_id=row.ma_agent_id)
            if agent_id in tenant_by_agent:
                known_matches.add(row.id)
        marked_ids = [
            row.id
            for row in live_sessions
            if row.id in known_matches
            or (row.ma_agent_id is None and row.tenant_id in credential_tenants)
        ]
        if marked_ids:
            bind.execute(
                sa.text("""
                    WITH marked AS (
                        UPDATE thread_sessions
                        SET fresh_start_requested_at =
                            COALESCE(fresh_start_requested_at, statement_timestamp())
                        WHERE id = ANY(:ids)
                        RETURNING id, ma_session_id
                    )
                    INSERT INTO github_retirement_session_audit
                        (thread_session_id, ma_session_id, marked_at)
                    SELECT id, ma_session_id, statement_timestamp() FROM marked
                """),
                {"ids": marked_ids},
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
