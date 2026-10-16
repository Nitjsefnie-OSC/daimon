"""Historical internal credentials for tests; never a production minter."""

from __future__ import annotations

import datetime as dt
import uuid

import jwt as pyjwt


def mint_internal_mcp_token(
    *,
    account_id: uuid.UUID,
    secret: bytes,
    now: dt.datetime,
) -> str:
    """Reproduce a retired credential only for negative/compatibility fixtures."""
    return pyjwt.encode(
        {
            "sub": str(account_id),
            "iat": int(now.timestamp()),
            "is_admin": True,
            "internal": True,
        },
        secret,
        algorithm="HS256",
    )
