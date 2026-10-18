"""Installation-token failures isolate repository groups without losing token inventory."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.core import github_app_session
from daimon.core._models import (
    GitHubInstallationReconciliation,
    GitHubIssuedToken,
    Tenant,
    TenantGitHubRepo,
)
from daimon.core.config import GithubAppSettings
from daimon.core.github_app_session import (
    GitHubTokenMintUnavailable,
    prepare_app_access,
    rotate_live_app_tokens,
)
from daimon.core.github_credentials import build_multifernet
from daimon.core.github_requester_access import PermissionCache
from daimon.core.session_snapshot import SessionSnapshot
from daimon.core.stores import github_access, github_app_installations
from daimon.core.stores.thread_sessions import create_thread_session, get_thread_session_by_id
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.termination import TerminationReason
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _two_groups(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with factory.begin() as session:
        session.add(Tenant(id=tenant_id, platform="discord", external_id=str(tenant_id)))
        await session.flush()
        for repo_id, name in ((101, "example/read"), (102, "example/write")):
            session.add(
                TenantGitHubRepo(
                    tenant_id=tenant_id,
                    repo_id=repo_id,
                    owner_id=1,
                    installation_id=771,
                    repo_full_name=name,
                    max_access="write",
                    authorized_by_github_user_id=2,
                    status="active",
                    version=1,
                )
            )
        await session.flush()
        await github_app_installations.upsert_github_app(
            session,
            installation_id=771,
            account_id=1,
            account_login="example",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        for repo_id, access in ((101, "read"), (102, "write")):
            await github_access.stage_grant(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                repo_id=repo_id,
                baseline_access=access,
                ceiling_access=access,
                granted_by_account_id=None,
                is_working_repo=repo_id == 102,
            )
    return tenant_id, agent_id


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 404, 422])
async def test_coverage_mint_failure_keeps_other_group_and_dedupes_reconciliation(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    tenant_id, agent_id = await _two_groups(db_session_factory)
    monkeypatch.setattr("daimon.core.github_app_session.build_app_jwt", lambda *a, **k: "jwt")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        ids = json.loads(request.content)["repository_ids"]
        return (
            httpx.Response(status)
            if ids == [102]
            else httpx.Response(201, json={"token": "read-token"})
        )

    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for attempt in range(2):
            access = await prepare_app_access(
                db_session_factory,
                client,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=None,
                is_external=False,
                provisional_session_id=f"pending-{attempt}",
                config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
                fernet=fernet,
                cache=PermissionCache(),
            )
            assert [token.repo_ids for token in access.tokens] == [(101,)]
            assert access.excluded_repo_urls == ("https://github.com/example/write",)
            assert access.resources == ()
            async with db_session_factory() as session:
                rows = list(
                    await session.scalars(
                        select(GitHubIssuedToken).where(
                            GitHubIssuedToken.session_id == f"pending-{attempt}"
                        )
                    )
                )
                assert sorted(row.status for row in rows) == ["revoked", "stored"]
                job = await session.get(GitHubInstallationReconciliation, 771)
                assert job is not None and job.generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 500, "timeout"])
async def test_transient_mint_failure_closes_rows_and_revokes_issued_token(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failure: int | str,
) -> None:
    tenant_id, agent_id = await _two_groups(db_session_factory)
    monkeypatch.setattr("daimon.core.github_app_session.build_app_jwt", lambda *a, **k: "jwt")
    revoked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            revoked.append(request.headers["Authorization"])
            return httpx.Response(204)
        if json.loads(request.content)["repository_ids"] == [101]:
            return httpx.Response(201, json={"token": "read-token"})
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(failure)

    fernet = build_multifernet((Fernet.generate_key().decode(),))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubTokenMintUnavailable) as caught:
            await prepare_app_access(
                db_session_factory,
                client,
                tenant_id=tenant_id,
                agent_id=agent_id,
                account_id=None,
                is_external=False,
                provisional_session_id="pending-failed",
                config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
                fernet=fernet,
                cache=PermissionCache(),
            )
    notice = render_termination_notice(TerminationReason.UPSTREAM, error=caught.value)
    assert notice is not None and notice.title == "GitHub access is temporarily unavailable."
    assert "again" in notice.next_step
    assert revoked == ["Bearer read-token"]
    async with db_session_factory() as session:
        rows = list(
            await session.scalars(
                select(GitHubIssuedToken).where(GitHubIssuedToken.session_id == "pending-failed")
            )
        )
        assert len(rows) == 2 and all(row.status == "revoked" for row in rows)
        assert await session.get(GitHubInstallationReconciliation, 771) is None


@pytest.mark.asyncio
async def test_rotation_keeps_successful_group_when_other_group_returns_404(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, agent_id = await _two_groups(db_session_factory)
    read_url = "https://github.com/example/read"
    write_url = "https://github.com/example/write"
    async with db_session_factory.begin() as session:
        mapped = await create_thread_session(
            session,
            tenant_id=tenant_id,
            platform="discord",
            thread_id="rotation-refusal",
            account_id=uuid.uuid4(),
            ma_session_id="live-session",
            effective_config=SessionSnapshot.model_validate(
                {
                    "ma_agent_id": "agent_rotation",
                    "model_id": "test-model",
                    "system_sha256": None,
                    "skills_sha256": "skills",
                    "environment_id": "environment",
                    "github_mode": "app",
                    "repo_urls": [write_url],
                    "token_repo_urls": [read_url, write_url],
                    "repo_url": None,
                    "repo_branch": None,
                    "memory_store_id": None,
                    "vault_id": "vault",
                    "tools_sha256": "tools",
                    "mcp_servers_sha256": "servers",
                    "env_sha256": None,
                    "agent_version": 1,
                    "agent_name": "Agent",
                }
            ),
        )
    monkeypatch.setattr("daimon.core.github_app_session.build_app_jwt", lambda *a, **k: "jwt")
    real_prepare = prepare_app_access
    minted_ids: list[list[int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        ids = json.loads(request.content)["repository_ids"]
        minted_ids.append(ids)
        return (
            httpx.Response(404)
            if ids == [101]
            else httpx.Response(201, json={"token": "write-token"})
        )

    async def prepare_with_transport(*args: object, **kwargs: object):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await real_prepare(db_session_factory, client, **kwargs)

    delivered: list[str] = []

    async def add_credentials(*args: object, **kwargs: object) -> None:
        access = kwargs["access"]
        delivered.extend(token.token for token in access.tokens)

    async def rotate_resource(*args: object, **kwargs: object) -> None:
        delivered.append(str(args[3]))

    monkeypatch.setattr(github_app_session, "prepare_app_access", prepare_with_transport)
    monkeypatch.setattr(github_app_session, "add_app_credentials", add_credentials)
    monkeypatch.setattr(github_app_session, "rotate_session_repo_token", rotate_resource)
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    rotated = await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]  # external MA calls are stubbed above
        db_session_factory,
        session_id="live-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        is_external=False,
        vault_id="vault",
        resource_ids={"https://github.com/example/write": "resource"},
        config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
        fernet=fernet,
    )
    assert delivered == ["write-token", "write-token"]
    assert rotated.excluded_repo_urls == (read_url,)
    assert rotated.token_repo_urls == (write_url,)
    async with db_session_factory() as session:
        rows = list(
            await session.scalars(
                select(GitHubIssuedToken).where(GitHubIssuedToken.agent_id == agent_id)
            )
        )
        assert sorted(row.status for row in rows) == ["delivered", "revoked"]
        job = await session.get(GitHubInstallationReconciliation, 771)
        assert job is not None and job.generation == 1
        current = await get_thread_session_by_id(session, id=mapped.id)
        assert current is not None and current.effective_config is not None
        assert current.effective_config.token_repo_urls == (write_url,)
        assert tuple(url for url, _ in current.effective_config.unavailable_repos) == (read_url,)
        assert current.effective_config.unavailable_notice_pending == (read_url,)
        first_refused = current.effective_config.unavailable_repos[0][1]

    minted_ids.clear()
    await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]
        db_session_factory,
        session_id="live-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        is_external=False,
        vault_id="vault",
        resource_ids={write_url: "resource"},
        config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
        fernet=fernet,
        unavailable_repos=((read_url, first_refused),),
    )
    assert minted_ids == [[102]]

    # A newly granted repo sharing the refused repo's profile is minted alone.
    async with db_session_factory.begin() as session:
        session.add(
            TenantGitHubRepo(
                tenant_id=tenant_id,
                repo_id=103,
                owner_id=1,
                installation_id=771,
                repo_full_name="example/new-read",
                max_access="read",
                authorized_by_github_user_id=2,
                status="active",
                version=1,
            )
        )
        await session.flush()
        await github_app_installations.upsert_github_app(
            session,
            installation_id=771,
            account_id=1,
            account_login="example",
            account_type="Organization",
            repository_selection="selected",
            suspended_at=None,
        )
        await github_access.stage_grant(
            session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_id=103,
            baseline_access="read",
            ceiling_access="read",
            granted_by_account_id=None,
            is_working_repo=False,
        )
    minted_ids.clear()
    newly_rotated = await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]
        db_session_factory,
        session_id="live-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        is_external=False,
        vault_id="vault",
        resource_ids={write_url: "resource"},
        config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
        fernet=fernet,
        unavailable_repos=((read_url, first_refused),),
    )
    assert minted_ids == [[103], [102]]
    assert newly_rotated.excluded_repo_urls == (read_url,)
    async with db_session_factory() as session:
        current = await get_thread_session_by_id(session, id=mapped.id)
        assert current is not None and current.effective_config is not None
        assert current.effective_config.unavailable_repos == ((read_url, first_refused),)
        assert current.effective_config.unavailable_notice_pending == (read_url,)

    # Even after its 60 minutes, rotation leaves the refused repo to the
    # turn-boundary replacement and keeps the original refusal time.
    expired = first_refused - timedelta(minutes=61)
    minted_ids.clear()
    await rotate_live_app_tokens(
        None,  # type: ignore[arg-type]
        db_session_factory,
        session_id="live-session",
        tenant_id=tenant_id,
        agent_id=agent_id,
        account_id=None,
        is_external=False,
        vault_id="vault",
        resource_ids={write_url: "resource"},
        config=GithubAppSettings(app_id="123", private_key=SecretStr("unused")),
        fernet=fernet,
        unavailable_repos=((read_url, expired),),
    )
    assert minted_ids == [[103], [102]]


def test_repos_refused_at_mint_do_not_replace_the_session_every_turn() -> None:
    from daimon.core.session_compat import identity_change_reasons
    from daimon.core.session_snapshot import SessionSnapshot

    base = SessionSnapshot.model_validate(
        {
            "ma_agent_id": "agent_compat",
            "model_id": "test-model",
            "system_sha256": None,
            "skills_sha256": "skills",
            "environment_id": "environment",
            "github_mode": "app",
            "repo_urls": [],
            "token_repo_urls": [],
            "repo_url": None,
            "repo_branch": None,
            "memory_store_id": None,
            "vault_id": "vault",
            "tools_sha256": "tools",
            "mcp_servers_sha256": "servers",
            "env_sha256": None,
            "agent_version": 1,
            "agent_name": "Agent",
        }
    )
    common = {
        "github_mode": "app",
        "repo_urls": ("https://github.com/acme/kept",),
        "repo_url": None,
        "repo_branch": None,
    }
    recorded = base.model_copy(
        update={
            **common,
            "token_repo_urls": ("https://github.com/acme/kept",),
            "unavailable_repos": (
                ("https://github.com/acme/refused", datetime(2026, 10, 16, 12, tzinfo=UTC)),
            ),
        }
    )
    still_covered = base.model_copy(
        update={
            **common,
            "token_repo_urls": ("https://github.com/acme/kept", "https://github.com/acme/refused"),
            "unavailable_repos": (),
        }
    )
    reconciled = base.model_copy(
        update={**common, "token_repo_urls": ("https://github.com/acme/kept",)}
    )
    newly_granted = base.model_copy(
        update={
            **common,
            "token_repo_urls": ("https://github.com/acme/kept", "https://github.com/acme/new"),
        }
    )

    now = recorded.unavailable_repos[0][1] + timedelta(minutes=30)
    assert "repo_set" not in identity_change_reasons(recorded, still_covered, now=now)
    assert "repo_set" not in identity_change_reasons(recorded, reconciled, now=now)
    assert "repo_set" in identity_change_reasons(recorded, newly_granted, now=now)
    assert "repo_set" in identity_change_reasons(
        recorded, still_covered, now=now + timedelta(minutes=30)
    )
    assert "repo_set" in identity_change_reasons(
        recorded.model_copy(update={"unavailable_repos": ()}), still_covered, now=now
    )
