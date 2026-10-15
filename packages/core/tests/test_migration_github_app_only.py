"""Retiring saved keys preserves only live App grants and their working repo."""

from __future__ import annotations

import importlib.util
import io
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.adapters.cli.commands.github import preview_legacy_retirement
from daimon.core.github_retirement import accepted_loss_agent_ids
from daimon.core.ma_identity import derive_agent_uuid
from rich.console import Console
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession


async def _legacy_counts(session: AsyncSession) -> dict[str, int]:
    """Snapshot every legacy table plus rows the upgrade retires in place."""
    tables = (
        "agent_github_mode",
        "agent_github_binding",
        "agent_repo_binding",
        "agent_skill_repo_credentials",
        "github_credentials",
        "github_oauth_states",
        "agent_github_grants",
        "agent_files",
        "github_connect_invitations",
        "credential_requests",
    )
    return {
        table: int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
        for table in tables
    }


@pytest.mark.fresh_schema
async def test_retire_legacy_rows_and_keep_grants(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0105_github_app_only.py"
    spec = importlib.util.spec_from_file_location("migration_github_app_only", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    tenant = uuid.UUID("00000000-0000-4000-8000-000000000001")
    agent = derive_agent_uuid(tenant_id=tenant, ma_agent_id="agent-a")
    accepted_agent = uuid.UUID("00000000-0000-4000-8000-000000000002")
    await db_session.execute(
        text("INSERT INTO tenants (id, platform, external_id) VALUES (:tenant, 'discord', 'g')"),
        {"tenant": tenant},
    )
    account = uuid.uuid4()
    await db_session.execute(
        text("INSERT INTO accounts (id, tenant_id, role) VALUES (:account, :tenant, 'admin')"),
        {"account": account, "tenant": tenant},
    )
    for statement in (
        "CREATE TABLE agent_github_mode (tenant_id uuid, agent_id uuid, mode text)",
        "CREATE TABLE agent_github_binding (agent_id uuid, principal_id uuid)",
        "CREATE TABLE agent_repo_binding (tenant_id uuid, agent_id uuid, repo_url text, "
        "default_branch text, ma_secret_ref text)",
        "CREATE TABLE agent_skill_repo_credentials (tenant_id uuid, agent_id uuid, "
        "repo_url text, ma_secret_ref text)",
        "CREATE TABLE github_credentials (principal_id uuid)",
        "CREATE TABLE github_oauth_states (state uuid)",
    ):
        await db_session.execute(text(statement))
    # The current ORM schema has already retired these two columns. Restore
    # their old shape to exercise the real migration against populated rows.
    await db_session.execute(
        text("ALTER TABLE agent_github_grants ADD COLUMN staged boolean NOT NULL DEFAULT true")
    )
    await db_session.execute(
        text("ALTER TABLE github_connect_invitations ADD COLUMN activation_status text")
    )
    await db_session.execute(
        text(
            "ALTER TABLE github_connect_invitations ADD CONSTRAINT ck_github_connect_invitation_activation CHECK (activation_status IS NULL OR activation_status IN ('activated', 'update_pending'))"
        )
    )
    await db_session.execute(
        text("ALTER TABLE agent_files DROP CONSTRAINT ck_agent_files_no_github_token")
    )
    await db_session.execute(
        text("""
            INSERT INTO github_connect_invitations
                (token_hash, tenant_id, requester_account_id, workspace_label,
                 requester_label, expires_at, activation_status)
            VALUES ('pending-update', :tenant, :account, 'team', 'admin', now() + interval '1 hour',
                    'update_pending')
        """),
        {"tenant": tenant, "account": account},
    )
    params = {"tenant": tenant, "agent": agent}
    await db_session.execute(
        text("INSERT INTO agent_github_mode (tenant_id, agent_id) VALUES (:tenant, :agent)"),
        params,
    )
    await db_session.execute(
        text("UPDATE agent_github_mode SET mode='app' WHERE tenant_id=:tenant AND agent_id=:agent"),
        params,
    )
    await db_session.execute(
        text(
            "INSERT INTO agent_github_binding (agent_id, principal_id) VALUES (:agent, :principal)"
        ),
        {"agent": agent, "principal": account},
    )
    await db_session.execute(
        text("INSERT INTO github_credentials (principal_id) VALUES (:principal)"),
        {"principal": account},
    )
    await db_session.execute(
        text("INSERT INTO github_oauth_states (state) VALUES (:state)"),
        {"state": uuid.uuid4()},
    )
    await db_session.execute(
        text(
            "INSERT INTO tenant_github_repos (tenant_id, repo_id, owner_id, installation_id, repo_full_name, max_access, authorized_by_github_user_id) VALUES (:tenant, 101, 1, 1, 'team/app', 'write', 1)"
        ),
        params,
    )
    await db_session.execute(
        text(
            "INSERT INTO agent_github_grants (tenant_id, agent_id, repo_id, baseline_access, ceiling_access, staged) VALUES (:tenant, :agent, 101, 'read', 'read', false)"
        ),
        params,
    )
    await db_session.execute(
        text(
            "INSERT INTO agent_repo_binding (tenant_id, agent_id, repo_url, default_branch, ma_secret_ref) VALUES (:tenant, :agent, 'https://github.com/team/app.git', 'main', 'cred_legacy')"
        ),
        params,
    )
    await db_session.execute(
        text(
            "INSERT INTO agent_files (tenant_id, agent_id, key, content) VALUES (:tenant, :agent, 'GH_TOKEN', 'encrypted')"
        ),
        params,
    )
    await db_session.execute(
        text("""
            INSERT INTO agent_skill_repo_credentials
                (tenant_id, agent_id, repo_url, ma_secret_ref)
            VALUES (:tenant, :agent, 'https://github.com/team/app.git', 'cred_skill')
        """),
        params,
    )
    await db_session.execute(
        text("""
            INSERT INTO thread_sessions
                (tenant_id, platform, thread_id, ma_session_id, ma_agent_id, effective_config)
            VALUES (:tenant, 'discord', 'thread-app', 'session-app', 'agent-a',
                    '{"github_mode":"app"}'::jsonb)
        """),
        params,
    )
    await db_session.execute(
        text("""
            INSERT INTO github_issued_tokens
                (tenant_id, agent_id, session_id, installation_id, repo_ids,
                 permissions, grant_versions, expires_at, status)
            VALUES (:tenant, :agent, 'session-app', 1, ARRAY[101]::bigint[],
                    '{}'::jsonb, '{}'::jsonb, now() + interval '1 hour', 'delivered')
        """),
        params,
    )
    await db_session.execute(
        text("""
            INSERT INTO tenant_github_repos
                (tenant_id, repo_id, owner_id, installation_id, repo_full_name,
                 max_access, authorized_by_github_user_id)
            VALUES (:tenant, 102, 1, 1, 'example/widgets', 'write', 1)
        """),
        params,
    )
    await db_session.execute(
        text("""
            INSERT INTO agent_github_grants
                (tenant_id, agent_id, repo_id, baseline_access, ceiling_access, staged)
            VALUES (:tenant, :agent, 102, 'read', 'read', true)
        """),
        {"tenant": tenant, "agent": accepted_agent},
    )
    await db_session.execute(
        text("""
            INSERT INTO agent_repo_binding
                (tenant_id, agent_id, repo_url, default_branch, ma_secret_ref)
            VALUES (:tenant, :agent, 'https://github.com/example/widgets.git',
                    'main', 'cred_staged')
        """),
        {"tenant": tenant, "agent": accepted_agent},
    )
    tenant_b = uuid.UUID("11111111-0000-4000-8000-000000000003")
    agent_c = uuid.UUID("00000000-0000-4000-8000-000000000004")
    agent_d = uuid.UUID("00000000-0000-4000-8000-000000000005")
    agent_e = uuid.UUID("00000000-0000-4000-8000-000000000006")
    await db_session.execute(
        text("INSERT INTO tenants (id, platform, external_id) VALUES (:tenant, 'slack', 's')"),
        {"tenant": tenant_b},
    )
    await db_session.execute(
        text("""
            INSERT INTO tenant_github_repos
                (tenant_id, repo_id, owner_id, installation_id, repo_full_name,
                 max_access, authorized_by_github_user_id)
            VALUES (:tenant, 101, 1, 1, 'team/app', 'write', 1)
        """),
        {"tenant": tenant_b},
    )
    for peer_tenant, peer_agent in (
        (tenant_b, agent_c),
        (tenant, agent_d),
        (tenant, agent_e),
    ):
        peer = {"tenant": peer_tenant, "agent": peer_agent}
        await db_session.execute(
            text(
                "INSERT INTO agent_github_mode (tenant_id, agent_id, mode) "
                "VALUES (:tenant, :agent, 'app')"
            ),
            peer,
        )
        await db_session.execute(
            text("""
                INSERT INTO agent_github_grants
                    (tenant_id, agent_id, repo_id, baseline_access, ceiling_access, staged)
                VALUES (:tenant, :agent, 101, 'read', 'read', false)
            """),
            peer,
        )
        await db_session.execute(
            text("""
                INSERT INTO agent_repo_binding
                    (tenant_id, agent_id, repo_url, default_branch, ma_secret_ref)
                VALUES (:tenant, :agent, 'https://github.com/team/app.git',
                        'main', 'cred_leftover')
            """),
            peer,
        )
    monkeypatch.setenv("DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS", str(accepted_agent))
    report = io.StringIO()
    await preview_legacy_retirement(
        db_session, Console(file=report, force_terminal=False, width=1000)
    )
    lines = report.getvalue()
    assert f"DELETE staged grant tenant={tenant} agent={accepted_agent} repo_id=102" in lines
    assert f"staged grants tenant={tenant} agent={accepted_agent}: 1" in lines
    assert f"agent={agent} saved_key=True" in lines
    assert f"agent={accepted_agent} saved_key=False" in lines
    assert "live_app_grant_covers_working_repo=True" in lines
    assert "live_app_grant_covers_working_repo=False" in lines
    assert f"agent={accepted_agent}" in lines
    assert "accepted_loss=True expected_loss=example/widgets until reconnected" in lines
    for peer_agent in (agent_c, agent_d, agent_e):
        peer_line = next(line for line in lines.splitlines() if f"agent={peer_agent}" in line)
        assert "live_app_grant_covers_working_repo=True" in peer_line
        assert "app_mode=True" in peer_line

    assert "session_gh_token_source=app" in lines
    assert "app_token_sessions=1/1" in lines
    assert "skill_repo_coverage=1/1" in lines

    await db_session.execute(
        text("UPDATE github_issued_tokens SET status='revoked' WHERE session_id='session-app'")
    )
    with pytest.raises(RuntimeError, match="cannot verify another agent"):
        await preview_legacy_retirement(
            db_session, Console(file=io.StringIO(), force_terminal=False, width=1000)
        )
    await db_session.execute(
        text("UPDATE github_issued_tokens SET status='delivered' WHERE session_id='session-app'")
    )

    other_agent = uuid.uuid4()
    await db_session.execute(
        text("""
            INSERT INTO agent_repo_binding
                (tenant_id, agent_id, repo_url, default_branch, ma_secret_ref)
            VALUES (:tenant, :agent, 'https://github.com/team/other.git', 'main', 'cred_other')
        """),
        {"tenant": tenant, "agent": other_agent},
    )
    with pytest.raises(RuntimeError, match="cannot verify another agent"):
        await preview_legacy_retirement(
            db_session, Console(file=io.StringIO(), force_terminal=False, width=1000)
        )
    await db_session.execute(
        text("DELETE FROM agent_repo_binding WHERE agent_id=:agent"),
        {"agent": other_agent},
    )

    conn = await db_session.connection()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    before_rejected_upgrade = await _legacy_counts(db_session)
    assert all(
        before_rejected_upgrade[table] > 0
        for table in (
            "agent_github_mode",
            "agent_github_binding",
            "agent_repo_binding",
            "agent_skill_repo_credentials",
            "github_credentials",
            "github_oauth_states",
        )
    )
    monkeypatch.delenv("DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS")
    with pytest.raises(RuntimeError, match="retire unapproved access"):
        await conn.run_sync(upgrade)
    assert await _legacy_counts(db_session) == before_rejected_upgrade
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM agent_github_grants WHERE agent_id=:agent AND staged"),
            {"agent": accepted_agent},
        )
        == 1
    )
    monkeypatch.setenv("DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS", str(accepted_agent))
    await db_session.execute(
        text("UPDATE github_issued_tokens SET status='revoked' WHERE session_id='session-app'")
    )
    before_unlisted_loss = await _legacy_counts(db_session)
    with pytest.raises(RuntimeError, match="retire unapproved access"):
        await conn.run_sync(upgrade)
    assert await _legacy_counts(db_session) == before_unlisted_loss
    await db_session.execute(
        text("UPDATE github_issued_tokens SET status='delivered' WHERE session_id='session-app'")
    )
    await conn.run_sync(upgrade)
    grant = (
        await db_session.execute(
            text(
                "SELECT is_working_repo, version FROM agent_github_grants WHERE tenant_id = :tenant AND agent_id = :agent"
            ),
            params,
        )
    ).one()
    assert grant.is_working_repo is True
    assert grant.version == 2
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM github_issued_tokens "
                "WHERE tenant_id=:tenant AND agent_id=:agent AND status='delivered'"
            ),
            params,
        )
        == 1
    )
    for peer_tenant, peer_agent in ((tenant_b, agent_c), (tenant, agent_d), (tenant, agent_e)):
        assert (
            await db_session.execute(
                text(
                    "SELECT repo_id, is_working_repo FROM agent_github_grants "
                    "WHERE tenant_id=:tenant AND agent_id=:agent"
                ),
                {"tenant": peer_tenant, "agent": peer_agent},
            )
        ).one() == (101, True)
    assert (
        await db_session.scalar(
            text("SELECT count(*) FROM agent_github_grants WHERE agent_id=:agent"),
            {"agent": accepted_agent},
        )
        == 0
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM github_legacy_vault_cleanup WHERE credential_id = 'cred_legacy'"
            )
        )
        == 1
    )
    assert (
        await db_session.scalar(text("SELECT count(*) FROM agent_files WHERE key = 'GH_TOKEN'"))
        == 0
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT count(*) FROM github_connect_invitations WHERE token_hash='pending-update'"
            )
        )
        == 1
    )
    assert (
        await db_session.scalar(
            text("SELECT to_regclass(current_schema() || '.agent_repo_binding')")
        )
        is None
    )
    assert (
        await db_session.scalar(
            text("SELECT to_regclass(current_schema() || '.agent_github_grant_drafts')")
        )
        is not None
    )


def test_accepted_loss_env_parses_uuid_list(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_a = uuid.UUID("00000000-0000-4000-8000-000000000010")
    agent_b = uuid.UUID("00000000-0000-4000-8000-000000000011")
    monkeypatch.setenv("DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS", f"{agent_a}, {agent_b}")
    assert accepted_loss_agent_ids() == {agent_a, agent_b}
    monkeypatch.setenv("DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS", "invalid")
    with pytest.raises(ValueError, match="must contain only agent UUIDs"):
        accepted_loss_agent_ids()
