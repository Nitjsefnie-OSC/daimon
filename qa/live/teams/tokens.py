# pyright: basic
"""Persona-isolated MSAL caches; this module never initiates login."""

from __future__ import annotations

import base64
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import msal

from qa.live.teams.config import TeamsQaConfig


def token_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload locally, without verifying it; never log the result."""
    try:
        payload = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except (IndexError, ValueError, UnicodeDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


def access_scopes(token: str) -> set[str]:
    """The delegated scopes an access token carries."""
    return set(str(token_claims(token).get("scp", "")).split())


class Secret:
    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return "<redacted>"


class ReauthRequired(Exception):
    def __init__(self, persona: str, reason: str) -> None:
        super().__init__(f"{persona}: reauthentication required ({reason})")


class AuthClient(Protocol):
    """The two MSAL auth-code calls used by login."""

    def initiate_auth_code_flow(
        self, scopes: list[str], *, redirect_uri: str, login_hint: str, prompt: str
    ) -> dict[str, object]: ...

    def acquire_token_by_auth_code_flow(
        self, flow: dict[str, object], response: dict[str, str]
    ) -> dict[str, Any]: ...


class TokenStore:
    def __init__(self, config: TeamsQaConfig) -> None:
        self.config = config

    def path(self, persona: str) -> Path:
        """Return the validated persona's private cache filename."""
        if persona not in self.config.personas:
            raise ValueError("unknown persona")
        return self.config.token_dir / f"{persona}.json"

    def _client(
        self, persona: str
    ) -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache]:
        cache = msal.SerializableTokenCache()
        path = self.path(persona)
        if path.exists():
            cache.deserialize(path.read_text())
        client = msal.PublicClientApplication(
            self.config.client_id,
            authority=f"https://login.microsoftonline.com/{self.config.tenant_id}",
            token_cache=cache,
        )
        return client, cache

    def client_for_login(self, persona: str) -> tuple[AuthClient, msal.SerializableTokenCache]:
        """Expose only the MSAL auth-code interface to the login driver."""
        client, cache = self._client(persona)
        return cast(AuthClient, client), cache

    def save(self, persona: str, cache: msal.SerializableTokenCache) -> None:
        """Atomically replace a mode 0600 persona cache."""
        path = self.path(persona)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".cache-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(cache.serialize())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)

    def validate(self, persona: str, result: dict[str, Any]) -> Secret:
        """Check the token belongs to this persona before it is stored or used.

        Interactive results carry ID-token claims; silent refreshes do not, so
        those are checked against the Graph access token's own claims.
        """
        token = result.get("access_token")
        if not isinstance(token, str):
            raise ReauthRequired(persona, "access token missing")
        claims = result.get("id_token_claims")
        if not isinstance(claims, dict):
            claims = token_claims(token)
        self._check_identity(persona, claims)
        return Secret(token)

    def _check_identity(self, persona: str, claims: dict[str, Any]) -> None:
        identity = self.config.personas[persona]
        if not claims:
            raise ReauthRequired(persona, "identity claims missing")
        if claims.get("tid") != self.config.tenant_id:
            raise ReauthRequired(persona, "tenant mismatch")
        upn = claims.get("preferred_username") or claims.get("upn") or ""
        if str(upn).casefold() != identity.upn.casefold():
            raise ReauthRequired(persona, "account mismatch")
        if identity.expected_oid and claims.get("oid") != identity.expected_oid:
            raise ReauthRequired(persona, "object mismatch")

    def acquire(self, persona: str, scopes: list[str]) -> Secret:
        """Acquire silently from exactly one matching persona account."""
        client, cache = self._client(persona)
        accounts = [
            account
            for account in client.get_accounts()
            if str(account.get("username", "")).casefold()
            == self.config.personas[persona].upn.casefold()
            and account.get("realm") == self.config.tenant_id
        ]
        if len(accounts) != 1:
            raise ReauthRequired(persona, "account unavailable")
        try:
            result = client.acquire_token_silent(scopes, account=accounts[0])
        except Exception:
            raise ReauthRequired(persona, "silent acquisition failed") from None
        if not isinstance(result, dict) or "error" in result:
            raise ReauthRequired(persona, "silent acquisition failed")
        secret = self.validate(persona, result)
        if cache.has_state_changed:
            self.save(persona, cache)
        return secret

    def status(self, persona: str) -> dict[str, Any]:
        """Return local cache metadata with no token material."""
        client, cache = self._client(persona)
        accounts = client.get_accounts()
        matching = [
            a
            for a in accounts
            if str(a.get("username", "")).casefold() == self.config.personas[persona].upn.casefold()
        ]
        account = matching[0] if len(matching) == 1 else {}
        entries = json.loads(cache.serialize()) if self.path(persona).exists() else {}
        access = list(entries.get("AccessToken", {}).values())
        refresh = list(entries.get("RefreshToken", {}).values())
        scopes = sorted(
            {scope for entry in access for scope in access_scopes(str(entry.get("secret", "")))}
        )
        expiry = max((int(entry.get("expires_on", 0)) for entry in access), default=0)
        return {
            "account_upn": account.get("username"),
            "oid": account.get("local_account_id"),
            "tid": account.get("realm"),
            "scopes_granted": scopes,
            "expiry": datetime.fromtimestamp(expiry, UTC).isoformat() if expiry else None,
            "refresh_token_present": bool(refresh),
        }

    def logout(self, persona: str) -> None:
        """Remove one persona's local cache."""
        self.path(persona).unlink(missing_ok=True)
