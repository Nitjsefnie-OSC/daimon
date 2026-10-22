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
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


@pytest.fixture
async def store(db_nullpool_engine: AsyncEngine, db_clean: None) -> RecruitmentStore:
    db_session_factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
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


async def test_update_needs_fresh_confirmation_and_keeps_both_fields(
    store: RecruitmentStore,
) -> None:
    from daimon.core.recruitment import ProposedChange, ThreadState
    from daimon.core.recruitment_api_models import MatchResponse

    async def authorize(actor: str, thread: str) -> None:
        if actor != "7":
            raise PermissionError

    async def extractor(messages, primary, options):
        latest = messages[-1]
        field = "phone" if "phone" in latest.content else "position"
        return Extraction(
            summary=latest.content,
            changes=[
                ProposedChange(
                    field=field,
                    operation="set",
                    value=latest.content,
                    source_message_id=latest.id,
                    evidence=latest.content,
                )
            ],
        )

    config = await store.configuration()
    assert config
    await store.save(
        ThreadState(
            thread_id="20",
            parent_id="2",
            phase="active",
            candidate=Candidate(name="Synthetic"),
            candidate_id=str(uuid4()),
            matches=MatchResponse(status="none", candidates=[]).model_dump(mode="json"),
            scan_complete=True,
        ),
        config,
    )
    fake = SyntheticRecruitmentTransport()
    async with httpx.AsyncClient(transport=fake) as http:
        service = RecruitmentService(
            store,
            RecruitmentAPI(http, "https://synthetic.invalid", SecretStr("fake")),
            extractor,
            authorize,
            backend="synthetic",
        )
        await service.ingest(
            "20", [message("10", "phone 555"), message("11", "position Synthetic")]
        )
        state = await store.get("20")
        assert state and {c["field"] for c in state.changes} == {"phone", "position"}
        await service.deliver("20", force=True)
        assert not fake.effects
        await service.confirm("20", state.revision, "update", "7", "i1")
        await service.deliver("20", force=True)
        assert len(fake.effects) == 1
        state = await store.get("20")
        assert state and state.candidate and state.candidate.phone == "phone 555"


async def test_source_commit_invalidates_summary_even_if_worker_dies(
    store: RecruitmentStore,
) -> None:
    from daimon.core.recruitment import ThreadState

    config = await store.configuration()
    assert config
    await store.save(
        ThreadState(
            thread_id="20",
            parent_id="2",
            phase="review",
            revision="old",
            candidate=Candidate(name="Synthetic"),
        ),
        config,
    )
    await store.ingest("20", message())
    state = await store.get("20")
    assert state and state.revision != "old" and state.error == "refresh_required"


async def test_old_edit_reconciliation_resumes_pages_after_restart(store: RecruitmentStore) -> None:
    from daimon.core.recruitment_runtime import reconcile_thread

    class Discord:
        def __init__(self):
            self.edited = False

        async def bot_id(self):
            return "bot"

        async def check_access(self, *args, **kwargs):
            pass

        async def message(self, *args):
            return None

        async def history(self, config, thread, *, before=None, after=None):
            if after is not None:
                return [], None, after
            if before:
                return [message("1")], None, "1"
            values = [
                message(str(i), "Synthetic OLD EDIT" if self.edited and i == 2 else "Synthetic")
                for i in range(2, 102)
            ]
            # Stable source timestamps unless an actual edit happened.
            values = [
                v.model_copy(update={"observed_at": datetime(2026, 10, 1, tzinfo=UTC)})
                for v in values
            ]
            return values, "2", "101"

    async def authorize(actor, thread):
        pass

    fake = SyntheticRecruitmentTransport()
    discord = Discord()
    async with httpx.AsyncClient(transport=fake) as http:
        api = RecruitmentAPI(http, "https://synthetic.invalid", SecretStr("fake"))
        service = RecruitmentService(store, api, extract, authorize, backend="synthetic")
        await service.enroll("20", "2", "!labs-staffing: Synthetic")
        await reconcile_thread(service, discord, "20")
        first = await store.get("20")
        assert first and first.scan_before == "2" and not first.scan_complete
        with pytest.raises(ValueError):
            await service.confirm("20", first.revision, "create", "7", "incomplete")
        restarted = RecruitmentService(store, api, extract, authorize, backend="synthetic")
        await reconcile_thread(restarted, discord, "20")
        complete = await store.get("20")
        assert complete and complete.scan_complete and complete.scan_before is None
        discord.edited = True
        await reconcile_thread(restarted, discord, "20")
        changed = await store.get("20")
        assert changed and changed.revision != complete.revision
        assert any(m.id == "2" and "OLD EDIT" in m.content for m in await store.messages("20"))
        assert not fake.effects


