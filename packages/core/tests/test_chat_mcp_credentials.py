"""DB-backed lifetime/rotation tests through the actual vault SDK and registry."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import httpx
import jwt
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.verifier import DaimonJWTVerifier
from daimon.core.mcp_auth import CHAT_TOKEN_TTL
from daimon.core.mcp_vault import ensure_agent_mcp_vault
from daimon.core.stores.mcp_tokens import get_mcp_token, revoke_mcp_token
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.mark.parametrize("private", [False, True])
async def test_registered_vault_rotates_before_expiry_and_after_revoke(
    db_engine, db_clean, private
):
    # Separate connections expose registry publication races hidden by shared-connection fixtures.
    db_session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(minutes=30)
    async with db_session_factory() as db, db.begin():
        tenant = await make_tenant(db, platform="discord")
        account = await make_account(db, tenant=tenant)
    agent = uuid.uuid4()
    context_id = uuid.uuid4() if private else None
    name = f"daimon-mcp:{account.id}:{agent}"
    if context_id is not None:
        name += f":slack-turn:{context_id}"
    secret = b"s" * 32
    slots = []
    minted = []
    tokens = []
    writes = []
    admitted_at_publication = []

    async def handler(req):
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "vlt_chat",
                            "type": "vault",
                            "display_name": name,
                            "created_at": now.isoformat(),
                            "metadata": None,
                            "archived_at": None,
                        }
                    ],
                    "has_more": False,
                },
            )
        if req.method == "GET" and req.url.path == "/v1/vaults/vlt_chat/credentials":
            return httpx.Response(200, json={"data": slots, "has_more": False})
        if req.method == "POST" and req.url.path in (
            "/v1/vaults/vlt_chat/credentials",
            "/v1/vaults/vlt_chat/credentials/cred_chat",
        ):
            body = json.loads(req.content)
            claims = jwt.decode(
                body["auth"]["token"],
                secret,
                algorithms=["HS256"],
                options={"verify_iat": False, "verify_exp": False},
            )
            minted.append(claims)
            tokens.append(body["auth"]["token"])
            if len(minted) > 1:
                verifier = DaimonJWTVerifier(secret=secret, sessionmaker=db_session_factory)
                admitted_at_publication.append(
                    await verifier.verify_token(body["auth"]["token"]) is not None
                )
            writes.append(req.url.path)
            slot = {
                "id": "cred_chat",
                "type": "credential",
                "vault_id": "vlt_chat",
                "auth": {"type": "static_bearer", "mcp_server_url": "https://mcp.example.com/mcp"},
                "metadata": body["metadata"],
            }
            slots[:] = [slot]
            return httpx.Response(200, json=slot)
        raise AssertionError(f"unexpected request:{req.method}:{req.url.path}")

    client = AsyncAnthropic(
        api_key="offline", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    try:
        kwargs = dict(
            account_id=account.id,
            agent_id=agent,
            jwt_secret=secret,
            public_url="https://mcp.example.com/mcp",
            session_factory=db_session_factory,
            slack_turn_context_id=context_id,
        )
        assert await ensure_agent_mcp_vault(client, now=now, **kwargs) == "vlt_chat"
        claims = minted[0]
        assert claims["exp"] - claims["iat"] == int(CHAT_TOKEN_TTL.total_seconds())
        assert (
            claims["chat_agent_id"] == str(agent)
            and "agent_id" not in claims
            and "internal" not in claims
        )
        async with db_session_factory() as db:
            row = await get_mcp_token(db, jti=uuid.UUID(claims["jti"]))
        assert (
            row is not None
            and row.account_id == account.id
            and row.tenant_id == tenant.id
            and row.agent_id == str(agent)
        )
        assert row.expires_at is not None
        await ensure_agent_mcp_vault(client, now=now + dt.timedelta(minutes=1), **kwargs)
        assert len(minted) == 1
        # Exercise the REAL reused-session hook even with no external credentials,
        # no encryption, and no compatibility update operations.
        from daimon.core.config import McpSettings
        from daimon.core.session_snapshot import snapshot_from_created_session
        from daimon.core.session_update_ops import apply_update_ops
        from daimon.core.tool_safety import OPEN_TOOL_SAFETY
        from daimon.testing import ma_agent, ma_session
        from pydantic import SecretStr

        native_agent = ma_agent(id="agent_renew")
        live = ma_session(id="sess_renew", agent_id=native_agent.id)
        recorded = snapshot_from_created_session(
            live,
            env_sha256=None,
            env_file_id=None,
            repo_token_issued_at=None,
            vault_id="vlt_chat",
            sent_skills=live.agent.skills,
        )
        result = await apply_update_ops(
            client,
            db_session_factory,
            ops=(),
            session_id=live.id,
            recorded=recorded,
            agent=native_agent,
            tenant_id=tenant.id,
            agent_uuid=agent,
            account_id=account.id,
            mcp=McpSettings(
                jwt_secret=SecretStr(secret.decode()), public_url="https://mcp.example.com/mcp"
            ),
            tool_safety=OPEN_TOOL_SAFETY,
            fernet=None,
            github_fallback_pat=None,
            github_app_id=None,
            github_app_private_key=None,
            now=now + dt.timedelta(minutes=11),
        )
        assert result.snapshot.vault_id == "vlt_chat" and result.applied == ()
        assert minted[-1].get("slack_turn_context_id") == (
            None if context_id is None else str(context_id)
        )
        assert len(minted) == 2 and minted[1]["jti"] != claims["jti"]
        async with db_session_factory() as db, db.begin():
            await revoke_mcp_token(
                db, jti=uuid.UUID(minted[1]["jti"]), now=now + dt.timedelta(minutes=12)
            )
        await ensure_agent_mcp_vault(client, now=now + dt.timedelta(minutes=12), **kwargs)
        assert len(minted) == 3 and minted[2]["jti"] != minted[1]["jti"]
        # Rotation preserves the first, individually revocable credential for a running turn.
        async with db_session_factory() as db:
            old = await get_mcp_token(db, jti=uuid.UUID(claims["jti"]))
        assert old is not None and old.revoked_at is None
        verifier = DaimonJWTVerifier(secret=secret, sessionmaker=db_session_factory)
        assert await verifier.verify_token(tokens[0]) is not None
        assert writes[1:] == ["/v1/vaults/vlt_chat/credentials/cred_chat"] * 2
        assert admitted_at_publication == [True, True], (
            "a running session can use the replacement before its vault-write response returns"
        )
    finally:
        await client.close()
