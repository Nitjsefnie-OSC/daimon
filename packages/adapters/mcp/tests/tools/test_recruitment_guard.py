"""One tenant predicate protects direct reads, parent starters and rename ingress."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.mcp.tools.discord._recruitment import recruitment_message, recruitment_thread
from daimon.core.recruitment import RecruitmentConfig, ThreadState
from daimon.core.stores.recruitment import RecruitmentStore
from daimon.testing.factories import make_tenant


async def test_qualifying_and_persisted_sources_survive_disable_and_rename(
    db_session_factory, db_clean
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, workspace_id="101")
    store = RecruitmentStore(
        db_session_factory, MultiFernet([Fernet(Fernet.generate_key())]), tenant.id
    )
    config = RecruitmentConfig(guild_id="101", enabled=False, channel_ids=("2",))
    await store.configure(config)
    runtime = SimpleNamespace(session_factory=db_session_factory)
    assert await recruitment_thread(
        runtime, "101", "20", title=" !LABS-staffing: Synthetic", parent_id="2"
    )
    assert not await recruitment_thread(
        runtime, "101", "20", title=" !LABS-staffing: Synthetic", parent_id="3"
    )
    await store.save(ThreadState(thread_id="20", parent_id="2"), config)
    assert await recruitment_thread(runtime, "101", "20", title="Prefix removed", parent_id="2")
    assert not await recruitment_thread(runtime, "102", "20")
    message = MagicMock(spec=discord.Message)
    message.id = 20
    message.channel = MagicMock(spec=discord.TextChannel)
    assert await recruitment_message(runtime, "101", message)  # parent starter
    await store.purge(datetime.max.replace(tzinfo=UTC))
    assert await recruitment_thread(runtime, "101", "20")  # tombstone remains private
