"""Support requests use the configured escalation channel."""

from __future__ import annotations

import uuid

from daimon.core.support_routing import support_recipient_tiers


async def test_support_request_has_no_direct_message_recipients() -> None:
    assert (
        await support_recipient_tiers(
            None,  # type: ignore[arg-type]
            tenant_id=uuid.uuid4(),
            platform="discord",
            channel_id="channel",
            requester_id="requester",
        )
        == ()
    )
