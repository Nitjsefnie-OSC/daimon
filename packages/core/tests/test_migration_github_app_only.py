"""Retiring saved keys preserves only live App grants and their working repo."""

from __future__ import annotations

import importlib.util
import io
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.adapters.cli.commands.github import preview_legacy_retirement
from daimon.core._models import AgentGitHubGrant
from daimon.core.github_retirement import RETIREMENT_LOSS_SQL, accepted_loss_agent_ids
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.github_access import (
    list_agent_grants,
    list_live_grant_repositories,
    stage_grant,
)
from daimon.core.stores.github_app_installations import upsert_github_app
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
@pytest.mark.parametrize("mode", [None, "legacy"])
@pytest.mark.parametrize("installation_issue", ["suspended", "repo_missing"])
async def test_retained_grant_gets_app_mode_before_installation_recovers(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    mode: str | None,
    installation_issue: str,
) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0105_github_app_only.py"
    spec = importlib.util.spec_from_file_location("migration_github_app_only", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.delenv("DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS", raising=False)

    tenant = uuid.UUID("00000000-0000-4000-8000-000000000031")
    agent = uuid.UUID("00000000-0000-4000-8000-000000000032")
    params = {"tenant": tenant, "agent": agent}
    await db_session.execute(
        text("INSERT INTO tenants (id, platform, external_id) VALUES (:tenant, 'discord', 'test')"),
        params,
    )
    for statement in (
        "CREATE TABLE agent_github_mode (tenant_id uuid, agent_id uuid, mode text, "
        "PRIMARY KEY (tenant_id, agent_id))",
        "CREATE TABLE agent_github_binding (agent_id uuid, principal_id uuid)",
        "CREATE TABLE agent_repo_binding (tenant_id uuid, agent_id uuid, repo_url text, "
        "default_branch text, ma_secret_ref text)",
        "CREATE TABLE agent_skill_repo_credentials (tenant_id uuid, agent_id uuid, "
        "repo_url text, ma_secret_ref text)",
        "CREATE TABLE github_credentials (principal_id uuid)",
        "CREATE TABLE github_oauth_states (state uuid)",
    ):
        await db_session.execute(text(statement))
    await db_session.execute(
        text("ALTER TABLE agent_github_grants ADD COLUMN staged boolean NOT NULL DEFAULT true")
    )
    await db_session.execute(text("ALTER TABLE agent_github_grants DROP COLUMN app_visible"))
    if mode is not None:
        await db_session.execute(
            text("INSERT INTO agent_github_mode VALUES (:tenant, :agent, :mode)"),
            {**params, "mode": mode},
        )
    await db_session.execute(
        text("""
            INSERT INTO github_app_installations
                (installation_id, account_login, repo_full_names, suspended_at)
            VALUES (11, 'example', :repos, :suspended)
        """),
        {
            "repos": ["example/other"]
            if installation_issue == "repo_missing"
            else ["example/widgets"],
            "suspended": datetime(2026, 1, 1, tzinfo=UTC)
            if installation_issue == "suspended"
            else None,
        },
    )
    await db_session.execute(
        text("""
            INSERT INTO tenant_github_repos
                (tenant_id, repo_id, owner_id, installation_id, repo_full_name,
                 max_access, authorized_by_github_user_id)
            VALUES (:tenant, 101, 1, 11, 'example/widgets', 'write', 1)
        """),
        params,
    )
    await db_session.execute(
        text("""
            INSERT INTO agent_github_grants
                (tenant_id, agent_id, repo_id, baseline_access, ceiling_access,
                 staged, is_working_repo)
            VALUES (:tenant, :agent, 101, 'read', 'read', false, true)
        """),
        params,
    )
    assert set((await db_session.execute(text(RETIREMENT_LOSS_SQL))).scalars()) == set()
    conn = await db_session.connection()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    await conn.run_sync(upgrade)
    assert (
        await db_session.execute(
            text("""
                SELECT staged, app_visible, is_working_repo FROM agent_github_grants
                WHERE tenant_id=:tenant AND agent_id=:agent AND repo_id=101
            """),
            params,
        )
    ).one() == (False, True, True)
    assert (
        await db_session.scalar(
            text("SELECT mode FROM agent_github_mode WHERE tenant_id=:tenant AND agent_id=:agent"),
            params,
        )
        == "app"
    )
    assert await list_live_grant_repositories(db_session, tenant_id=tenant, agent_id=agent) == []
    await upsert_github_app(
        db_session,
        installation_id=11,
        account_id=1,
        account_login="example",
        account_type="Organization",
        repository_selection="selected",
        suspended_at=None,
    )
    live = await list_live_grant_repositories(db_session, tenant_id=tenant, agent_id=agent)
    assert [
        (grant.repo_id, grant.is_working_repo, repo.repo_full_name) for grant, repo in live
    ] == [(101, True, "example/widgets")]
    # Replay d95cef53d's mode read and staged assignment after recovery.
    await db_session.execute(
        text("""
            UPDATE agent_github_grants g
            SET staged = COALESCE((SELECT m.mode FROM agent_github_mode m
                                   WHERE m.tenant_id=g.tenant_id AND m.agent_id=g.agent_id),
                                  'legacy') <> 'app'
            WHERE g.tenant_id=:tenant AND g.agent_id=:agent AND g.repo_id=101
        """),
        params,
    )
    assert (
        await db_session.execute(
            text("""
                SELECT staged, app_visible FROM agent_github_grants
                WHERE tenant_id=:tenant AND agent_id=:agent AND repo_id=101
            """),
            params,
        )
    ).one() == (False, True)


@pytest.mark.fresh_schema
@pytest.mark.parametrize(
    ("case", "expected_loss"),
    [
        ("agent_scope_revoked", True),
        ("agent_scope_suspended", True),
        ("installation_missing", True),
        ("installation_suspended", True),
        ("installation_repo_removed", True),
        ("duplicate_confirmations", False),
        ("stale_working_marker", False),
        ("git_suffix_before_slash", True),
        ("git_suffix_skill_repo", True),
    ],
)
async def test_retirement_uses_runtime_repo_resolution(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_loss: bool,
) -> None:
    path = Path(__file__).parents[1] / "alembic/versions/0105_github_app_only.py"
    spec = importlib.util.spec_from_file_location("migration_github_app_only", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.delenv("DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS", raising=False)

    tenant = uuid.UUID("00000000-0000-4000-8000-000000000021")
    agent = uuid.UUID("00000000-0000-4000-8000-000000000022")
    params = {"tenant": tenant, "agent": agent}
    await db_session.execute(
        text("INSERT INTO tenants (id, platform, external_id) VALUES (:tenant, 'discord', 'test')"),
        params,
    )
    for statement in (
        "CREATE TABLE agent_github_mode (tenant_id uuid, agent_id uuid, mode text, "
        "PRIMARY KEY (tenant_id, agent_id))",
        "CREATE TABLE agent_github_binding (agent_id uuid, principal_id uuid)",
        "CREATE TABLE agent_repo_binding (tenant_id uuid, agent_id uuid, repo_url text, "
        "default_branch text, ma_secret_ref text)",
        "CREATE TABLE agent_skill_repo_credentials (tenant_id uuid, agent_id uuid, "
        "repo_url text, ma_secret_ref text)",
        "CREATE TABLE github_credentials (principal_id uuid)",
        "CREATE TABLE github_oauth_states (state uuid)",
    ):
        await db_session.execute(text(statement))
    await db_session.execute(
        text("ALTER TABLE agent_github_grants ADD COLUMN staged boolean NOT NULL DEFAULT true")
    )
    await db_session.execute(text("ALTER TABLE agent_github_grants DROP COLUMN app_visible"))
    await db_session.execute(
        text("INSERT INTO agent_github_mode VALUES (:tenant, :agent, 'app')"), params
    )
    repo_url = (
        "https://github.com/example/widgets.git/"
        if case == "git_suffix_before_slash"
        else "https://github.com/example/widgets.git"
    )
    await db_session.execute(
        text("INSERT INTO agent_repo_binding VALUES (:tenant, :agent, :url, 'main', 'cred_test')"),
        {**params, "url": repo_url},
    )
    if case != "installation_missing":
        await db_session.execute(
            text("""
                INSERT INTO github_app_installations
                    (installation_id, account_login, repo_full_names, suspended_at)
                VALUES (11, 'example', :repos, :suspended)
            """),
            {
                "repos": ["example/other"]
                if case == "installation_repo_removed"
                else ["example/widgets"],
                "suspended": datetime(2026, 1, 1, tzinfo=UTC)
                if case == "installation_suspended"
                else None,
            },
        )
    await db_session.execute(
        text("""
            INSERT INTO tenant_github_repos
                (tenant_id, repo_id, owner_id, installation_id, repo_full_name,
                 max_access, authorized_by_github_user_id)
            VALUES (:tenant, 101, 1, 11, 'example/widgets', 'write', 1)
        """),
        params,
    )
    if case in {"agent_scope_revoked", "agent_scope_suspended", "duplicate_confirmations"}:
        await db_session.execute(
            text("""
                INSERT INTO tenant_github_repos
                    (tenant_id, repo_id, scope_agent_id, owner_id, installation_id,
                     repo_full_name, max_access, authorized_by_github_user_id, status)
                VALUES (:tenant, 101, :agent, 1, 11, 'example/widgets', 'write', 1, :status)
            """),
            {
                **params,
                "status": "revoked"
                if case == "agent_scope_revoked"
                else "suspended"
                if case == "agent_scope_suspended"
                else "active",
            },
        )
    if case == "git_suffix_skill_repo":
        await db_session.execute(
            text("""
                INSERT INTO agent_skill_repo_credentials VALUES
                    (:tenant, :agent, 'https://github.com/example/widgets.git/', 'cred_skill')
            """),
            params,
        )
    await db_session.execute(
        text("""
            INSERT INTO agent_github_grants
                (tenant_id, agent_id, repo_id, baseline_access, ceiling_access, staged)
            VALUES (:tenant, :agent, 101, 'read', 'read', false)
        """),
        params,
    )
    if case == "stale_working_marker":
        await db_session.execute(
            text("""
                INSERT INTO tenant_github_repos
                    (tenant_id, repo_id, owner_id, installation_id, repo_full_name,
                     max_access, authorized_by_github_user_id, status)
                VALUES (:tenant, 102, 1, 11, 'example/old', 'write', 1, 'revoked')
            """),
            params,
        )
        await db_session.execute(
            text("""
                INSERT INTO agent_github_grants
                    (tenant_id, agent_id, repo_id, baseline_access, ceiling_access,
                     staged, is_working_repo)
                VALUES (:tenant, :agent, 102, 'read', 'read', false, true)
            """),
            params,
        )
    loss_ids = set((await db_session.execute(text(RETIREMENT_LOSS_SQL))).scalars())
    assert (agent in loss_ids) is expected_loss
    report = io.StringIO()
    if expected_loss:
        with pytest.raises(RuntimeError, match="cannot verify another agent"):
            await preview_legacy_retirement(
                db_session, Console(file=report, force_terminal=False, width=1000)
            )
    else:
        await preview_legacy_retirement(
            db_session, Console(file=report, force_terminal=False, width=1000)
        )
    preview_line = next(
        line for line in report.getvalue().splitlines() if line.startswith("RETIRE ")
    )
    assert f"loss={expected_loss}" in preview_line
    conn = await db_session.connection()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    if expected_loss:
        before = await _legacy_counts(db_session)
        with pytest.raises(RuntimeError, match="retire unapproved access"):
            await conn.run_sync(upgrade)
        assert await _legacy_counts(db_session) == before
    else:
        await conn.run_sync(upgrade)
        assert (
            await db_session.scalar(
                text("""
                    SELECT is_working_repo FROM agent_github_grants
                    WHERE tenant_id=:tenant AND agent_id=:agent AND repo_id=101
                """),
                params,
            )
            is True
        )
        if case == "stale_working_marker":
            assert (
                await db_session.scalar(
                    text("""
                        SELECT is_working_repo FROM agent_github_grants
                        WHERE tenant_id=:tenant AND agent_id=:agent AND repo_id=102
                    """),
                    params,
                )
                is False
            )


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
        "CREATE TABLE agent_github_mode (tenant_id uuid, agent_id uuid, mode text, "
        "PRIMARY KEY (tenant_id, agent_id))",
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
    await db_session.execute(text("ALTER TABLE agent_github_grants DROP COLUMN app_visible"))
    await db_session.execute(
        text("ALTER TABLE github_connect_invitations ADD COLUMN activation_status text")
    )
    await db_session.execute(
        text(
            "ALTER TABLE github_connect_invitations ADD CONSTRAINT ck_github_connect_invitation_activation CHECK (activation_status IS NULL OR activation_status IN ('activated', 'update_pending'))"
        )
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
        text("""
            INSERT INTO github_app_installations
                (installation_id, account_login, repo_full_names)
            VALUES (1, 'team', ARRAY['team/app', 'example/widgets'])
        """)
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
                    '{"ma_agent_id":"agent-a"}'::jsonb)
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
    agent_f = uuid.UUID("00000000-0000-4000-8000-000000000007")
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
    # A live grant can exist without a legacy mode row. Phase 1 must add one
    # so an old worker does not re-stage that grant during the rollout.
    await db_session.execute(
        text("""
            INSERT INTO agent_github_grants
                (tenant_id, agent_id, repo_id, baseline_access, ceiling_access, staged)
            VALUES (:tenant, :agent, 101, 'read', 'read', false)
        """),
        {"tenant": tenant, "agent": agent_f},
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

    assert f"agent={agent} " in lines
    assert "loss=False" in next(line for line in lines.splitlines() if f"agent={agent} " in line)
    assert "skill_repo_coverage=1/1" in lines

    # The deployed-style snapshot lacks github_mode, but grants still cover it.
    assert set((await db_session.execute(text(RETIREMENT_LOSS_SQL))).scalars()) == {accepted_agent}
    await preview_legacy_retirement(
        db_session, Console(file=io.StringIO(), force_terminal=False, width=1000)
    )
    # The same live grants can mint a new token after the delivered one expires.
    await db_session.execute(
        text(
            "UPDATE github_issued_tokens SET expires_at=now() - interval '1 minute' "
            "WHERE session_id='session-app'"
        )
    )
    assert set((await db_session.execute(text(RETIREMENT_LOSS_SQL))).scalars()) == {accepted_agent}
    await preview_legacy_retirement(
        db_session, Console(file=io.StringIO(), force_terminal=False, width=1000)
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
    assert set((await db_session.execute(text(RETIREMENT_LOSS_SQL))).scalars()) == {
        accepted_agent,
        other_agent,
    }
    rejected_report = io.StringIO()
    with pytest.raises(RuntimeError, match="cannot verify another agent"):
        await preview_legacy_retirement(
            db_session, Console(file=rejected_report, force_terminal=False, width=1000)
        )
    preview_loss = {
        uuid.UUID(line.split("agent=", 1)[1].split(" ", 1)[0])
        for line in rejected_report.getvalue().splitlines()
        if line.startswith("RETIRE ") and "loss=True" in line
    }
    assert preview_loss == set((await db_session.execute(text(RETIREMENT_LOSS_SQL))).scalars())

    conn = await db_session.connection()

    def upgrade(sync_conn: Connection) -> None:
        with Operations.context(MigrationContext.configure(sync_conn)):
            migration.upgrade()

    before_working_loss = await _legacy_counts(db_session)
    with pytest.raises(RuntimeError, match="retire unapproved access"):
        await conn.run_sync(upgrade)
    assert await _legacy_counts(db_session) == before_working_loss
    await db_session.execute(
        text("DELETE FROM agent_repo_binding WHERE agent_id=:agent"),
        {"agent": other_agent},
    )

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
    # A private skill repo without a matching live grant also blocks every write.
    await db_session.execute(
        text("""
        INSERT INTO agent_skill_repo_credentials
            (tenant_id, agent_id, repo_url, ma_secret_ref)
        VALUES (:tenant, :agent, 'https://github.com/team/private.git', 'cred_private')
    """),
        params,
    )
    assert set((await db_session.execute(text(RETIREMENT_LOSS_SQL))).scalars()) == {
        accepted_agent,
        agent,
    }
    skill_report = io.StringIO()
    with pytest.raises(RuntimeError, match="cannot verify another agent"):
        await preview_legacy_retirement(
            db_session, Console(file=skill_report, force_terminal=False, width=1000)
        )
    assert f"agent={agent} " in skill_report.getvalue()
    assert "loss=True" in next(
        line for line in skill_report.getvalue().splitlines() if f"agent={agent} " in line
    )
    before_skill_loss = await _legacy_counts(db_session)
    monkeypatch.setenv("DAIMON_GITHUB_APP_ONLY_ACCEPTED_LOSS", str(accepted_agent))
    with pytest.raises(RuntimeError, match="retire unapproved access"):
        await conn.run_sync(upgrade)
    assert await _legacy_counts(db_session) == before_skill_loss
    await db_session.execute(
        text("DELETE FROM agent_skill_repo_credentials WHERE ma_secret_ref='cred_private'")
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
    # Expand phase: old code can still read every legacy table and column.
    for table in (
        "agent_github_mode",
        "agent_github_binding",
        "agent_repo_binding",
        "agent_skill_repo_credentials",
        "github_credentials",
        "github_oauth_states",
    ):
        assert (
            await db_session.scalar(text("SELECT to_regclass(:table)"), {"table": table})
            is not None
        )
        if table != "agent_github_mode":
            assert await db_session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    assert (
        await db_session.scalar(
            text("SELECT mode FROM agent_github_mode WHERE tenant_id=:tenant AND agent_id=:agent"),
            {"tenant": tenant, "agent": agent_f},
        )
        == "app"
    )
    # Replay d95cef53d's mode-dependent staged assignment on an old worker.
    await db_session.execute(
        text("""
            UPDATE agent_github_grants g
            SET staged = COALESCE((SELECT m.mode FROM agent_github_mode m
                                   WHERE m.tenant_id=g.tenant_id AND m.agent_id=g.agent_id),
                                  'legacy') <> 'app'
            WHERE g.tenant_id=:tenant AND g.agent_id=:agent AND g.repo_id=101
        """),
        {"tenant": tenant, "agent": agent_f},
    )
    assert (
        await db_session.execute(
            text("""
                SELECT staged, app_visible FROM agent_github_grants
                WHERE tenant_id=:tenant AND agent_id=:agent AND repo_id=101
            """),
            {"tenant": tenant, "agent": agent_f},
        )
    ).one() == (False, True)
    # This agent had no grant or mode row when 0105 ran. New code creates its
    # grant during the overlap, then an old worker writes the same grant.
    late_agent = uuid.UUID("00000000-0000-4000-8000-000000000008")
    late_params = {"tenant": tenant, "agent": late_agent}
    assert (
        await db_session.scalar(
            text("SELECT mode FROM agent_github_mode WHERE tenant_id=:tenant AND agent_id=:agent"),
            late_params,
        )
        is None
    )
    await stage_grant(
        db_session,
        tenant_id=tenant,
        agent_id=late_agent,
        repo_id=101,
        baseline_access="read",
        ceiling_access="read",
        granted_by_account_id=None,
        is_working_repo=True,
    )
    assert (
        await db_session.scalar(
            text("SELECT mode FROM agent_github_mode WHERE tenant_id=:tenant AND agent_id=:agent"),
            late_params,
        )
        == "app"
    )
    await db_session.execute(
        text("""
            UPDATE agent_github_grants g
            SET staged = COALESCE((SELECT m.mode FROM agent_github_mode m
                                   WHERE m.tenant_id=g.tenant_id AND m.agent_id=g.agent_id),
                                  'legacy') <> 'app'
            WHERE g.tenant_id=:tenant AND g.agent_id=:agent AND g.repo_id=101
        """),
        late_params,
    )
    assert (
        await db_session.execute(
            text("""
                SELECT staged, app_visible, is_working_repo FROM agent_github_grants
                WHERE tenant_id=:tenant AND agent_id=:agent AND repo_id=101
            """),
            late_params,
        )
    ).one() == (False, True, True)
    assert (
        await db_session.scalar(
            text(
                "SELECT staged FROM agent_github_grants WHERE tenant_id=:tenant AND agent_id=:agent"
            ),
            params,
        )
    ) is False
    assert (
        await db_session.scalar(
            text(
                "SELECT activation_status FROM github_connect_invitations WHERE token_hash='pending-update'"
            )
        )
    ) == "update_pending"
    await db_session.execute(
        text("""
        INSERT INTO github_connect_invitations
            (token_hash, tenant_id, requester_account_id, workspace_label,
             requester_label, expires_at, activation_status)
        VALUES ('late-old-invite', :tenant, :account, 'team', 'admin',
                now() + interval '1 hour', 'activated')
    """),
        {"tenant": tenant, "account": account},
    )
    assert (
        await db_session.scalar(
            text(
                "SELECT activation_status FROM github_connect_invitations "
                "WHERE token_hash='late-old-invite'"
            )
        )
    ) == "activated"

    # Old code may still write rows during the rollout; new grant readers ignore
    # its staged rows, while new ORM inserts omit the retained staged column.
    await db_session.execute(
        text("""
        INSERT INTO agent_github_grants
            (tenant_id, agent_id, repo_id, baseline_access, ceiling_access, staged)
        VALUES (:tenant, :agent, 101, 'read', 'read', true)
    """),
        {"tenant": tenant, "agent": accepted_agent},
    )
    assert (
        await db_session.scalar(
            text("""
        SELECT app_visible FROM agent_github_grants
        WHERE tenant_id=:tenant AND agent_id=:agent AND repo_id=101
    """),
            {"tenant": tenant, "agent": accepted_agent},
        )
    ) is False
    assert await list_agent_grants(db_session, tenant_id=tenant, agent_id=accepted_agent) == []
    await stage_grant(
        db_session,
        tenant_id=tenant,
        agent_id=accepted_agent,
        repo_id=101,
        baseline_access="read",
        ceiling_access="read",
        granted_by_account_id=None,
    )
    assert (
        await db_session.execute(
            text("""
                SELECT staged, app_visible FROM agent_github_grants
                WHERE tenant_id=:tenant AND agent_id=:agent AND repo_id=101
            """),
            {"tenant": tenant, "agent": accepted_agent},
        )
    ).one() == (False, True)
    db_session.add(
        AgentGitHubGrant(
            tenant_id=tenant,
            agent_id=agent,
            repo_id=102,
            baseline_access="read",
            ceiling_access="read",
        )
    )
    await db_session.flush()
    assert (
        await db_session.execute(
            text("""
        SELECT staged, app_visible FROM agent_github_grants
        WHERE tenant_id=:tenant AND agent_id=:agent AND repo_id=102
    """),
            params,
        )
    ).one() == (False, True)
    await db_session.execute(
        text("""
        INSERT INTO agent_files (tenant_id, agent_id, key, content)
        VALUES (:tenant, :agent, 'GH_TOKEN', 'late-legacy-value')
    """),
        {"tenant": tenant, "agent": accepted_agent},
    )
    await db_session.execute(
        text("""
        INSERT INTO agent_repo_binding
            (tenant_id, agent_id, repo_url, default_branch, ma_secret_ref)
        VALUES (:tenant, :agent, 'https://github.com/example/widgets.git',
                'main', 'late-legacy-ref')
    """),
        {"tenant": tenant, "agent": accepted_agent},
    )
    # The future contract migration can reuse the same exact-UUID guard for
    # rows an old worker recreated after phase 1.
    assert accepted_agent in set((await db_session.execute(text(RETIREMENT_LOSS_SQL))).scalars())
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
