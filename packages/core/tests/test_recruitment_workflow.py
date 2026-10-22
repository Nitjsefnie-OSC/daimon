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
from daimon.core.stores.recruitment import RecruitmentStore
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


@pytest.fixture
async def store(db_nullpool_engine: AsyncEngine, db_clean: None) -> RecruitmentStore:
    db_session_factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, workspace_id="101")
    result = RecruitmentStore(
        db_session_factory,
        MultiFernet([Fernet(Fernet.generate_key())]),
        tenant.id,
        RecruitmentConfig(
            guild_id="101", enabled=True, recruiter_user_ids=("7",), channel_ids=("2",)
        ),
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
        ThreadState(thread_id="20", parent_id="2", card_id="30"),
        await store.configuration(),
    )
    await store.ingest("20", message())
    other = RecruitmentStore(store.sessions, store.fernet, uuid4())
    assert await other.get("20") is None
    assert await other.messages("20") == []
    assert not await other.enrolled("20")
    assert "20" not in await other.list_threads()
    with pytest.raises(LookupError):
        await other.ingest("20", message())
    await other.purge(datetime.now(UTC) + timedelta(days=99))
    assert await store.messages("20")
    await store.purge(datetime.now(UTC) + timedelta(days=31))
    assert await store.messages("20") == []
    state = await store.get("20")
    assert state and state.phase == "expired" and state.card_id == "30"
    assert "20" in await store.list_threads()


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
    inbox = RecruitmentStore(store.sessions, store.fernet, store.tenant_id, store.config)
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
    settings = Settings(
        database={"url": "postgresql+asyncpg://unused:unused@localhost/daimon_test_nc_staffing"},
        _env_file=None,
    )
    assert not settings.recruitment.enabled
    # Cleanup remains independent of both enablement and missing crypto keys.
    expired = await store.get("20")
    assert expired and expired.phase == "expired"
    await reconcile_recruitment(store.sessions, settings, None, AsyncMock())
    state = await store.get("20")
    assert state and state.phase == "expired" and state.candidate is None


@pytest.mark.parametrize(
    "later_error", ["validation_error", "payload_too_large", "candidate_match_conflict"]
)
async def test_accepted_timeout_then_rejection_never_allows_discard(store, later_error) -> None:
    from daimon.adapters.discord.recruitment_cards import review_card

    fake = SyntheticRecruitmentTransport()
    attempts = 0

    class Mixed(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal attempts
            if request.url.path.endswith("/match"):
                return await fake.handle_async_request(request)
            attempts += 1
            if attempts == 1:
                await fake.handle_async_request(request)
                raise httpx.ReadTimeout("accepted then lost", request=request)
            return httpx.Response(400, json={"code": later_error})

    async def authorize(actor, thread):
        pass

    async with httpx.AsyncClient(transport=Mixed()) as http:
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
        state.scan_complete = True
        await store.save(state, await service.config())
        await service.confirm("20", state.revision, "create", "7", "mixed")
        await service.deliver("20", force=True)
        await service.deliver("20", force=True)
        state = await store.get("20")
        item = await store.outbox(state.outbox_id)
        assert item.possibly_applied and len(fake.effects) == 1
        # Reloading is the restart boundary; the latest HTTP error cannot clear uncertainty.
        with pytest.raises(ValueError, match="uncertain"):
            await service.discard("20", "7")
        _, view = review_card(state, item)
        assert next(b for b in view.children if b.label == "Discard").disabled


@pytest.mark.parametrize(
    "event",
    ["parent_edit", "empty_edit", "delete", "bulk_delete", "parent_delete", "parent_bulk_delete"],
)
async def test_raw_events_fence_review_before_discord_enrichment(store, event) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import discord
    from daimon.adapters.discord.recruitment import RecruitmentCog
    from daimon.core.recruitment_discord import DiscordMessage

    async def authorize(actor, thread):
        pass

    async with httpx.AsyncClient(transport=SyntheticRecruitmentTransport()) as http:
        service = RecruitmentService(
            store,
            RecruitmentAPI(http, "https://synthetic.invalid", SecretStr("fake")),
            extract,
            authorize,
            backend="synthetic",
        )
        await service.enroll("20", "2", "!labs-staffing: Synthetic")
        source = message("20" if event.startswith("parent_") else "10", "Synthetic old email")
        await service.ingest("20", [source])
        state = await store.get("20")
        state.scan_complete = True
        await store.save(state, await service.config())
        old_revision = state.revision
        bot = MagicMock()

        async def fetch(channel_id):
            # This is the first async Discord operation: the event must already
            # have made the old review unconfirmable, even if this read fails.
            with pytest.raises(ValueError):
                await service.confirm("20", old_revision, "create", "7", "stale")
            raise discord.Forbidden(MagicMock(status=403, reason="synthetic"), "denied")

        bot.fetch_channel = AsyncMock(side_effect=fetch)
        runtime = SimpleNamespace(
            sessionmaker=store.sessions,
            settings=SimpleNamespace(
                recruitment=__import__(
                    "daimon.core.config", fromlist=["RecruitmentSettings"]
                ).RecruitmentSettings(
                    enabled=True,
                    tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}},
                )
            ),
        )
        cog = RecruitmentCog(bot, runtime)
        payload = SimpleNamespace(
            guild_id=101,
            channel_id=2 if event.startswith("parent_") else 20,
            message_id=int(source.id),
            data={"content": "", "edited_timestamp": datetime.now(UTC).isoformat()},
            message_ids={int(source.id)},
        )
        if event in {"delete", "parent_delete"}:
            await cog.on_raw_message_delete(payload)
        elif event in {"bulk_delete", "parent_bulk_delete"}:
            await cog.on_raw_bulk_message_delete(payload)
        else:
            await cog.on_raw_message_edit(payload)
        with pytest.raises(ValueError):
            await service.confirm("20", old_revision, "create", "7", "stale-after")
        if event in {"parent_edit", "empty_edit"}:
            assert await store.pending_enrichment("20")
            blank = DiscordMessage(
                id=source.id,
                content="",
                author={"id": "7"},
                timestamp=source.observed_at,
                edited_timestamp=datetime.fromisoformat(payload.data["edited_timestamp"]),
            ).source("101", str(payload.channel_id))
            assert blank is not None and not blank.content.strip()
            await store.ingest("20", blank)
            assert not await store.pending_enrichment("20")
        else:
            assert any(m.deleted for m in await store.messages("20", pending_only=True))


