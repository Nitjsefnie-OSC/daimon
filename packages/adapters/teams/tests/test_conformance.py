"""Actual platform queue/DB controls; synthetic root callbacks do not certify C01."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from typing import cast

import pytest
from daimon.adapters.teams.app import TeamsApp
from daimon.adapters.teams.attachments import InboundFile
from daimon.adapters.teams.conformance import (
    AdmissionBarrier,
    MentionTurn,
    MultiHumanEnvelope,
    ThreadInputs,
    multi_human_evidence,
)
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.ma_identity import derive_tenant_uuid
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CHANNEL_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    FakeSender,
    bot_token,
    build_teams_runtime,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def pooled_factory(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    # Concurrent orchestration tails need separate pooled connections.
    del db_clean
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


async def test_teams_real_orchestration_db_last_reply_and_composed_activity_ids(
    pooled_factory: async_sessionmaker[AsyncSession],
) -> None:
    runtime = build_teams_runtime(pooled_factory)
    app = TeamsApp(runtime=runtime, sender=FakeSender(), commands={}, bot_token=bot_token)
    tenant_id = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
    seen: list[MentionTurn] = []

    async def run(turn: MentionTurn, barrier: AdmissionBarrier | None) -> None:
        seen.append(turn)
        if barrier is not None:
            barrier.started.set()
            await barrier.release.wait()

    def inbound(id_: str, root: str, user: str, text: str) -> TeamsInbound:
        return TeamsInbound(
            kind="channel",
            entra_tenant_id=ENTRA_TENANT_ID,
            user_id=user,
            conversation_id=root,
            channel_id=CHANNEL_ID,
            activity_id=id_,
            text=text,
            files=(
                InboundFile(
                    kind="shared_file",
                    name=f"input-{id_}.bin",
                    url=f"https://offline.invalid/{id_}",
                ),
            ),
            service_url=SERVICE_URL,
            team_id="team-test",
        )

    inputs = (
        ThreadInputs(
            inbound("1", "thread-a", AAD_OBJECT_ID, "initial-a"),
            (
                inbound("2", "thread-a", OTHER_AAD_OBJECT_ID, "b-first"),
                inbound("3", "thread-a", AAD_OBJECT_ID, "a-followup"),
                inbound("4", "thread-a", OTHER_AAD_OBJECT_ID, "b-second"),
            ),
        ),
        ThreadInputs(
            inbound("5", "thread-b", OTHER_AAD_OBJECT_ID, "initial-b"),
            (inbound("6", "thread-b", AAD_OBJECT_ID, "other-thread"),),
        ),
    )
    try:
        evidence = await multi_human_evidence(
            MultiHumanEnvelope(app, tenant_id, ENTRA_TENANT_ID, CHANNEL_ID, inputs, run)
        )
        assert [(t.inbound.activity_id, t.inbound.text, t.inbound.message_ids) for t in seen] == [
            ("1", "initial-a", ("1",)),
            ("4", "b-first\n\nb-second", ("2", "4")),
            ("3", "a-followup", ("3",)),
            ("5", "initial-b", ("5",)),
            ("6", "other-thread", ("6",)),
        ]
        assert all(t.tenant_id == tenant_id for t in seen)
        assert [[file.name for file in turn.inbound.files] for turn in seen] == [
            ["input-1.bin"],
            ["input-2.bin", "input-4.bin"],
            ["input-3.bin"],
            ["input-5.bin"],
            ["input-6.bin"],
        ]
        assert [[file.url for file in turn.inbound.files] for turn in seen] == [
            ["https://offline.invalid/1"],
            ["https://offline.invalid/2", "https://offline.invalid/4"],
            ["https://offline.invalid/3"],
            ["https://offline.invalid/5"],
            ["https://offline.invalid/6"],
        ]
        assert "offline.invalid" not in repr(evidence)
        assert all(
            batch["callback_returned"] is True
            for batch in cast(tuple[Mapping[str, object], ...], evidence["batches"])
        )

        assert evidence["peak_callbacks_by_thread"] == {"thread-a": 1, "thread-b": 1}
        assert evidence["queue_empty_after"] is evidence["processing_empty_after"] is True
        assert not app.turn_queue.in_flight() and (not app.turn_queue.depth())
        assert "_run_turn_guarded" not in app.__dict__
    finally:
        await runtime.anthropic.close()


@pytest.mark.parametrize("failure", ["exception", "timeout", "cancel", "early_return"])
async def test_exit_restores_handler_releases_ticket_and_preserves_failure(
    failure: str, pooled_factory: async_sessionmaker[AsyncSession]
) -> None:
    entered = asyncio.Event()
    runtime = build_teams_runtime(pooled_factory)
    app = TeamsApp(runtime=runtime, sender=FakeSender(), commands={}, bot_token=bot_token)
    anthropic = runtime.anthropic

    def inbound(id_: str, root: str, human: str) -> TeamsInbound:
        return TeamsInbound(
            kind="channel",
            entra_tenant_id=ENTRA_TENANT_ID,
            user_id=human,
            conversation_id=root,
            channel_id=CHANNEL_ID,
            activity_id=id_,
            text=id_,
            service_url=SERVICE_URL,
        )

    inputs = (
        ThreadInputs(
            inbound("1", "root-a", AAD_OBJECT_ID), (inbound("2", "root-a", OTHER_AAD_OBJECT_ID),)
        ),
        ThreadInputs(
            inbound("3", "root-b", OTHER_AAD_OBJECT_ID), (inbound("4", "root-b", AAD_OBJECT_ID),)
        ),
    )
    attr = "_run_turn_guarded"
    original = getattr(app, attr)

    async def run(_turn: MentionTurn, barrier: AdmissionBarrier | None) -> None:
        assert barrier is not None
        entered.set()
        if failure == "exception":
            raise LookupError("controlled root callback failure")
        if failure == "early_return":
            barrier.started.set()
            return
        await asyncio.Event().wait()

    try:
        probe = multi_human_evidence(
            MultiHumanEnvelope(
                app, uuid.uuid4(), ENTRA_TENANT_ID, CHANNEL_ID, inputs, run, timeout_s=1.0
            )
        )
        task = asyncio.create_task(probe)
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        if failure == "cancel":
            task.cancel()
        expected = {
            "exception": LookupError,
            "timeout": TimeoutError,
            "cancel": asyncio.CancelledError,
            "early_return": RuntimeError,
        }[failure]
        with pytest.raises(expected):
            await task
        assert getattr(app, attr) == original and attr not in app.__dict__
        assert not app._processing  # pyright: ignore[reportPrivateUsage]
        assert not app._pending  # pyright: ignore[reportPrivateUsage]
        assert not app.turn_queue.in_flight() and (not app.turn_queue.depth())
    finally:
        await anthropic.close()
