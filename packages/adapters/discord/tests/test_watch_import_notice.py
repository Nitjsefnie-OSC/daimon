"""Imported watch notices use the existing private member-notice transport."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.discord.budget_notice import send_member_notice
from daimon.core.config import DirectMessagePolicy
from daimon.core.continuity.watch_imports import UNMAPPED_NOTICE


@pytest.mark.parametrize("closed,allowed", [(False, True), (True, True), (False, False)])
async def test_import_notice_is_owner_only_dm_with_no_channel_fallback(closed, allowed):
    tenant_id = uuid.uuid4()
    policy = DirectMessagePolicy() if allowed else DirectMessagePolicy(mode="disabled")
    runtime = SimpleNamespace(settings=SimpleNamespace(direct_message_policies={tenant_id: policy}))
    dm = SimpleNamespace(send=AsyncMock())
    open_dm = AsyncMock(return_value=dm)
    result = await send_member_notice(
        runtime,
        open_dm,
        lambda: closed,
        tenant_id=tenant_id,
        workspace_id="100",
        recipient_id="123",
        text=UNMAPPED_NOTICE,
    )
    assert result is (allowed and not closed)
    if result:
        open_dm.assert_awaited_once_with(100, 123)
        assert dm.send.await_args.args == (UNMAPPED_NOTICE,)
        assert dm.send.await_args.kwargs["allowed_mentions"].everyone is False
    else:
        open_dm.assert_not_called()