async def test_new_edit_after_raw_purge_invalidates_derived_review(store) -> None:
    from daimon.core.recruitment import ThreadState

    config = await store.configuration()
    await store.save(
        ThreadState(thread_id="20", parent_id="2", phase="review", revision="old"), config
    )
    original = message("10", "Synthetic old email")
    await store.ingest("20", original)
    before = await store.get("20")
    await store.purge(datetime.now(UTC) + timedelta(days=8))
    assert await store.messages("20") == []
    assert not await store.ingest("20", original)  # never resurrect unchanged erased text
    newer = original.model_copy(
        update={
            "content": "Synthetic corrected email",
            "observed_at": original.observed_at + timedelta(days=9),
        }
    )
    assert await store.ingest("20", newer)
    after = await store.get("20")
    assert after.phase != "expired" and after.revision != before.revision
    assert after.error == "refresh_required"
    assert (await store.messages("20"))[0].content == newer.content


def test_privacy_settings_are_authoritative_without_io():
    from daimon.core.config import RecruitmentSettings

    settings = RecruitmentSettings(
        tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}}
    )
    for _ in range(20):
        assert settings.is_recruitment_channel("101", "20", "2")
        assert settings.is_recruitment_channel("101", "2", "")
        assert settings.is_recruitment_channel("101", "20", None)
        assert not RecruitmentSettings().is_recruitment_channel("101", "20", None)
    # Rename, prefix removal, enrollment and retention cannot change this boundary.
    assert not settings.is_recruitment_channel("102", "20", "2")


async def test_partial_event_does_not_repopulate_unchanged_erased_evidence(store) -> None:
    from daimon.core.stores.recruitment import invalidate_source

    config = await store.configuration()
    await store.save(ThreadState(thread_id="20", parent_id="2", phase="review"), config)
    original = message("10", "Synthetic original")
    await store.ingest("20", original)
    await store.purge(datetime.now(UTC) + timedelta(days=8))
    before = await store.get("20")
    await invalidate_source(store.sessions, store.tenant_id, "20", "10")
    assert await store.pending_enrichment("20")
    assert not await store.ingest("20", original)
    assert not await store.pending_enrichment("20")
    assert await store.messages("20") == []
    state = await store.get("20")
    assert state.seen_watermark > state.covered_watermark or state.revision != before.revision