async def test_timeout_after_apply_keeps_exact_body_and_key(store: RecruitmentStore) -> None:
    fake = SyntheticRecruitmentTransport()
    lose_response = True

    class Lossy(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal lose_response
            response = await fake.handle_async_request(request)
            if not request.url.path.endswith("/match") and lose_response:
                lose_response = False
                raise httpx.ReadTimeout("synthetic response loss", request=request)
            return response

    async def authorize(actor, thread):
        pass

    async with httpx.AsyncClient(transport=Lossy()) as http:
        service = RecruitmentService(
            store,
            RecruitmentAPI(http, "https://synthetic.invalid", SecretStr("fake")),
            extract,
            authorize,
            backend="synthetic",
        )
        await service.enroll("20", "2", "!labs-staffing: Synthetic")
        await service.ingest("20", [message()])
        state = await store.get("20")
        assert state
        state.scan_complete = True
        await store.save(state, await service.config())
        await service.confirm("20", state.revision, "create", "7", "i1")
        state = await store.get("20")
        assert state and state.outbox_id
        before = await store.outbox(state.outbox_id)
        await service.deliver("20", force=True)
        with pytest.raises(ValueError, match="uncertain"):
            await service.discard("20", "7")
        await service.deliver("20", force=True)
        after = await store.outbox(state.outbox_id)
        assert before and after and before.key == after.key and before.payload == after.payload
        assert after.state == "completed" and len(fake.effects) == 1


async def test_gateway_edit_fences_inflight_worker_and_remains_pending(
    store: RecruitmentStore,
) -> None:
    from daimon.core.recruitment import ThreadState
    from daimon.core.stores.recruitment import RecruitmentBusy

    config = await store.configuration()
    assert config
    await store.save(ThreadState(thread_id="20", parent_id="2", revision="old"), config)
    inbox = RecruitmentStore(store.sessions, store.fernet, store.tenant_id)
    async with store.claim("20"):
        stale = await store.get("20")
        assert stale
        await inbox.ingest("20", message())
        with pytest.raises(RecruitmentBusy, match="claim_expired"):
            await store.save(stale, config)
    assert await store.messages("20", pending_only=True)
    current = await store.get("20")
    assert current and current.revision != "old"


async def test_disabled_scheduler_still_erases_expired_state(store: RecruitmentStore) -> None:
    from unittest.mock import AsyncMock

    from daimon.core.config import Settings
    from daimon.core.recruitment import ThreadState
    from daimon.core.recruitment_runtime import reconcile_recruitment
    from sqlalchemy import text

    config = await store.configuration()
    assert config
    await store.save(
        ThreadState(thread_id="20", parent_id="2", candidate=Candidate(name="Synthetic")), config
    )
    async with store.sessions.begin() as session:
        await session.execute(
            text("UPDATE recruitment_threads SET expires_at=:expired WHERE tenant_id=:tenant"),
            {"expired": datetime.now(UTC) - timedelta(days=1), "tenant": store.tenant_id},
        )
    settings = Settings(_env_file=None)
    assert not settings.recruitment.enabled
    await reconcile_recruitment(store.sessions, settings, store.fernet, AsyncMock())
    state = await store.get("20")
    assert state and state.phase == "expired" and state.candidate is None
