"""Linked-grant journey with synthetic GitHub responses."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.oauth_github_personal import build_personal_link_routes
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import github_connect as connect_tool
from daimon.adapters.mcp.tools import github_linked_grant as linked
from daimon.adapters.mcp.tools import github_linked_pending as linked_pending
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    GithubAppSettings,
    HubSettings,
    McpSettings,
    Settings,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.accounts import get_account, set_role
from daimon.core.stores.domain import Role
from daimon.core.stores.github_access import list_agent_grants, list_authorized_repos
from daimon.core.stores.github_app_installations import get as get_installation
from daimon.core.stores.github_installation_reconciliation import get as get_reconciliation
from daimon.core.stores.github_linked_pending import take as take_pending
from daimon.core.stores.github_links import get_account_link, save_verified_link, unlink_account
from daimon.core.stores.github_owner_grants import exclude_repo, list_owner_grants
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route


@pytest.fixture
def github_http(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(
        permission="push",
        repo_visible=True,
        refresh_fail=False,
        installation_suspended=False,
    )
    original_client = httpx.AsyncClient

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(
                200,
                json={"error": "bad_refresh_token"}
                if state.refresh_fail
                else {
                    "access_token": "rotated",
                    "refresh_token": "new-refresh",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 100000,
                },
            )
        permissions = {
            key: key == state.permission for key in ("admin", "maintain", "push", "triage", "pull")
        }
        if request.url.path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "installations": [
                        {
                            "id": 201,
                            "account": {"id": 12, "login": "acme", "type": "Organization"},
                            "repository_selection": "selected",
                        }
                    ]
                },
            )
        if request.url.path == "/app/installations/201":
            return httpx.Response(
                200,
                json={
                    "id": 201,
                    "account": {"id": 12, "login": "acme", "type": "Organization"},
                    "repository_selection": "selected",
                    "suspended_at": (
                        "2026-10-16T00:00:00Z" if state.installation_suspended else None
                    ),
                    "permissions": {
                        "metadata": "read",
                        "contents": "write",
                        "issues": "write",
                        "pull_requests": "write",
                    },
                },
            )
        if request.url.path.endswith("/repositories"):
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": 101,
                            "full_name": "acme/widgets",
                            "owner": {"id": 12, "login": "acme"},
                            "permissions": permissions,
                        }
                    ]
                    if state.repo_visible
                    else []
                },
            )
        if request.url.path == "/repos/acme/widgets":
            if not state.repo_visible:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "id": 101,
                    "full_name": "acme/widgets",
                    "owner": {"id": 12},
                    "permissions": permissions,
                },
            )
        raise AssertionError(request.url)

    monkeypatch.setattr(
        linked.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(linked, "build_app_jwt", lambda *_args, **_kwargs: "app-jwt")
    state.original_client = original_client
    state.respond = respond
    return state


@pytest_asyncio.fixture
async def setup_linked(committing_sessionmaker: async_sessionmaker[AsyncSession]):
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session, workspace_id=f"acme-{uuid.uuid4().hex[:8]}")
        agent_id = derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ma_agent")
        account = await make_account(session, tenant=tenant)
        await set_role(session, account.id, Role.ADMIN)
        await make_platform_principal(
            session, platform="discord", external_id="person", tenant=tenant, account=account
        )
        await session.execute(
            text(
                "INSERT INTO github_app_installations "
                "(installation_id, account_login, app, account_id, account_type, "
                "repository_selection, repo_full_names) VALUES "
                "(201, 'acme', 'github_app', 12, 'Organization', 'selected', "
                "ARRAY['acme/base', 'acme/widgets'])"
            )
        )
        await session.execute(
            text(
                "INSERT INTO tenant_github_repos "
                "(tenant_id, repo_id, scope_agent_id, owner_id, installation_id, "
                "repo_full_name, max_access, authorized_by_github_user_id, "
                "authorized_by_account_id, status) VALUES "
                "(:tenant_id, 100, :agent_id, 12, 201, 'acme/base', 'read', 300, "
                ":account_id, 'active')"
            ),
            {"tenant_id": tenant.id, "agent_id": agent_id, "account_id": account.id},
        )
        await save_verified_link(
            session,
            intent_account_id=account.id,
            platform="discord",
            platform_user_id="person",
            github_user_id=300,
            login="person",
            access_token="synthetic",
            refresh_token="synthetic-refresh",
            expires_in=3600,
            refresh_expires_in=86400,
            fernet=fernet,
        )
        assert await get_account_link(session, account_id=account.id) is not None

    async def manages(session: AsyncSession) -> bool:
        current = await get_account(session, account.id)
        return current is not None and current.role == Role.ADMIN

    async def grant(*, account_id: uuid.UUID | None = None):
        return await linked.linked_grant(
            committing_sessionmaker,
            tenant_id=tenant.id,
            account_id=account_id or account.id,
            agent_id=agent_id,
            agent_name="Agent",
            repo_name="acme/widgets",
            fernet=fernet,
            client_id="synthetic",
            client_secret="synthetic",
            app_id="42",
            private_key="synthetic",
            manages_now=manages,
        )

    return SimpleNamespace(
        sessionmaker=committing_sessionmaker,
        tenant=tenant,
        account=account,
        agent_id=agent_id,
        grant=grant,
        fernet=fernet,
    )


@pytest.mark.asyncio
async def test_tool_uses_verified_turn_account_and_private_link(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = McpRuntime(
        session_factory=setup_linked.sessionmaker,
        client=AsyncMock(),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
            anthropic=AnthropicSettings(api_key=SecretStr("test")),
            mcp=McpSettings(public_url=HttpUrl("https://mcp.test/mcp")),
            github_app=GithubAppSettings(
                app_id="42",
                app_slug="sample-app",
                private_key=SecretStr("pem"),
                client_id="client",
                client_secret=SecretStr("secret"),
            ),
        ),
        deployment_default=DeploymentDefault(),
        fernet=setup_linked.fernet,
    )
    auth = AuthIdentity(
        account_id=setup_linked.account.id,
        tenant_id=setup_linked.tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id=setup_linked.tenant.external_id,
        platform_user_id="person",
        is_admin=True,
    )
    origin = SimpleNamespace(
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="123",
        thread_id="456",
    )
    monkeypatch.setattr(connect_tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(
        connect_tool,
        "resolve_setup_agent",
        AsyncMock(
            return_value=SimpleNamespace(
                id="ma_agent",
                name="Agent",
                metadata={},
            )
        ),
    )
    post_link = AsyncMock()
    monkeypatch.setattr(connect_tool, "_post_personal_link", post_link)
    github_http.permission = "pull"
    result = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/widgets"
    )
    assert result.status == "granted_read_only"
    assert "read and change" not in result.message
    post_link.assert_not_awaited()
    async with setup_linked.sessionmaker() as session:
        repo = next(
            row
            for row in await list_authorized_repos(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
            if row.repo_id == 101
        )
        [grant] = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert (repo.max_access, grant.ceiling_access) == ("read", "read")
    github_http.permission = "push"
    upgraded = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/widgets", access="write"
    )
    assert upgraded.status == "granted"
    assert "read and change" in upgraded.message
    async with setup_linked.sessionmaker() as session:
        repo = next(
            row
            for row in await list_authorized_repos(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
            if row.repo_id == 101
        )
        [grant] = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert (repo.max_access, grant.ceiling_access) == ("write", "write")
    github_http.permission = "pull"
    limited = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/widgets", access="write"
    )
    assert limited.status == "granted_read_only"
    assert "read and change" not in limited.message
    async with setup_linked.sessionmaker() as session:
        repo = next(
            row
            for row in await list_authorized_repos(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
            if row.repo_id == 101
        )
        [grant] = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert (repo.max_access, grant.ceiling_access) == ("read", "read")
    result = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/new"
    )
    assert result.status == "needs_install"
    assert "https://" not in result.message
    assert post_link.await_args.kwargs["label"] == "Open GitHub setup"
    post_link.reset_mock()
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE github_user_links SET status = 'broken' WHERE github_user_id = 300")
        )
    result = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/new"
    )
    assert result.status == "relink"
    post_link.assert_awaited_once()
    assert "/oauth/github/link/" in post_link.await_args.kwargs["url"]
    post_link.reset_mock()
    async with setup_linked.sessionmaker.begin() as session:
        await unlink_account(session, account_id=setup_linked.account.id)
    result = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/new"
    )
    assert result.status == "needs_link"
    assert "oauth/github/link" not in result.message
    post_link.assert_awaited_once()
    assert "/oauth/github/link/" in post_link.await_args.kwargs["url"]


@pytest.mark.asyncio
async def test_linked_manager_grants_without_browser(setup_linked, github_http) -> None:
    result = await setup_linked.grant()
    assert result.status == "granted"
    async with setup_linked.sessionmaker() as session:
        [grant] = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert (grant.repo_id, grant.ceiling_access) == (101, "write")
        repo = next(
            r
            for r in await list_authorized_repos(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
            if r.repo_id == 101
        )
        assert (
            repo.scope_agent_id,
            repo.authorized_by_github_user_id,
            repo.authorized_permission,
        ) == (setup_linked.agent_id, 300, "push")
    assert (await setup_linked.grant()).status == "granted"
    async with setup_linked.sessionmaker() as session:
        assert (
            len(
                await list_agent_grants(
                    session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
                )
            )
            == 1
        )
        reconciliation = await get_reconciliation(session, installation_id=201)
        assert reconciliation is not None
        assert (reconciliation.state, reconciliation.generation) == ("pending", 2)


@pytest.mark.asyncio
async def test_explicit_single_grant_clears_owner_exclusion(setup_linked, github_http) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await exclude_repo(
            session,
            tenant_id=setup_linked.tenant.id,
            agent_id=setup_linked.agent_id,
            repo_full_name="acme/widgets",
        )
    assert (await setup_linked.grant()).status == "granted"
    async with setup_linked.sessionmaker() as session:
        excluded = await session.scalar(
            text(
                "SELECT count(*) FROM agent_github_owner_exclusions WHERE tenant_id = :tenant "
                "AND agent_id = :agent AND repo_full_name = 'acme/widgets'"
            ),
            {"tenant": setup_linked.tenant.id, "agent": setup_linked.agent_id},
        )
        assert excluded == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("repo_name", ["acme/widgets", "acme/*"])
async def test_relink_during_github_lookup_rejects_old_token_evidence(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch, repo_name: str
) -> None:
    if repo_name.endswith("/*"):
        async with setup_linked.sessionmaker.begin() as session:
            await session.execute(
                text(
                    "UPDATE github_app_installations SET repo_full_names = "
                    "ARRAY['acme/widgets'] WHERE installation_id = 201"
                )
            )
    original_installations = linked._installations

    async def relink_during_lookup(client: httpx.AsyncClient, token: str):
        async with setup_linked.sessionmaker.begin() as session:
            await save_verified_link(
                session,
                intent_account_id=setup_linked.account.id,
                platform="discord",
                platform_user_id="person",
                github_user_id=300,
                login="person",
                access_token="new-token",
                refresh_token="new-refresh",
                expires_in=3600,
                refresh_expires_in=86400,
                fernet=setup_linked.fernet,
            )
        return await original_installations(client, token)

    monkeypatch.setattr(linked, "_installations", relink_during_lookup)

    async def manages(session: AsyncSession) -> bool:
        account = await get_account(session, setup_linked.account.id)
        return account is not None and account.role == Role.ADMIN

    result = await linked.linked_grant(
        setup_linked.sessionmaker,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        agent_id=setup_linked.agent_id,
        agent_name="Agent",
        repo_name=repo_name,
        fernet=setup_linked.fernet,
        client_id="synthetic",
        client_secret="synthetic",
        app_id="42",
        private_key="synthetic",
        manages_now=manages,
    )
    assert result.status == "changed_try_again"
    async with setup_linked.sessionmaker() as session:
        assert not await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert not await list_owner_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )


@pytest.mark.asyncio
async def test_owner_first_use_associates_existing_installation(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(text("DELETE FROM tenant_github_repos WHERE installation_id = 201"))
        await session.execute(
            text("DELETE FROM github_app_installations WHERE installation_id = 201")
        )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/201/access_tokens":
            return httpx.Response(201, json={"token": "listing-token"})
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "repositories": [{"full_name": "acme/widgets"}],
                },
            )
        return github_http.respond(request)

    monkeypatch.setattr(
        linked.httpx,
        "AsyncClient",
        lambda **kwargs: github_http.original_client(transport=httpx.MockTransport(respond)),
    )

    async def manages(session: AsyncSession) -> bool:
        account = await get_account(session, setup_linked.account.id)
        return account is not None and account.role == Role.ADMIN

    result = await linked.linked_grant(
        setup_linked.sessionmaker,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        agent_id=setup_linked.agent_id,
        agent_name="Agent",
        repo_name="acme/*",
        fernet=setup_linked.fernet,
        client_id="synthetic",
        client_secret="synthetic",
        app_id="42",
        private_key="synthetic",
        manages_now=manages,
    )
    assert result.status == "granted_read_only"
    async with setup_linked.sessionmaker() as session:
        assert await get_installation(session, installation_id=201) is not None
        assert [
            grant.owner_login
            for grant in await list_owner_grants(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
        ] == ["acme"]
        assert await get_reconciliation(session, installation_id=201) is not None


@pytest.mark.asyncio
async def test_resume_result_posts_to_originating_slack_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    monkeypatch.setattr(linked_pending, "slack_web_client", AsyncMock(return_value=client))
    pending = SimpleNamespace(
        platform="slack",
        workspace_id="team",
        parent_channel_id="channel",
        thread_id="thread",
        platform_user_id="person",
    )
    await linked_pending._deliver_result(SimpleNamespace(), pending, "Grant completed.")
    client.chat_postMessage.assert_awaited_once_with(
        channel="channel", thread_ts="thread", text="Grant completed."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_type,login,second_permission,access,expected",
    [
        ("User", "acme", "none", "read", "granted_read_only"),
        ("Organization", "person", "pull", "read", "granted_read_only"),
        ("Organization", "person", "none", "read", "no_github_access"),
        ("Organization", "person", "pull", "write", "no_github_access"),
        ("Organization", "person", "push", "write", "granted"),
    ],
)
async def test_owner_grant_requires_full_owner_authority(
    setup_linked,
    github_http,
    monkeypatch: pytest.MonkeyPatch,
    owner_type: str,
    login: str,
    second_permission: str,
    access: str,
    expected: str,
) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text("UPDATE github_user_links SET login = :login WHERE github_user_id = 300"),
            {"login": login},
        )
    original = github_http.original_client

    def respond(request: httpx.Request) -> httpx.Response:
        response = github_http.respond(request)
        if request.url.path == "/user/installations":
            body = response.json()
            body["installations"][0]["account"]["type"] = owner_type
            return httpx.Response(200, json=body)
        if request.url.path.endswith("/repositories"):
            body = response.json()
            permissions = {
                key: key == second_permission
                for key in ("admin", "maintain", "push", "triage", "pull")
            }
            body["repositories"].append(
                {
                    "id": 100,
                    "full_name": "acme/base",
                    "owner": {"id": 12, "login": "acme"},
                    "permissions": permissions,
                }
            )
            return httpx.Response(200, json=body)
        return response

    monkeypatch.setattr(
        linked.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond)),
    )

    async def manages(session: AsyncSession) -> bool:
        account = await get_account(session, setup_linked.account.id)
        return account is not None and account.role == Role.ADMIN

    result = await linked.linked_grant(
        setup_linked.sessionmaker,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        agent_id=setup_linked.agent_id,
        agent_name="Agent",
        repo_name="acme/*",
        fernet=setup_linked.fernet,
        client_id="synthetic",
        client_secret="synthetic",
        app_id="42",
        private_key="synthetic",
        manages_now=manages,
        requested_access=access,
    )
    assert result.status == expected
    if expected == "no_github_access":
        assert result.message == "You need access to every acme repo the GitHub App covers."
    async with setup_linked.sessionmaker() as session:
        grants = await list_owner_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert [grant.access for grant in grants] == (
            [access] if expected.startswith("granted") else []
        )


@pytest.mark.asyncio
async def test_owner_grant_manager_revoked_before_commit(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE github_app_installations SET repo_full_names = ARRAY['acme/widgets'] WHERE installation_id = 201"
            )
        )

    async def revoke() -> None:
        async with setup_linked.sessionmaker.begin() as session:
            await set_role(session, setup_linked.account.id, Role.USER)

    monkeypatch.setattr(linked, "_before_commit", revoke)

    async def manages(session: AsyncSession) -> bool:
        account = await get_account(session, setup_linked.account.id)
        return account is not None and account.role == Role.ADMIN

    result = await linked.linked_grant(
        setup_linked.sessionmaker,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        agent_id=setup_linked.agent_id,
        agent_name="Agent",
        repo_name="acme/*",
        fernet=setup_linked.fernet,
        client_id="synthetic",
        client_secret="synthetic",
        app_id="42",
        private_key="synthetic",
        manages_now=manages,
    )
    assert result.status == "changed_try_again"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permission,status,access",
    [
        ("admin", "granted", "write"),
        ("maintain", "granted", "write"),
        ("pull", "granted_read_only", "read"),
        ("triage", "granted_read_only", "read"),
        ("none", "no_github_access", None),
    ],
)
async def test_proposed_permission_policy(
    setup_linked, github_http, permission, status, access
) -> None:
    github_http.permission = permission
    assert (await setup_linked.grant()).status == status
    async with setup_linked.sessionmaker() as session:
        grants = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert [g.ceiling_access for g in grants] == ([access] if access else [])


@pytest.mark.asyncio
async def test_no_github_access_does_not_grant(setup_linked, github_http) -> None:
    github_http.permission = "none"
    assert (await setup_linked.grant()).status == "no_github_access"


@pytest.mark.asyncio
async def test_unlinked_or_other_account_needs_link(setup_linked, github_http) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        other = await make_account(session, tenant=setup_linked.tenant)
    assert (await setup_linked.grant(account_id=other.id)).status == "needs_link"
    async with setup_linked.sessionmaker.begin() as session:
        await unlink_account(session, account_id=setup_linked.account.id)
    assert (await setup_linked.grant()).status == "needs_link"


@pytest.mark.asyncio
async def test_other_tenant_link_is_not_used(setup_linked, github_http) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        other_tenant = await make_tenant(session, workspace_id=f"foreign-{uuid.uuid4().hex[:8]}")
        foreign = await make_account(session, tenant=other_tenant)
        await make_platform_principal(
            session,
            platform="discord",
            external_id="foreign",
            tenant=other_tenant,
            account=foreign,
        )
        await save_verified_link(
            session,
            intent_account_id=foreign.id,
            platform="discord",
            platform_user_id="foreign",
            github_user_id=301,
            login="foreign",
            access_token="foreign-synthetic",
            refresh_token="foreign-refresh",
            expires_in=3600,
            refresh_expires_in=86400,
            fernet=build_multifernet((Fernet.generate_key().decode(),)),
        )
    assert (await setup_linked.grant(account_id=foreign.id)).status == "needs_link"


@pytest.mark.asyncio
async def test_other_tenant_installation_is_visible_to_linked_caller(
    setup_linked, github_http
) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        other = await make_tenant(session, workspace_id=f"other-{uuid.uuid4().hex[:8]}")
        await session.execute(
            text("UPDATE tenant_github_repos SET tenant_id = :other WHERE tenant_id = :own"),
            {"other": other.id, "own": setup_linked.tenant.id},
        )
    assert (await setup_linked.grant()).status == "granted"
    async with setup_linked.sessionmaker() as session:
        rows = await list_authorized_repos(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
        assert any(r.repo_full_name == "acme/widgets" for r in rows)


@pytest.mark.asyncio
async def test_new_selected_installation_needs_no_prior_rows(setup_linked, github_http) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(text("DELETE FROM tenant_github_repos"))
        await session.execute(
            text("DELETE FROM github_app_installations WHERE installation_id = 201")
        )
    assert (await setup_linked.grant()).status == "granted"
    async with setup_linked.sessionmaker() as session:
        installation = await get_installation(session, installation_id=201)
        assert installation is not None
        assert installation.repository_selection == "selected"
        assert "acme/widgets" in installation.repo_full_names


@pytest.mark.asyncio
async def test_selected_installation_needs_install(setup_linked, github_http) -> None:
    github_http.repo_visible = False
    assert (await setup_linked.grant()).status == "needs_install"


@pytest.mark.asyncio
async def test_suspended_installation_never_grants(setup_linked, github_http) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE github_app_installations SET suspended_at = :now WHERE installation_id = 201"
            ),
            {"now": datetime.now(UTC)},
        )
    assert (await setup_linked.grant()).status == "changed_try_again"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["snapshot_removed", "installation_removed"])
async def test_github_coverage_loss_never_grants(setup_linked, github_http, change) -> None:
    async with setup_linked.sessionmaker.begin() as session:
        if change == "snapshot_removed":
            await session.execute(
                text(
                    "UPDATE github_app_installations SET github_repo_full_names = "
                    "ARRAY['acme/base'] WHERE installation_id = 201"
                )
            )
        else:
            await session.execute(
                text(
                    "UPDATE github_app_installations SET github_removed_at = :now "
                    "WHERE installation_id = 201"
                ),
                {"now": datetime.now(UTC)},
            )
    assert (await setup_linked.grant()).status == "changed_try_again"


@pytest.mark.asyncio
async def test_new_suspended_installation_never_grants(setup_linked, github_http) -> None:
    github_http.installation_suspended = True
    assert (await setup_linked.grant()).status == "needs_install"


@pytest.mark.asyncio
async def test_failed_refresh_returns_relink(setup_linked, github_http) -> None:
    github_http.refresh_fail = True
    async with setup_linked.sessionmaker.begin() as session:
        await session.execute(
            text(
                "UPDATE github_user_links SET access_expires_at = :past WHERE github_user_id = 300"
            ),
            {"past": datetime.now(UTC) - timedelta(minutes=1)},
        )
    assert (await setup_linked.grant()).status == "relink"


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", ["manager", "link"])
async def test_revocation_between_lookup_and_commit(
    setup_linked, github_http, monkeypatch, revoked
) -> None:
    async def revoke() -> None:
        async with setup_linked.sessionmaker.begin() as session:
            if revoked == "manager":
                await set_role(session, setup_linked.account.id, Role.USER)
            else:
                await unlink_account(session, account_id=setup_linked.account.id)

    monkeypatch.setattr(linked, "_before_commit", revoke)
    assert (await setup_linked.grant()).status == "changed_try_again"
    async with setup_linked.sessionmaker() as session:
        assert (
            await list_agent_grants(
                session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
            )
            == []
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["removed", "suspended", "snapshot_removed", "installation_removed"]
)
async def test_installation_change_before_commit_retries(
    setup_linked, github_http, monkeypatch, change
) -> None:
    async def change_installation() -> None:
        async with setup_linked.sessionmaker.begin() as session:
            if change == "removed":
                await session.execute(
                    text(
                        "UPDATE github_app_installations SET repo_full_names = "
                        "ARRAY['acme/base'] WHERE installation_id = 201"
                    )
                )
            elif change == "snapshot_removed":
                await session.execute(
                    text(
                        "UPDATE github_app_installations SET github_repo_full_names = "
                        "ARRAY['acme/base'] WHERE installation_id = 201"
                    )
                )
            elif change == "suspended":
                await session.execute(
                    text(
                        "UPDATE github_app_installations SET suspended_at = :now "
                        "WHERE installation_id = 201"
                    ),
                    {"now": datetime.now(UTC)},
                )
            else:
                await session.execute(
                    text(
                        "UPDATE github_app_installations SET github_removed_at = :now "
                        "WHERE installation_id = 201"
                    ),
                    {"now": datetime.now(UTC)},
                )

    monkeypatch.setattr(linked, "_before_commit", change_installation)
    assert (await setup_linked.grant()).status == "changed_try_again"


@pytest.mark.asyncio
async def test_github_permission_revoked_after_lookup_is_grant_time_authority(
    setup_linked, github_http, monkeypatch
) -> None:
    async def revoke_permission() -> None:
        github_http.permission = "none"

    monkeypatch.setattr(linked, "_before_commit", revoke_permission)
    assert (await setup_linked.grant()).status == "granted"


@pytest.mark.asyncio
async def test_platform_membership_revoked_after_manages_now_returns(
    setup_linked, github_http
) -> None:
    membership = {"active": True}

    async def manages_now(_session: AsyncSession) -> bool:
        allowed = membership["active"]
        membership["active"] = False
        return allowed

    result = await linked.linked_grant(
        setup_linked.sessionmaker,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        agent_id=setup_linked.agent_id,
        agent_name="Agent",
        repo_name="acme/widgets",
        fernet=setup_linked.fernet,
        client_id="synthetic",
        client_secret="synthetic",
        app_id="42",
        private_key="synthetic",
        manages_now=manages_now,
    )
    assert result.status == "granted"
    assert membership["active"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["linked", "needs_install", "wrong_person", "expired_pending"])
async def test_pending_grant_resumes_from_personal_link_callback(
    setup_linked, github_http, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    settings = Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
        anthropic=AnthropicSettings(api_key=SecretStr("test")),
        mcp=McpSettings(public_url=HttpUrl("https://mcp.test/mcp")),
        github_app=GithubAppSettings(
            app_id="42",
            app_slug="sample-app",
            private_key=SecretStr("pem"),
            client_id="client",
            client_secret=SecretStr("secret"),
        ),
        hub=HubSettings(
            discord_client_id="discord-client", discord_client_secret=SecretStr("discord-secret")
        ),
    )
    runtime = McpRuntime(
        session_factory=setup_linked.sessionmaker,
        client=AsyncMock(),
        settings=settings,
        deployment_default=DeploymentDefault(),
        fernet=setup_linked.fernet,
    )
    auth = AuthIdentity(
        account_id=setup_linked.account.id,
        tenant_id=setup_linked.tenant.id,
        role=Role.ADMIN,
        platform="discord",
        external_id=setup_linked.tenant.external_id,
        platform_user_id="person",
        is_admin=True,
    )
    origin = SimpleNamespace(
        configuration_target_name="Agent",
        configuration_target_ma_agent_id="ma_agent",
        responder_name="Daimon",
        responder_ma_agent_id="ma_daimon",
        parent_channel_id="123",
        thread_id="456",
    )
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(connect_tool, "require_turn_origin", AsyncMock(return_value=origin))
    monkeypatch.setattr(connect_tool, "resolve_setup_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    delivery = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    post_link = AsyncMock()
    monkeypatch.setattr(connect_tool, "_post_personal_link", post_link)
    async with setup_linked.sessionmaker.begin() as session:
        await unlink_account(session, account_id=setup_linked.account.id)
    result = await connect_tool._github_connect_impl(
        runtime, auth, origin_context_id="synthetic", repo_name="acme/widgets"
    )
    assert result.status == "needs_link"
    post_link.assert_awaited_once()
    url = post_link.await_args.kwargs["url"]
    if scenario == "needs_install":
        github_http.repo_visible = False
    if scenario == "expired_pending":
        async with setup_linked.sessionmaker.begin() as session:
            await session.execute(
                text("UPDATE github_linked_connect_pending SET expires_at = :past"),
                {"past": datetime.now(UTC) - timedelta(seconds=1)},
            )

    discord_user = "another" if scenario == "wrong_person" else "person"

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/oauth2/token":
            return httpx.Response(200, json={"access_token": "discord-access"})
        if request.url.path == "/api/users/@me":
            return httpx.Response(200, json={"id": discord_user})
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(
                200,
                json={
                    "access_token": "github-access",
                    "refresh_token": "github-refresh",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 86400,
                },
            )
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 300, "login": "person"})
        return github_http.respond(request)

    routes = build_personal_link_routes(
        settings=settings,
        sessionmaker=setup_linked.sessionmaker,
        fernet=setup_linked.fernet,
        runtime=runtime,
        client_factory=lambda: github_http.original_client(transport=httpx.MockTransport(api)),
    )
    app = Starlette(
        routes=[
            Route("/oauth/github/link/platform-callback", routes[1]),
            Route("/oauth/github/link/{token}", routes[0]),
            Route("/oauth/github/callback", routes[2]),
        ]
    )
    async with github_http.original_client(
        transport=httpx.ASGITransport(app=app), base_url="https://mcp.test"
    ) as browser:
        opened = await browser.get(url)
        assert opened.status_code == 307
        platform_state = parse_qs(urlparse(opened.headers["location"]).query)["state"][0]
        verified = await browser.get(
            "/oauth/github/link/platform-callback",
            params={"state": platform_state, "code": "discord-code"},
        )
        if scenario == "wrong_person":
            assert verified.status_code == 403
        else:
            assert verified.status_code == 307
            github_state = parse_qs(urlparse(verified.headers["location"]).query)["state"][0]
            linked_page = await browser.get(
                "/oauth/github/callback", params={"state": github_state, "code": "github-code"}
            )
            assert linked_page.status_code == 200
    async with setup_linked.sessionmaker() as session:
        grants = await list_agent_grants(
            session, tenant_id=setup_linked.tenant.id, agent_id=setup_linked.agent_id
        )
    assert len(grants) == (1 if scenario == "linked" else 0)
    assert delivery.await_count == (1 if scenario in ("linked", "needs_install") else 0)
    if scenario == "needs_install":
        assert post_link.await_count == 2
        assert post_link.await_args.kwargs["label"] == "Open GitHub setup"
        assert post_link.await_args.kwargs["url"] == (
            "https://github.com/apps/sample-app/installations/new"
        )
        assert "https://" not in delivery.await_args.args[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["needs_install", "needs_link", "relink"])
async def test_pending_resume_sends_private_recovery_button(
    setup_linked, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    runtime = McpRuntime(
        session_factory=setup_linked.sessionmaker,
        client=AsyncMock(),
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test/test")),
            anthropic=AnthropicSettings(api_key=SecretStr("test")),
            mcp=McpSettings(public_url=HttpUrl("https://mcp.test/mcp")),
            github_app=GithubAppSettings(
                app_id="42",
                app_slug="sample-app",
                private_key=SecretStr("pem"),
                client_id="client",
                client_secret=SecretStr("secret"),
            ),
        ),
        deployment_default=DeploymentDefault(),
        fernet=setup_linked.fernet,
    )
    async with setup_linked.sessionmaker.begin() as session:
        await linked_pending.save_pending(
            session,
            tenant_id=setup_linked.tenant.id,
            account_id=setup_linked.account.id,
            agent_id=setup_linked.agent_id,
            ma_agent_id="ma_agent",
            agent_name="Agent",
            repo_name="acme/widgets",
            requested_access="write",
            platform="discord",
            platform_user_id="person",
            workspace_id=setup_linked.tenant.external_id,
            parent_channel_id="123",
            thread_id="456",
        )
    agent = SimpleNamespace(id="ma_agent", name="Agent", metadata={})
    monkeypatch.setattr(linked_pending, "find_agent_by_daimon_tag", AsyncMock(return_value=agent))
    monkeypatch.setattr(linked_pending, "requester_manages_agent", AsyncMock(return_value=True))
    monkeypatch.setattr(
        linked_pending,
        "linked_grant",
        AsyncMock(
            return_value=linked.LinkedOutcome(status=status, message="Private setup needed.")
        ),
    )
    delivery = AsyncMock()
    private_button = AsyncMock()
    monkeypatch.setattr(linked_pending, "_deliver_result", delivery)
    monkeypatch.setattr(connect_tool, "_post_personal_link", private_button)
    result = await linked_pending.resume_pending(
        runtime,
        tenant_id=setup_linked.tenant.id,
        account_id=setup_linked.account.id,
        platform="discord",
        platform_user_id="person",
    )
    assert result == ["Private setup needed."]
    delivery.assert_awaited_once()
    assert delivery.await_args.args[2] == "Private setup needed."
    private_button.assert_awaited_once()
    button = private_button.await_args.kwargs
    assert (button["channel_id"], button["thread_id"]) == ("123", "456")
    if status == "needs_install":
        assert button["label"] == "Open GitHub setup"
        assert button["url"] == "https://github.com/apps/sample-app/installations/new"
    else:
        assert button["label"] == "Link GitHub"
        assert button["url"].startswith("https://mcp.test/oauth/github/link/")
    async with setup_linked.sessionmaker.begin() as session:
        pending = await take_pending(
            session,
            tenant_id=setup_linked.tenant.id,
            account_id=setup_linked.account.id,
            platform="discord",
            platform_user_id="person",
        )
    assert len(pending) == (0 if status == "needs_install" else 1)
    if pending:
        assert pending[0].requested_access == "write"
