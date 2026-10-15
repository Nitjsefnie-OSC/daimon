"""F2 agent-scoped credential owner, invoked only after an acknowledged agent."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import mint_agent_mcp_token, token_jti
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.mcp_tokens import get_mcp_token, revoke_mcp_token
from mux.contracts.ids import ResourceRef
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from daimon.testing.anthropic_live import LiveContext


class AgentMCPBinding:
    """One minted registered token for this attempt's actual created agent; no retries."""

    def __init__(self, context: LiveContext) -> None:
        self.context = context
        self.engine: AsyncEngine | None = None
        self.sessions: async_sessionmaker[AsyncSession] | None = None
        self.agent: UUID | None = None
        self.jti: UUID | None = None
        self.token: str | None = None
        self.started = False

    def receipt(self, name: str, value: dict[str, object]) -> None:
        path = self.context.admission.output / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    async def bind(self, agent: ResourceRef) -> str:
        from daimon.testing.anthropic_live import LiveRefused, explicit_secret

        binding = self.context.admission.binding
        if self.started:
            raise LiveRefused(
                "F2 credential binding cannot be replayed or switched to another agent"
            )
        self.started = True
        if (
            agent.provider,
            agent.kind,
            agent.account_scope_id,
            agent.tenant_id,
            agent.account_id,
        ) != (
            "anthropic",
            "agent",
            binding.account_scope_id,
            binding.scope.tenant_id,
            binding.scope.account_id,
        ) or ("agent", agent.id) not in self.context.transport.authorization.created:
            raise LiveRefused("F2 credential requires this attempt's acknowledged agent")
        if binding.database_url is None or binding.mcp_signing_secret_file is None:
            raise LiveRefused("F2 requires explicit staging DB and MCP signing authority")
        secret = explicit_secret(binding.mcp_signing_secret_file)
        self.context.redaction_secrets += (secret, binding.database_url)
        self.agent = derive_agent_uuid(tenant_id=UUID(agent.tenant_id), ma_agent_id=agent.id)
        self.engine = create_async_engine(binding.database_url, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        account, tenant = UUID(agent.account_id), UUID(agent.tenant_id)
        async with self.sessions() as db:
            identity = await get_account_with_tenant(db, account_id=account)
            if identity is None or identity.tenant_id != tenant or identity.role.value != "user":
                raise LiveRefused("F2 token requires its actual isolated nonadmin account")
        now = datetime.now(UTC)
        ttl = min(900, self.context.admission.deadline_s)
        async with self.sessions.begin() as db:
            token = await mint_agent_mcp_token(
                db,
                account_id=account,
                tenant_id=tenant,
                agent_id=self.agent,
                label="F2-owned-session-boundary",
                secret=secret.encode(),
                now=now,
                ttl_days=ttl / 86400,  # pyright: ignore[reportArgumentType]
            )
            self.context.redaction_secrets += (token,)
            self.jti = token_jti(token)
            self.receipt(
                "f2-mcp-token-intent.json",
                {
                    "jti": str(self.jti),
                    "tenant_id": str(tenant),
                    "account_id": str(account),
                    "agent_id": str(self.agent),
                    "native_agent_id": agent.id,
                    "commit_outcome": "pending",
                    "token_exported": False,
                },
            )
        async with self.sessions() as db:
            row = await get_mcp_token(db, jti=self.jti)
            if (
                row is None
                or row.account_id != account
                or row.tenant_id != tenant
                or row.agent_id != str(self.agent)
                or row.kind != "agent"
                or row.scopes
                or row.revoked_at is not None
                or row.expires_at is None
                or not now < row.expires_at <= now + timedelta(seconds=ttl)
            ):
                raise LiveRefused("F2 agent-token registry readback differs from its ACK binding")
        self.token = token
        return token

    async def close(self) -> None:
        from daimon.testing.anthropic_live import LiveRefused

        try:
            if self.jti is not None:
                assert self.sessions is not None
                scope = self.context.admission.binding.scope
                async with self.sessions.begin() as db:
                    row = await get_mcp_token(db, jti=self.jti)
                    if row is not None:
                        if (
                            str(row.account_id) != scope.account_id
                            or str(row.tenant_id) != scope.tenant_id
                            or row.agent_id != str(self.agent)
                            or row.label != "F2-owned-session-boundary"
                        ):
                            raise LiveRefused("refuse foreign F2 token cleanup")
                        await revoke_mcp_token(db, jti=self.jti, now=datetime.now(UTC))
                async with self.sessions() as db:
                    after = await get_mcp_token(db, jti=self.jti)
                    if after is not None and after.revoked_at is None:
                        raise LiveRefused("F2 token revocation readback failed")
                self.receipt(
                    "f2-mcp-token-cleanup.json",
                    {
                        "jti": str(self.jti),
                        "revoked_or_absent_verified": True,
                        "token_exported": False,
                    },
                )
        finally:
            self.token = None
            if self.engine is not None:
                await self.engine.dispose()
