"""Caller-owned MCP vaults created only for an authorized native session request."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from pydantic import JsonValue

from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.resources import CredentialBinding, ProviderBinding
from mux.drivers.openai._common import Context, objects, text
from mux.drivers.openai.mcp_auth import MCPSecretResolver, decode, non_mcp_tool, tool
from mux.drivers.openai.transport import Object
from mux.drivers.openai.vaults import OpenAIVaults, SecretResolver
from mux.errors import ProviderError, ScopeViolation

VAULT = "mcp_vault"
NAME = "mcp_vault_name"
CREDENTIAL = "mcp_vault_credential"


@dataclass(frozen=True)
class SessionMCPVault:
    vault: ResourceRef
    name: str
    credential_id: str | None = None

    def refs(self) -> dict[str, str]:
        return {
            VAULT: self.vault.id,
            NAME: self.name,
            **({CREDENTIAL: self.credential_id} if self.credential_id else {}),
        }

    async def delete(self, context: Context, scope: Scope, *, key: str) -> None:
        port = _port(context, scope, {self.vault.id})
        actual = await port.retrieve(scope, self.vault)
        if actual.name != self.name:
            raise ScopeViolation(self.vault.id, "session MCP vault identity changed")
        await port.delete(scope, self.vault, key=key)


def _port(
    context: Context, scope: Scope, owned: set[str], resolver: SecretResolver | None = None
) -> OpenAIVaults:
    # This private child grant derives from the already-authorized session create.
    # No inventory ID/name grants access: only this request's native ACK is added.
    private = Context(
        context.transport,
        context.account_scope_id,
        context.profile_id,
        lambda candidate, kind, identity: (
            candidate == scope and kind == "vault" and (identity is None or identity in owned)
        ),
    )
    return OpenAIVaults(private, resolver)


def from_binding(
    context: Context, scope: Scope, binding: ProviderBinding | None
) -> SessionMCPVault | None:
    if binding is None or VAULT not in binding.native_refs:
        return None
    if (
        binding.thread.channel.tenant_id != scope.tenant_id
        or binding.legacy_account_id != scope.account_id
        or binding.provider != "openai"
        or binding.profile != context.profile_id
    ):
        raise ScopeViolation(binding.id, "session MCP vault has a foreign owner")
    return SessionMCPVault(
        context.ref(scope, "vault", text(binding.native_refs[VAULT])),
        text(binding.native_refs[NAME]),
        binding.native_refs.get(CREDENTIAL),
    )


def valid_secret(value: object) -> str:
    if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
        raise ProviderError("permission", retryable=False, native_code="mcp_secret_unavailable")
    return value


async def prepare(
    context: Context,
    scope: Scope,
    agent: Object,
    resolver: MCPSecretResolver | None,
    *,
    key: str,
    vaults_attached: bool,
) -> tuple[list[JsonValue] | None, SessionMCPVault | None]:
    connections = decode(agent, context)
    if not connections:
        return None, None
    if resolver is None:
        raise context.unsupported("host_mcp_secret_resolver")
    if vaults_attached:
        raise context.unsupported("mixed_mcp_authentication")
    if scope.is_platform or scope.is_legacy_host_authorized:
        raise context.unsupported("caller_scoped_mcp_vault")
    connection = connections[0]
    reference = connection.credential_ref
    if reference is None:
        raise ValueError("missing MCP credential reference")
    try:
        secret = valid_secret(await resolver(scope, reference, connection.url))
    except Exception:
        raise ProviderError(
            "permission", retryable=False, native_code="mcp_secret_unavailable"
        ) from None
    # Validate every copied tool before allocating any provider-held credential.
    tools: list[JsonValue] = [
        non_mcp_tool(value) for value in objects(agent["tools"]) if value.get("type") != "mcp"
    ]
    expected = tool(connection, context)
    owned: set[str] = set()

    async def resolve(candidate: Scope, credential_ref: str) -> str:
        if candidate != scope or credential_ref != reference:
            raise ScopeViolation(credential_ref, "MCP credential has a foreign scope")
        return secret

    port = _port(context, scope, owned, resolve)
    digest = hashlib.sha256(f"{scope.tenant_id}\0{scope.account_id}\0{key}".encode()).hexdigest()
    name = "mux-session-mcp-" + digest
    vault = await port.ensure(scope, name, key=key + ":mcp-vault")
    owned.add(vault.ref.id)
    managed = SessionMCPVault(vault.ref, name)
    try:
        credential = await port.put(
            scope,
            vault.ref,
            CredentialBinding(
                name=connection.name,
                kind="static_bearer",
                credential_ref=reference,
                mcp_server_url=connection.url,
            ),
            expected=None,
            key=key + ":mcp-credential",
        )
    except BaseException:
        # The ACK-owned vault is private to this request, including a credential
        # whose PUT outcome was uncertain. Deleting it revokes that whole child.
        await managed.delete(context, scope, key=key + ":mcp-cleanup")
        raise
    managed = SessionMCPVault(vault.ref, name, credential.id)
    tools.append({**expected, "credential_id": credential.id, "connection_origin": "service"})
    return tools, managed
