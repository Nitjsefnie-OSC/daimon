"""Real Postgres, synthetic model/API, and live authorization decisions."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet, MultiFernet
from daimon.core.recruitment import (
    Candidate,
    Extraction,
    RecruitmentConfig,
    SourceMessage,
    ThreadState,
)
from daimon.core.recruitment_api import RecruitmentAPI, SyntheticRecruitmentTransport
from daimon.core.recruitment_service import RecruitmentService
from daimon.core.stores.recruitment import RecruitmentStore, is_recruitment_source
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture
async def store(
    db_session_factory: async_sessionmaker[AsyncSession], db_clean: None
) -> RecruitmentStore:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, workspace_id="101")
    result = RecruitmentStore(
        db_session_factory, MultiFernet([Fernet(Fernet.generate_key())]), tenant.id
    )
    await result.configure(
        RecruitmentConfig(guild_id="101", enabled=True, recruiter_user_ids=("7",))
    )
    return result


async def extract(
    messages: list[SourceMessage], primary: Candidate | None, options: dict[str, list[str]]
) -> Extraction:
    return Extraction(candidate=Candidate(name="Synthetic"), summary=messages[-1].content)


def message(id: str = "10", content: str = "Synthetic candidate") -> SourceMessage:
    return SourceMessage(
        id=id, content=content, observed_at=datetime.now(UTC), url=f"https://synthetic.invalid/{id}"
    )


async def test_stale_confirm_nonrecruiter_and_restart_retry(store: RecruitmentStore) -> None:
    allowed = True

    async def authorize(actor: str, thread: str) -> None:
        if not allowed or actor != "7":
            raise PermissionError("refused")

    fake = SyntheticRecruitmentTransport()
    async with httpx.AsyncClient(transport=fake) as http:
        api = RecruitmentAPI(http, "https://synthetic.invalid", SecretStr("fake"))
        service = RecruitmentService(store, api, extract, authorize, backend="synthetic")
        await service.enroll("20", "2", "  !LABS-staffing: Synthetic")
        await service.ingest("20", [message()])
        ready = await store.get("20")
        assert ready
        ready.scan_complete = True
        await store.save(ready, await service.config())
        old = await store.get("20")
        assert old and old.revision
        await service.ingest("20", [message(content="Synthetic corrected")])
        state = await store.get("20")
        assert state and state.revision != old.revision
        with pytest.raises(ValueError, match="stale"):
            await service.confirm("20", old.revision, "create", "7", "i1")
        with pytest.raises(PermissionError):
            await service.confirm("20", state.revision, "create", "8", "i2")
        assert not fake.effects
        await service.confirm("20", state.revision, "create", "7", "i3")
        fake.fail = 503
        await service.deliver("20", force=True)
        pending = await store.get("20")
        assert pending and pending.outbox_id
        item = await store.outbox(pending.outbox_id)
        assert item and item.attempts == 1
        allowed = False
        fake.fail = None
        restarted = RecruitmentService(store, api, extract, authorize, backend="synthetic")
        await restarted.deliver("20", force=True)
        assert not fake.effects
        allowed = True
        await restarted.deliver("20", force=True)
        assert len(fake.effects) == 1
        state = await store.get("20")
        assert state and state.candidate_id and state.phase == "active"
        # A restart cannot reapply completed effects.
        await restarted.deliver("20", force=True)
        assert len(fake.effects) == 1


async def test_tenant_filter_and_retention_tombstones(store: RecruitmentStore) -> None:
    await store.save(
        ThreadState(thread_id="20", parent_id="2"),
        await store.configuration(),
    )
    await store.ingest("20", message())
    other = RecruitmentStore(store.sessions, store.fernet, uuid4())
    assert await other.get("20") is None
    assert await other.messages("20") == []
    assert not await other.enrolled("20")
    assert not await is_recruitment_source(store.sessions, other.tenant_id, "20")
    with pytest.raises(LookupError):
        await other.ingest("20", message())
    await other.purge(datetime.now(UTC) + timedelta(days=99))
    assert await store.messages("20")
    await store.purge(datetime.now(UTC) + timedelta(days=31))
    assert await store.messages("20") == []
    state = await store.get("20")
    assert state and state.phase == "expired"
    assert await is_recruitment_source(store.sessions, store.tenant_id, "20")