async def test_offline_deletion_after_raw_purge_uses_content_free_manifest(store) -> None:
    config = await store.configuration()
    await store.save(
        ThreadState(
            thread_id="20",
            parent_id="2",
            phase="active",
            candidate=Candidate(name="Synthetic"),
            candidate_id=str(uuid4()),
        ),
        config,
    )
    await store.ingest("20", message("10", "Synthetic erased email"))
    before = await store.get("20")
    await store.purge(datetime.now(UTC) + timedelta(days=8))
    assert await store.messages("20") == []
    assert [id for id, _, _ in await store.message_manifest("20")] == ["10"]

    async def authorize(actor, thread):
        pass

    async with httpx.AsyncClient(transport=SyntheticRecruitmentTransport()) as http:
        service = RecruitmentService(
            store,
            RecruitmentAPI(http, "https://synthetic.invalid", SecretStr("fake")),
            extract,
            authorize,
            backend="synthetic",
        )
        await service.delete_message("20", "10")
    state = await store.get("20")
    assert state.seen_watermark > before.seen_watermark
    assert (await store.message_manifest("20"))[0][2] is True


@pytest.mark.parametrize("parent", [False, True])
async def test_new_message_fences_confirmation_before_first_enrichment(store, parent):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    import discord
    from daimon.adapters.discord.recruitment import RecruitmentCog
    from daimon.core.config import RecruitmentSettings

    async def authorize(actor, thread):
        pass

    async with httpx.AsyncClient(transport=SyntheticRecruitmentTransport()) as http:
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
        state.scan_complete = True
        await store.save(state, await service.config())
        settings = RecruitmentSettings(
            enabled=True, tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}}
        )
        cog = RecruitmentCog(
            MagicMock(),
            SimpleNamespace(
                sessionmaker=store.sessions, settings=SimpleNamespace(recruitment=settings)
            ),
        )

        async def enrichment(*args, **kwargs):
            with pytest.raises(ValueError, match="stale"):
                await service.confirm("20", state.revision, "create", "7", "racing-create")
            assert await store.pending_enrichment("20")

        cog.ingest_message = AsyncMock(side_effect=enrichment)
        incoming = MagicMock(spec=discord.Message)
        incoming.author.bot = False
        incoming.guild.id = 101
        incoming.channel = MagicMock(spec=discord.TextChannel if parent else discord.Thread)
        incoming.channel.id, incoming.channel.parent_id = (2 if parent else 20), 2
        incoming.id = 20 if parent else 11
        incoming.edited_at = None
        incoming.created_at = datetime.now(UTC)
        await cog.on_message(incoming)
        with pytest.raises(ValueError, match="stale"):
            await service.confirm("20", state.revision, "create", "7", "after-create")
        current = await store.get("20")
        assert current.seen_watermark > current.covered_watermark
        # Recovery accepts the actual timestamp, even though the event arrived later.
        assert await store.ingest(
            "20", message(str(incoming.id)).model_copy(update={"observed_at": incoming.created_at})
        )


@pytest.mark.parametrize("known_card", [True, False])
async def test_own_card_echo_is_excluded_and_unknown_bot_recovers_without_new_review(
    store, known_card
):
    from daimon.core.recruitment_discord import DiscordMessage
    from daimon.core.stores.recruitment import invalidate_source

    config = await store.configuration()
    await store.save(
        ThreadState(thread_id="20", parent_id="2", revision="review", card_id="30", phase="review"),
        config,
    )
    before = await store.get("20")
    id = "30" if known_card else "31"
    await invalidate_source(store.sessions, store.tenant_id, "20", id)
    if not known_card:
        assert await store.pending_enrichment("20")
        # Same conversion used by both live and scheduler enrichment.
        source = DiscordMessage(
            id=id,
            content="private card",
            author={"id": "99", "bot": True},
            timestamp=datetime.now(UTC),
        ).source("101", "20")
        assert source is None
        await store.ignore_bot_source("20", id)
    assert not await store.pending_enrichment("20")
    after = await store.get("20")
    assert after.revision == before.revision
    assert after.seen_watermark == after.covered_watermark
    assert await store.messages("20") == []


async def test_confirmation_watermark_is_checked_atomically_with_outbox_insert(store, monkeypatch):
    from daimon.core.stores.recruitment import RecruitmentBusy, invalidate_source

    async def authorize(actor, thread):
        pass

    async with httpx.AsyncClient(transport=SyntheticRecruitmentTransport()) as http:
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
        state.scan_complete = True
        await store.save(state, await service.config())
        original_save = store.save

        async def racing_save(state, config, **kwargs):
            if kwargs.get("outbox"):
                await invalidate_source(store.sessions, store.tenant_id, "20", "11")
            await original_save(state, config, **kwargs)

        monkeypatch.setattr(store, "save", racing_save)
        with pytest.raises((ValueError, RecruitmentBusy)):
            await service.confirm(
                "20",
                state.revision,
                "create",
                "7",
                "race",
                covered_watermark=state.covered_watermark,
            )
        assert (await store.get("20")).outbox_id is None


