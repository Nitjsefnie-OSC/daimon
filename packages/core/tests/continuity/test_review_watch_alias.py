import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.core.continuity import watch_turn
from daimon.core.tool_safety import ToolCall, ToolSafetyPolicy
from daimon.testing import ma_agent, ma_session
from mux.contracts.ids import Scope


@pytest.mark.parametrize(
    "query",
    [
        "?extra=1",
        "?daimon_session=one&daimon_session=two",
        "?daimon_session=invalid",
        "?daimon_session=00000000-0000-0000-0000-000000000001&extra=1",
    ],
)
async def test_review_management_alias_cannot_survive(query, monkeypatch):
    snapshot = ma_session(
        agent=ma_agent(
            tools=[
                {
                    "type": "mcp_toolset",
                    "mcp_server_name": "renamed",
                    "configs": [],
                    "default_config": {
                        "enabled": True,
                        "permission_policy": {"type": "always_allow"},
                    },
                }
            ],
            mcp_servers=[
                {"name": "renamed", "type": "url", "url": "https://daimon.invalid/mcp" + query}
            ],
        )
    )
    update = AsyncMock()
    monkeypatch.setattr(watch_turn, "update_session_record", update)
    monkeypatch.setattr(watch_turn, "gate_session_snapshot", AsyncMock())
    await watch_turn.constrain_session(
        MagicMock(),
        snapshot,
        ToolSafetyPolicy(),
        scope=Scope(
            tenant_id=str(uuid.uuid4()),
            account_id=str(uuid.uuid4()),
            principal_id="owner",
            authorization_id="grant",
        ),
        public_url="https://daimon.invalid/mcp",
        sessions=MagicMock(),
        tenant_id=uuid.uuid4(),
    )
    verdict = await watch_turn.watch_posture(ToolSafetyPolicy()).decide(
        ToolCall(tool_use_id="bypass", server_name="renamed", tool_name="get_agent")
    )
    assert verdict.allow, "probe must show the retained alias can pass its read decision"
    assert update.await_args.kwargs["agent"]["tools"] == []
