"""Build a caller from the native Teams admin-team owner check."""

from __future__ import annotations

import uuid

from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.core.native_admins import AdminCaller


async def admin_caller(
    runtime: TeamsRuntime,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    is_admin: bool,
    fresh: bool = False,
) -> AdminCaller:
    return AdminCaller(platform_user_id=user_id, is_server_admin=is_admin)