async def test_scheduler_recovers_unknown_author_bot_edit_without_card_echo_loop(store):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from daimon.core.recruitment_runtime import reconcile_thread
    from daimon.core.stores.recruitment import invalidate_source

    config = await store.configuration()
    await store.save(
        ThreadState(
            thread_id="20", parent_id="2", phase="review", revision="same-review", card_id="30"
        ),
        config,
    )
    await invalidate_source(store.sessions, store.tenant_id, "20", "31")

    async def authorize(actor, thread):
        pass

    port = SimpleNamespace(
        check_access=AsyncMock(),
        bot_id=AsyncMock(return_value="99"),
        message=AsyncMock(return_value=None),
        history=AsyncMock(return_value=([], None, "0")),
    )
    async with httpx.AsyncClient(transport=SyntheticRecruitmentTransport()) as http:
        service = RecruitmentService(
            store,
            RecruitmentAPI(http, "https://synthetic.invalid", SecretStr("fake")),
            extract,
            authorize,
            backend="synthetic",
        )
        await reconcile_thread(service, port, "20")
    state = await store.get("20")
    assert state.revision == "same-review"
    assert state.seen_watermark == state.covered_watermark
    assert not await store.pending_enrichment("20")
    assert await store.messages("20") == []


async def test_new_evidence_does_not_deadlock_same_key_uncertainty_resolution(store):
    fake = SyntheticRecruitmentTransport()
    lost = False

    class AcceptedThenLost(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal lost
            response = await fake.handle_async_request(request)
            if not request.url.path.endswith("/match") and not lost:
                lost = True
                raise httpx.ReadTimeout("accepted", request=request)
            return response

    async def authorize(actor, thread):
        pass

    async with httpx.AsyncClient(transport=AcceptedThenLost()) as http:
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
        state.scan_complete = True
        await store.save(state, await service.config())
        await service.confirm("20", state.revision, "create", "7", "uncertain")
        await service.deliver("20", force=True)
        item = await store.outbox((await store.get("20")).outbox_id)
        assert item.possibly_applied
        await service.ingest("20", [message("11", "Synthetic newer fact")])
        await service.deliver("20", force=True)
        state = await store.get("20")
        assert state.candidate_id and state.outbox_id is None
        assert state.phase == "blocked" and len(fake.effects) == 1


async def test_every_non_control_gateway_event_advances_watermark_even_if_reordered(store):
    from daimon.core.stores.recruitment import invalidate_source

    await store.save(
        ThreadState(thread_id="20", parent_id="2", card_id="30"), await store.configuration()
    )
    original = message()
    await store.ingest("20", original)
    before = (await store.get("20")).seen_watermark
    await invalidate_source(store.sessions, store.tenant_id, "20", "10", deleted=True)
    await invalidate_source(store.sessions, store.tenant_id, "20", "10", deleted=True)
    await invalidate_source(
        store.sessions,
        store.tenant_id,
        "20",
        "10",
        observed_at=original.observed_at - timedelta(days=1),
    )
    assert (await store.get("20")).seen_watermark == before + 3
    assert (await store.message_manifest("20"))[0][2] is True
    await invalidate_source(store.sessions, store.tenant_id, "20", "30")
    assert (await store.get("20")).seen_watermark == before + 3


@pytest.mark.parametrize("known_card", [True, False])
async def test_control_echo_needs_no_reapproval_for_current_or_pending_confirmation(
    store, known_card
):
    from daimon.core.stores.recruitment import invalidate_source

    async def authorize(actor, thread):
        pass

    fake = SyntheticRecruitmentTransport()
    async with httpx.AsyncClient(transport=fake) as http:
        service = RecruitmentService(
            store,
            RecruitmentAPI(http, "https://synthetic.invalid", SecretStr("fake")),
            extract,
            authorize,
            backend="synthetic",
        )
        await service.enroll("20", "2", "!labs-staffing: Synthetic")
        await service.ingest("20", [message()])
        review = await store.get("20")
        review.scan_complete, review.card_id = True, "30"
        await store.save(review, await service.config())
        if not known_card:
            await service.confirm(
                "20",
                review.revision,
                "create",
                "7",
                "original",
                covered_watermark=review.covered_watermark,
            )
        await invalidate_source(store.sessions, store.tenant_id, "20", "30" if known_card else "31")
        if known_card:
            # An own-card echo cannot invalidate a concurrently clicked review.
            await service.confirm(
                "20",
                review.revision,
                "create",
                "7",
                "original",
                covered_watermark=review.covered_watermark,
            )
        else:
            await service.deliver("20", force=True)
            assert not fake.effects
            await store.ignore_bot_source("20", "31")
        await service.deliver("20", force=True)
        assert len(fake.effects) == 1
        assert (await store.get("20")).candidate_id
