"""The keep-alive sweep spends refresh tokens that are close to expiry, once."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.core._models import GitHubUserLink
from daimon.core.github_credentials import build_multifernet, decrypt_token, encrypt_token
from daimon.core.github_link_refresh import LinkRefreshCounts, refresh_expiring_links
from daimon.core.github_requester_access import linked_access_token_with_generation
from daimon.core.stores import github_links
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio


@pytest.fixture
def fernet() -> MultiFernet:
    return build_multifernet((Fernet.generate_key().decode(),))


async def _add_user(
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
    user_id: int,
    *,
    refresh_in: timedelta | None = timedelta(days=10),
    access_in: timedelta = timedelta(hours=5),
    status: Literal["active", "broken"] = "active",
    with_refresh_token: bool = True,
) -> None:
    now = datetime.now(UTC)
    async with sessionmaker.begin() as session:
        session.add(
            GitHubUserLink(
                github_user_id=user_id,
                login=f"user{user_id}",
                encrypted_access_token=encrypt_token(fernet, f"access-{user_id}"),
                encrypted_refresh_token=(
                    encrypt_token(fernet, f"refresh-{user_id}") if with_refresh_token else None
                ),
                access_expires_at=now + access_in,
                refresh_expires_at=None if refresh_in is None else now + refresh_in,
                status=status,
            )
        )


def _github(
    seen: list[str], *, fail_for: dict[str, httpx.Response] | None = None
) -> httpx.AsyncClient:
    """Fake GitHub refresh endpoint; rotates ``refresh-N`` to ``refresh-N-new``."""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/login/oauth/access_token"
        body = dict(pair.split("=") for pair in request.content.decode().split("&"))
        spent = body["refresh_token"]
        seen.append(spent)
        if fail_for and spent in fail_for:
            return fail_for[spent]
        return httpx.Response(
            200,
            json={
                "access_token": f"{spent}-access",
                "refresh_token": f"{spent}-new",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _sweep(
    sessionmaker: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    fernet: MultiFernet,
    limit: int = 50,
) -> LinkRefreshCounts:
    return await refresh_expiring_links(
        sessionmaker,
        client,
        fernet=fernet,
        client_id="client",
        client_secret="secret",
        now=datetime.now(UTC),
        limit=limit,
    )


async def test_sweep_refreshes_a_row_inside_the_window_and_rotates_tokens(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 1)
    seen: list[str] = []
    async with _github(seen) as client:
        counts = await _sweep(db_session_factory, client, fernet)
    assert counts == LinkRefreshCounts(refreshed=1, broken=0, failed=0)
    assert seen == ["refresh-1"]
    async with db_session_factory() as session:
        row = await github_links.get_user(session, github_user_id=1)
    assert row is not None and row.encrypted_refresh_token is not None
    assert row.status == "active" and row.token_generation == 2 and row.link_generation == 1
    assert decrypt_token(fernet, row.encrypted_access_token) == "refresh-1-access"
    assert decrypt_token(fernet, row.encrypted_refresh_token) == "refresh-1-new"
    assert row.refresh_expires_at is not None
    assert row.refresh_expires_at > datetime.now(UTC) + timedelta(days=150)
    # The row has left the window, so the next sweep leaves it alone.
    async with _github(seen) as client:
        again = await _sweep(db_session_factory, client, fernet)
    assert again == LinkRefreshCounts() and seen == ["refresh-1"]


async def test_sweep_skips_rows_it_must_not_touch(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 1, refresh_in=timedelta(days=90))  # outside
    await _add_user(db_session_factory, fernet, 2, refresh_in=timedelta(days=-1))  # expired
    await _add_user(db_session_factory, fernet, 3, status="broken")
    await _add_user(db_session_factory, fernet, 4, with_refresh_token=False)
    await _add_user(db_session_factory, fernet, 5, refresh_in=None)
    seen: list[str] = []
    async with _github(seen) as client:
        counts = await _sweep(db_session_factory, client, fernet)
    assert counts == LinkRefreshCounts()
    assert seen == []


async def test_sweep_takes_the_soonest_expiring_rows_up_to_the_limit(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 1, refresh_in=timedelta(days=20))
    await _add_user(db_session_factory, fernet, 2, refresh_in=timedelta(days=2))
    await _add_user(db_session_factory, fernet, 3, refresh_in=timedelta(days=9))
    seen: list[str] = []
    async with _github(seen) as client:
        counts = await _sweep(db_session_factory, client, fernet, limit=2)
    assert counts.refreshed == 2
    assert seen == ["refresh-2", "refresh-3"]


async def test_bad_refresh_token_marks_the_link_broken(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 1)
    seen: list[str] = []
    bad = httpx.Response(200, json={"error": "bad_refresh_token"})
    async with _github(seen, fail_for={"refresh-1": bad}) as client:
        counts = await _sweep(db_session_factory, client, fernet)
    assert counts == LinkRefreshCounts(refreshed=0, broken=1, failed=0)
    async with db_session_factory() as session:
        row = await github_links.get_user(session, github_user_id=1)
    assert row is not None and row.status == "broken" and row.link_generation == 2


async def test_a_server_error_for_one_user_does_not_stop_the_next(
    db_session_factory: async_sessionmaker[AsyncSession],
    fernet: MultiFernet,
) -> None:
    await _add_user(db_session_factory, fernet, 1, refresh_in=timedelta(days=1))
    await _add_user(db_session_factory, fernet, 2, refresh_in=timedelta(days=2))
    await _add_user(db_session_factory, fernet, 3, refresh_in=timedelta(days=3))
    seen: list[str] = []
    fail = {
        "refresh-1": httpx.Response(500, text="boom"),
        "refresh-2": httpx.Response(429, text="slow down"),
    }
    async with _github(seen, fail_for=fail) as client:
        counts = await _sweep(db_session_factory, client, fernet)
    assert counts == LinkRefreshCounts(refreshed=1, broken=0, failed=2)
    assert seen == ["refresh-1", "refresh-2", "refresh-3"]
    async with db_session_factory() as session:
        failed = await github_links.get_user(session, github_user_id=1)
        done = await github_links.get_user(session, github_user_id=3)
    # A failed refresh leaves the stored credential untouched, ready for the next sweep.
    assert failed is not None and failed.status == "active" and failed.token_generation == 1
    assert failed.encrypted_refresh_token is not None
    assert decrypt_token(fernet, failed.encrypted_refresh_token) == "refresh-1"
    assert done is not None and done.token_generation == 2


async def test_sweep_and_connect_never_spend_the_same_refresh_token(
    db_engine: AsyncEngine, db_clean: None, fernet: MultiFernet
) -> None:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    # The access token is expired, so Connect and the sweep both want a refresh.
    await _add_user(sessionmaker, fernet, 1, access_in=timedelta(minutes=-5))
    seen: list[str] = []
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = dict(pair.split("=") for pair in request.content.decode().split("&"))
        seen.append(body["refresh_token"])
        await asyncio.sleep(0.1)  # hold the refresh open so the other caller must queue
        gate.set()
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 28800,
                "refresh_token_expires_in": 15897600,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connect, counts = await asyncio.gather(
            linked_access_token_with_generation(
                sessionmaker,
                client,
                user_id=1,
                fernet=fernet,
                client_id="client",
                client_secret="secret",
            ),
            _sweep(sessionmaker, client, fernet),
        )
    assert seen == ["refresh-1"], "the refresh token must be spent exactly once"
    assert connect[0] == "rotated-access"
    assert counts == LinkRefreshCounts(refreshed=1, broken=0, failed=0)
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=1)
    assert row is not None and row.token_generation == 2 and row.status == "active"


async def test_sweep_does_not_respend_a_token_connect_just_rotated(
    db_engine: AsyncEngine, db_clean: None, fernet: MultiFernet
) -> None:
    """A valid access token and a near-expiry refresh token: two sweeps, one POST."""
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    await _add_user(sessionmaker, fernet, 1)
    seen: list[str] = []
    async with _github(seen) as client:
        first, second = await asyncio.gather(
            _sweep(sessionmaker, client, fernet), _sweep(sessionmaker, client, fernet)
        )
    assert seen == ["refresh-1"]
    assert first.refreshed + second.refreshed == 2  # both end with a usable token
    async with sessionmaker() as session:
        row = await github_links.get_user(session, github_user_id=1)
    assert row is not None and row.token_generation == 2
