"""Retire saved GitHub keys without activating staged grants.

downgrade: unsupported
"""

import uuid
from typing import cast

import sqlalchemy as sa
from alembic import op
from daimon.core.github_retirement import accepted_loss_agent_ids
from daimon.core.ma_identity import derive_agent_uuid

revision: str = "0105_github_app_only"
down_revision: str | None = "0104_agent_tool_rules"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Hold the tables used by the loss query stable until retirement finishes.
    # Every writer to these tables must wait while this transaction decides
    # whether its current rows are covered by live App grants and tokens.
    op.execute(
        "LOCK TABLE agent_files, agent_github_binding, agent_github_grants, "
        "agent_github_mode, agent_repo_binding, agent_skill_repo_credentials, "
        "github_issued_tokens, tenant_github_repos, thread_sessions "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    # Fail closed before deleting any row. Staged grants are excluded from
    # coverage because they never conferred access.
    loss_rows = (
        op.get_bind()
        .execute(
            sa.text(r"""
        WITH affected AS (
            SELECT tenant_id, agent_id FROM agent_files
              WHERE key IN ('GH_TOKEN', 'GITHUB_TOKEN')
            UNION SELECT tenant_id, agent_id FROM agent_repo_binding
            UNION SELECT tenant_id, agent_id FROM agent_skill_repo_credentials
            UNION SELECT m.tenant_id, b.agent_id FROM agent_github_binding b
              LEFT JOIN agent_github_mode m ON m.agent_id=b.agent_id
        ), live_grants AS (
            SELECT g.tenant_id, g.agent_id, g.repo_id, g.is_working_repo,
                   lower(r.repo_full_name) AS repo_name
            FROM agent_github_grants g
            JOIN tenant_github_repos r USING (tenant_id, repo_id)
            WHERE NOT g.staged AND r.status='active'
              AND (r.scope_agent_id IS NULL OR r.scope_agent_id=g.agent_id)
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
                       AND g.repo_name=lower(regexp_replace(
                           regexp_replace(b.repo_url,
                               '^(https://github\.com/|http://github\.com/|github\.com/)', ''),
                           '(\.git)?/*$', ''))))
           OR EXISTS (
               SELECT 1 FROM agent_skill_repo_credentials sc
               WHERE sc.tenant_id=a.tenant_id AND sc.agent_id=a.agent_id
                 AND NOT EXISTS (
                     SELECT 1 FROM live_grants g
                     WHERE g.tenant_id=sc.tenant_id AND g.agent_id=sc.agent_id
                       AND g.repo_name=lower(regexp_replace(
                           regexp_replace(sc.repo_url,
                               '^(https://github\.com/|http://github\.com/|github\.com/)', ''),
                           '(\.git)?/*$', ''))))
    """)
        )
        .scalars()
        .all()
    )
    # An existing live session must already be on the App token path. A
    # missing snapshot or delivered token is treated as unverified access.
    session_loss: set[uuid.UUID] = set()
    affected_rows = (
        op.get_bind()
        .execute(
            sa.text("""
        SELECT tenant_id, agent_id FROM agent_files
          WHERE key IN ('GH_TOKEN', 'GITHUB_TOKEN')
        UNION SELECT tenant_id, agent_id FROM agent_repo_binding
        UNION SELECT tenant_id, agent_id FROM agent_skill_repo_credentials
        UNION SELECT m.tenant_id, b.agent_id FROM agent_github_binding b
          LEFT JOIN agent_github_mode m ON m.agent_id=b.agent_id
    """)
        )
        .all()
    )
    for tenant_id, agent_id in affected_rows:
        if tenant_id is None:
            session_loss.add(agent_id)
            continue
        working_repo_id = (
            op.get_bind()
            .execute(
                sa.text(r"""
            SELECT g.repo_id FROM agent_github_grants g
            JOIN tenant_github_repos r USING (tenant_id, repo_id)
            LEFT JOIN agent_repo_binding b
              ON b.tenant_id=g.tenant_id AND b.agent_id=g.agent_id
            WHERE g.tenant_id=:tenant AND g.agent_id=:agent
              AND NOT g.staged AND r.status='active'
              AND (r.scope_agent_id IS NULL OR r.scope_agent_id=g.agent_id)
              AND (g.is_working_repo OR lower(r.repo_full_name)=lower(regexp_replace(
                  regexp_replace(b.repo_url,
                      '^(https://github\.com/|http://github\.com/|github\.com/)', ''),
                  '(\.git)?/*$', '')))
            ORDER BY g.is_working_repo DESC LIMIT 1
        """),
                {"tenant": tenant_id, "agent": agent_id},
            )
            .scalar_one_or_none()
        )
        sessions = cast(
            list[tuple[str | None, str, dict[str, object] | None]],
            op.get_bind()
            .execute(
                sa.text("""
            SELECT ma_agent_id, ma_session_id, effective_config
            FROM thread_sessions WHERE tenant_id=:tenant AND status='live'
        """),
                {"tenant": tenant_id},
            )
            .all(),
        )
        for ma_agent_id, ma_session_id, snapshot in sessions:
            source_agent_id = ma_agent_id or (snapshot or {}).get("ma_agent_id")
            if (
                not isinstance(source_agent_id, str)
                or derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=source_agent_id) != agent_id
            ):
                continue
            delivered = (
                op.get_bind()
                .execute(
                    sa.text("""
                SELECT repo_ids FROM github_issued_tokens
                WHERE tenant_id=:tenant AND agent_id=:agent AND session_id=:session
                  AND status='delivered' AND expires_at > now()
            """),
                    {"tenant": tenant_id, "agent": agent_id, "session": ma_session_id},
                )
                .scalars()
                .all()
            )
            if (snapshot or {}).get("github_mode") != "app" or not any(
                working_repo_id in repo_ids for repo_ids in delivered
            ):
                session_loss.add(agent_id)
    if (set(loss_rows) | session_loss) - accepted_loss_agent_ids():
        raise RuntimeError(
            "GitHub App-only migration would retire unapproved access; "
            "run daimon github retirement-preview"
        )
    # Staged rows never conferred access. Delete them before carrying a working
    # repo marker so a legacy binding cannot turn one into a live grant.
    op.execute("DELETE FROM agent_github_grants WHERE staged")
    # A binding can designate the working repo only if the agent already has
    # exactly one matching, authorized App grant and has not selected another.
    op.execute(r"""
        WITH matches AS (
            SELECT g.tenant_id, g.agent_id, g.repo_id,
                   count(*) OVER (PARTITION BY g.tenant_id, g.agent_id) AS match_count
            FROM agent_repo_binding b
            JOIN agent_github_grants g USING (tenant_id, agent_id)
            JOIN tenant_github_repos r
              ON r.tenant_id = g.tenant_id AND r.repo_id = g.repo_id
            WHERE r.status = 'active'
              AND (r.scope_agent_id IS NULL OR r.scope_agent_id = g.agent_id)
              AND lower(r.repo_full_name) = lower(regexp_replace(
                  regexp_replace(b.repo_url,
                      '^(https://github\.com/|http://github\.com/|github\.com/)', ''),
                  '(\.git)?/*$', ''))
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
    op.create_check_constraint(
        "ck_agent_files_no_github_token",
        "agent_files",
        "key NOT IN ('GH_TOKEN', 'GITHUB_TOKEN')",
    )
    op.execute("DELETE FROM credential_requests WHERE kind IN ('repo', 'skill_repo')")
    op.drop_constraint("ck_github_connect_invitation_activation", "github_connect_invitations")
    op.drop_column("github_connect_invitations", "activation_status")
    op.drop_column("agent_github_grants", "staged")
    for table in (
        "agent_github_mode",
        "agent_github_binding",
        "agent_repo_binding",
        "agent_skill_repo_credentials",
        "github_credentials",
        "github_oauth_states",
    ):
        op.drop_table(table)


def downgrade() -> None:
    raise NotImplementedError("Retired GitHub keys cannot be restored")
