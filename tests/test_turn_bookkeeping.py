"""Current Teams settlement behavior when its store transaction fails."""

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from daimon.adapters.teams import app as teams_app
from sqlalchemy.exc import SQLAlchemyError


@pytest.mark.parametrize("failing_step", ["retire", "clear"])
async def test_teams_settlement_logs_store_failure_and_stops_the_transaction(
    monkeypatch: pytest.MonkeyPatch, failing_step: str
) -> None:
    intent_id, marker_id = UUID(int=1), UUID(int=2)
    calls: list[object] = []

    class Transaction:
        def begin(self) -> "Transaction":
            return self

        async def __aenter__(self) -> "Transaction":
            calls.append("begin")
            return self

        async def __aexit__(self, exc_type: type[BaseException] | None, *_args: object) -> None:
            calls.append(("exit", exc_type))

    async def retire(_session: object, *, id: UUID, expected_message_id: str) -> None:
        calls.append(("retire", id, expected_message_id))
        if failing_step == "retire":
            raise SQLAlchemyError("retire failed")

    async def clear(_session: object, *, id: UUID) -> None:
        calls.append(("clear", id))
        if failing_step == "clear":
            raise SQLAlchemyError("clear failed")

    log_exception = MagicMock()
    monkeypatch.setattr(teams_app, "retire_turn_card_intent", retire)
    monkeypatch.setattr(teams_app, "clear_active_turn", clear)
    monkeypatch.setattr(teams_app, "log", SimpleNamespace(exception=log_exception))
    owner = SimpleNamespace(runtime=SimpleNamespace(sessionmaker=Transaction()))

    await teams_app.TeamsApp._settle(  # pyright: ignore[reportPrivateUsage]
        cast(teams_app.TeamsApp, owner), intent_id, "card-1", True, {marker_id}
    )

    expected: list[object] = ["begin", ("retire", intent_id, "card-1")]
    if failing_step == "clear":
        expected.append(("clear", marker_id))
    assert calls == [*expected, ("exit", SQLAlchemyError)]
    log_exception.assert_called_once_with("teams.turn.settle_failed", intent_id=str(intent_id))
