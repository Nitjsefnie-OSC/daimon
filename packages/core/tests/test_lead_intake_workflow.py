"""Durable coordinator parity against isolated Postgres and synthetic providers."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from daimon.core.github_credentials import build_multifernet
from daimon.core.lead_intake import IntakeConfig, IntakeResolution, LeadDraft, LeadSource
from daimon.core.lead_intake_service import EffectRequest, IntakeService, VerifiedSource
from daimon.core.stores.lead_intake import IntakeStore
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/lead_intake/classification.json").read_text()
)


@pytest.fixture
async def lead_sessions(
    db_nullpool_engine: AsyncEngine, db_clean: None
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(db_nullpool_engine, expire_on_commit=False)


class FakePorts:
    def __init__(self, source_text: str) -> None:
        self.source_text = source_text
        self.remote: dict[str, str] = {}
        self.writes: list[str] = []
        self.fail_step: str | None = None
        self.fail_before = False
        self.recipients = ("intake@example.test",)
        self.scan_fail = False

    async def source(
        self, config: IntakeConfig, source: LeadSource, actor_id: str | None
    ) -> VerifiedSource:
        return VerifiedSource(
            source=source,
            text=self.source_text,
            raw="sanitized raw text",
            recipients=self.recipients,
        )

    async def recover(self, config: IntakeConfig, effect: EffectRequest) -> str | None:
        return effect.payload.get("selected") or self.remote.get(effect.key)

    def replay_safe(self, effect: EffectRequest) -> bool:
        return False

    async def write(self, config: IntakeConfig, effect: EffectRequest) -> str:
        if self.fail_before and self.fail_step == effect.step:
            raise TimeoutError("synthetic response loss")
        identity = str(len(self.remote) + 100)
        self.remote[effect.key] = identity
        self.writes.append(effect.step)
        if self.fail_step == effect.step:
            raise TimeoutError("synthetic response loss")
        return identity

    async def scan(
        self, config: IntakeConfig, start: datetime, end: datetime, cursor: str | None
    ) -> tuple[list[str], str | None]:
        if cursor and self.scan_fail:
            raise TimeoutError("synthetic page failure")
        return (["archived-a"], "next") if not cursor else (["archived-b"], None)


async def noop(source: LeadSource | None) -> None:
    pass


def draft(classification: str = "project", message: str = "immutable-1") -> LeadDraft:
    return LeadDraft(
        source=LeadSource(kind="gmail", mailbox="owner@example.test", message_id=message),
        classification=classification,
        opportunity_description="Forecast project",
        thread_title="Synthetic",
        thread_content="Research summary\nDRAFT reply: Thank you for your enquiry.",
        enquirer_email="ada@synthetic.example",
        company_name="Synthetic",
        company_domain="synthetic.example",
        enquirer_evidence="From: Ada <ada@synthetic.example>",
        company_evidence="Ada, Synthetic synthetic.example",
    )


async def setup(
    sessions: async_sessionmaker[AsyncSession], mode: str = "capture"
) -> tuple[IntakeStore, IntakeService, FakePorts]:
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session)
    store = IntakeStore(sessions, build_multifernet((Fernet.generate_key().decode(),)), tenant.id)
    config = IntakeConfig(
        agent_id=uuid4(),
        guild_id="1",
        channel_id="2",
        source_connection_id="gmail-1",
        gmail_mailbox="owner@example.test",
        crm_connection_id="hubspot-1",
        portal_id="3",
        recipients=("intake@example.test",),
        mode=mode,
        reconciliation_start=datetime.now(UTC) - timedelta(days=1),
    )
    await store.configure(config, {"hubspot_access_token": "synthetic"})
    ports = FakePorts(FIXTURES[-2]["text"])
    return store, IntakeService(store, ports, noop), ports


@pytest.mark.parametrize("row", FIXTURES, ids=[r["kind"] for r in FIXTURES])
async def test_fake_model_classification_replay(
    lead_sessions: async_sessionmaker[AsyncSession], row: dict[str, str]
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    ports.source_text = row["text"]
    discovered = await service.scan()
    assert "archived-a" in discovered["discovered"]
    fetched = await service.read_source("archived-a")
    assert fetched.text == row["text"] and fetched.raw is None
    # Replay the model decision from the text actually fetched through the
    # configured mailbox, without a separately provisioned model credential.
    classification = next(
        item["classification"] for item in FIXTURES if item["text"] == fetched.text
    )
    result = await service.submit(draft(classification, "archived-a"), "7")
    if row["classification"] == "other":
        assert result.status == "rejected"
        assert ports.writes == [], "Junk cannot post, create CRM or archive"
    else:
        assert result.status == "completed"
        assert ports.writes == [
            "starter",
            "thread",
            "dossier:0",
            "archive",
            "company",
            "contact",
            "deal",
            "association:companies",
            "association:contacts",
        ]
    assert (await store.get(result.id)).submission == result.submission


async def test_capture_and_replay_preserve_original(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    first = await service.submit(draft(), "7")
    ports.source_text = "changed provider text"
    replay = await service.submit(draft("other"), "8")
    assert replay.id == first.id
    assert replay.submission == first.submission
    assert replay.actor_id == "7"
    assert ports.writes == []
    assert len(await store.list()) == 1


@pytest.mark.parametrize(
    "step",
    [
        "starter",
        "thread",
        "dossier:0",
        "archive",
        "company",
        "contact",
        "deal",
        "association:companies",
    ],
)
async def test_uncertain_response_recovers_without_duplicates(
    lead_sessions: async_sessionmaker[AsyncSession], step: str
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    ports.fail_step = step
    first = await service.submit(draft(), "7")
    assert first.status == "pending"
    ports.fail_step = None
    results = await asyncio.gather(service.resume(first.id), service.resume(first.id))
    assert all(r.status in {"pending", "completed"} for r in results)
    assert (await service.resume(first.id)).status == "completed"
    assert ports.writes.count(step) == 1
    assert len(ports.writes) == 9
    assert (await store.get(first.id)).status == "completed"


async def test_uncertain_absence_never_blindly_reposts(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, service, ports = await setup(lead_sessions, "active")
    ports.fail_step, ports.fail_before = "starter", True
    first = await service.submit(draft(), "7")
    ports.fail_step = None
    retry = await service.resume(first.id)
    assert retry.status == "review"
    assert retry.error_code == "uncertain_write"
    assert ports.writes == []


async def test_recipient_rejection_precedes_acceptance(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    ports.recipients = ("unrelated@example.test",)
    with pytest.raises(ValueError, match="recipient_mismatch"):
        await service.submit(draft(), "7")
    assert await store.list() == []
    assert ports.writes == []


async def test_identity_review_still_delivers_draft(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, service, ports = await setup(lead_sessions, "active")
    ports.source_text = "Uncertain original sender"
    result = await service.submit(draft(), "7")
    assert result.status == "review"
    assert result.error_code == "identity_review"
    assert ports.writes == ["starter", "thread", "dossier:0", "archive"]


async def test_crm_paused_delivers_and_archives(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, service, ports = await setup(lead_sessions, "crm_paused")
    result = await service.submit(draft(), "7")
    assert result.status == "pending"
    assert ports.writes == ["starter", "thread", "dossier:0", "archive"]


async def test_retention_keeps_only_tombstone_and_tenant_isolation(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    first = await service.submit(draft(), "7")
    async with lead_sessions() as session:
        row = (
            await session.execute(
                text(
                    "SELECT encrypted_payload, encrypted_raw FROM lead_intakes WHERE tenant_id=:tenant"
                ),
                {"tenant": store.tenant_id},
            )
        ).one()
        assert b"synthetic.example" not in bytes(row.encrypted_payload)
        assert row.encrypted_raw is not None
    await store.purge(datetime.now(UTC) + timedelta(days=8))
    async with lead_sessions() as session:
        assert (
            await session.execute(
                text("SELECT encrypted_raw FROM lead_intakes WHERE tenant_id=:tenant"),
                {"tenant": store.tenant_id},
            )
        ).scalar() is None
    other, _, _ = await setup(lead_sessions)
    with pytest.raises(ValueError, match="intake_not_found"):
        await other.get(first.id)
    assert await other.effect(first.id, "starter") is None
    await store.purge(datetime.now(UTC) + timedelta(days=91))
    tombstone = await store.get(first.id)
    assert tombstone.submission is None
    assert tombstone.progress.discord_message_id is None
    assert tombstone.actor_id is None
    replay = await service.submit(draft(), "7")
    assert replay.id == first.id and replay.status == "expired"
    assert ports.writes.count("starter") == 1
    assert await store.effect(first.id, "starter") is None


async def test_scan_failure_preserves_discoveries_without_checkpoint_skip(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    ports.scan_fail = True
    failed = await service.scan()
    assert failed["error_code"] == "mailbox_scan_failed"
    assert failed["checkpoint"] is None
    assert failed["discovered"] == ["archived-a"]
    ports.scan_fail = False
    result = await service.scan()
    assert result["caught_up"] is True
    state = await store.mailbox("gmail-1")
    assert state.discovered == ["archived-a", "archived-b"]


async def test_resolution_audits_without_changing_source(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    ports.source_text = "Uncertain original sender"
    first = await service.submit(draft(), "7")
    result = await service.resolve(
        first.id,
        IntakeResolution(
            company_id="200",
            contact_id="201",
            evidence="Sales resolver verified original identity against source email.",
        ),
        "8",
    )
    assert result.status == "completed"
    assert result.submission == first.submission
    async with lead_sessions() as session:
        audit = (
            await session.execute(
                text(
                    "SELECT encrypted_resolution FROM lead_resolution_audit WHERE tenant_id=:tenant"
                ),
                {"tenant": store.tenant_id},
            )
        ).scalar_one()
    assert '"actor": "8"' in store.open(audit)
    assert "verified original identity" in store.open(audit)


async def test_privacy_erasure_and_retention_maintenance(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.stores.lead_intake import count_actor, erase_actor, purge_expired

    store, service, _ = await setup(lead_sessions, "active")
    first = await service.submit(draft(), "7")
    async with lead_sessions() as session, session.begin():
        assert await count_actor(session, store.tenant_id, "7") == 1
        assert await count_actor(session, store.tenant_id, "8") == 0
        assert await erase_actor(session, store.tenant_id, "7") == 1
    assert (await store.get(first.id)).status == "expired"
    assert await store.effect(first.id, "starter") is None
    second = await service.submit(draft(message="immutable-2"), "8")
    async with lead_sessions() as session, session.begin():
        await purge_expired(session, datetime.now(UTC) + timedelta(days=91))
    assert (await store.get(second.id)).submission is None


async def test_mode_cutover_and_configuration_revision(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    first = await service.submit(draft(), "7")
    config, credentials = await store.configuration()
    await store.configure(config.model_copy(update={"mode": "active"}), credentials)
    assert (await service.resume(first.id)).status == "completed"
    assert ports.writes.count("starter") == 1
    with pytest.raises(ValueError, match="configuration_revision_must_increase"):
        await store.configure(config.model_copy(update={"channel_id": "different"}), credentials)


async def test_queue_and_scan_ignore_completed_history(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, _ = await setup(lead_sessions, "active")
    first = await service.submit(draft(), "7")
    assert await store.list(pending_only=True) == []
    assert (await store.health())["status_counts"] == {"completed": 1}
    assert await store.list(after_id=first.id) == []
    config, credentials = await store.configuration()
    await store.configure(config.model_copy(update={"mode": "capture"}), credentials)
    pending = await service.submit(draft(message="archived-a"), "7")
    assert [r.id for r in await store.list(pending_only=True)] == [pending.id]
    scanned = await service.scan()
    assert scanned["discovered"] == ["archived-b"]


async def test_live_revocation_stops_next_effect(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, _, ports = await setup(lead_sessions, "active")
    calls = 0

    async def reauthorize(source: LeadSource | None) -> None:
        nonlocal calls
        calls += 1
        if calls >= 5:
            raise PermissionError("revoked")

    service = IntakeService(store, ports, reauthorize)
    result = await service.submit(draft(), "7")
    assert result.status == "pending"
    assert ports.writes == ["starter"]


async def test_execution_grant_is_live_tenant_bound_and_revoked(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.lead_intake_execution import current_intake_execution
    from daimon.core.stores.lead_intake import execution_is_active, routine_execution
    from daimon.testing.factories import make_routine

    async with lead_sessions() as session, session.begin():
        tenant = await make_tenant(session)
        routine = await make_routine(session, tenant=tenant, created_by_user_id="7")
    async with routine_execution(lead_sessions, tenant.id, routine.id):
        grant = current_intake_execution.get()
        assert grant is not None
        async with lead_sessions() as session:
            assert await execution_is_active(session, tenant.id, routine.id, grant[1])
            assert not await execution_is_active(session, uuid4(), routine.id, grant[1])
    async with lead_sessions() as session:
        assert not await execution_is_active(session, tenant.id, routine.id, grant[1])
    assert current_intake_execution.get() is None


async def test_partial_dossier_recovery_precedes_archive(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    _, service, ports = await setup(lead_sessions, "active")
    prepared = draft().model_copy(update={"thread_content": "DRAFT reply. " * 400})
    ports.fail_step = "dossier:1"
    first = await service.submit(prepared, "7")
    assert first.status == "pending"
    assert "archive" not in ports.writes
    ports.fail_step = None
    assert (await service.resume(first.id)).status == "completed"
    assert ports.writes.count("dossier:0") == ports.writes.count("dossier:1") == 1
    assert ports.writes.index("dossier:2") < ports.writes.index("archive")


async def test_resolution_confirmation_is_tenant_opt_in(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    from daimon.core.stores.lead_intake import confirmation_tools

    assert await confirmation_tools(lead_sessions, uuid4()) == frozenset()
    store, _, _ = await setup(lead_sessions)
    assert await confirmation_tools(lead_sessions, store.tenant_id) == frozenset(
        {"lead_intake_resolve"}
    )


async def test_resolution_cannot_approve_another_configuration_revision(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    record = await service.submit(draft(), "7")
    config, credentials = await store.configuration()
    await store.configure(
        config.model_copy(update={"revision": 2, "portal_id": "different"}), credentials
    )
    with pytest.raises(ValueError, match="config_revision_review"):
        await service.resolve(
            record.id,
            IntakeResolution(
                company_id="200", evidence="Verified company selection for the original portal."
            ),
            "7",
        )
    assert ports.writes == []


async def test_manual_source_replay_is_independent_of_gmail_connection(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    prepared = draft().model_copy(
        update={"source": LeadSource(kind="manual", channel_id="500", message_id="600")}
    )
    first = await service.submit(prepared, "7")
    config, credentials = await store.configuration()
    await store.configure(
        config.model_copy(update={"revision": 2, "source_connection_id": "gmail-new"}), credentials
    )
    replay = await service.submit(prepared, "7")
    assert replay.id == first.id
    assert ports.writes == []


async def test_gmail_tombstone_survives_connection_rename(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions, "active")
    first = await service.submit(draft(), "7")
    assert first.status == "completed"
    config, credentials = await store.configuration()
    renamed = config.model_copy(update={"revision": 2, "source_connection_id": "gmail-renamed"})
    await store.configure(renamed, credentials)
    assert await store.seen_source(renamed, draft().source.message_id)
    assert (await service.submit(draft(), "7")).id == first.id
    await store.purge(datetime.now(UTC) + timedelta(days=91))
    replay = await service.submit(draft(), "7")
    assert replay.id == first.id and replay.submission is None
    assert ports.writes.count("starter") == 1


async def test_resolution_cannot_recreate_data_after_actor_erasure(
    lead_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.core.lead_intake import IntakeRecord
    from daimon.core.stores.lead_intake import erase_actor

    store, service, _ = await setup(lead_sessions)
    record = await service.submit(draft(), "7")
    reached, release = asyncio.Event(), asyncio.Event()
    original = store.audit

    async def paused_audit(
        current: IntakeRecord,
        actor: str,
        before: str,
        after: str,
        *,
        effect_resolution: tuple[str, str | None] | None = None,
    ) -> None:
        reached.set()
        await release.wait()
        await original(current, actor, before, after, effect_resolution=effect_resolution)

    monkeypatch.setattr(store, "audit", paused_audit)
    resolution = asyncio.create_task(
        service.resolve(
            record.id,
            IntakeResolution(company_id="201", evidence="Human checked exact company record 201."),
            "8",
        )
    )
    await asyncio.wait_for(reached.wait(), 10)
    try:
        async with lead_sessions() as session, session.begin():
            assert await erase_actor(session, store.tenant_id, "7") == 1
    finally:
        release.set()
    with pytest.raises(ValueError, match="intake_erased_or_expired"):
        await resolution
    with pytest.raises(ValueError, match="intake_erased_or_expired"):
        await store.intent(record.id, "company", "stale")
    config, _ = await store.configuration()
    with pytest.raises(ValueError, match="intake_erased_or_expired"):
        await store.opportunity(config, record, "201", "stale personal description")
    async with lead_sessions() as session:
        for table in ("lead_resolution_audit", "lead_intake_effects", "lead_opportunities"):
            assert (
                await session.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id=:tenant"),
                    {"tenant": store.tenant_id},
                )
                == 0
            )
    assert (await store.get(record.id)).submission is None


async def test_unclassified_discovery_expires_from_health_and_resume(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, _ = await setup(lead_sessions)
    scanned = await service.scan()
    assert len(scanned["discovered"]) == 2
    assert (await store.health())["unclassified_count"] == 2
    async with lead_sessions() as session, session.begin():
        state = await session.scalar(
            text("SELECT encrypted_state FROM lead_mailbox_states WHERE tenant_id=:tenant"),
            {"tenant": store.tenant_id},
        )
        assert state is not None and "archived-a" not in store.open(state)
        await session.execute(
            text(
                "UPDATE lead_mailbox_messages SET expires_at=now()-interval '1 second' WHERE tenant_id=:tenant"
            ),
            {"tenant": store.tenant_id},
        )
    assert (await store.health())["unclassified_count"] == 0
    assert (await store.mailbox("gmail-1")).discovered == []
    assert (await service.scan())["discovered"] == []
    await store.purge(datetime.now(UTC))
    config, _ = await store.configuration()
    assert await store.seen_source(config, "archived-a")
    async with lead_sessions() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT encrypted_message_id, disposition, connection_id FROM lead_mailbox_messages WHERE tenant_id=:tenant"
                ),
                {"tenant": store.tenant_id},
            )
        ).all()
        assert len(rows) == 2
        assert all(row == (None, "expired", "") for row in rows)


async def test_preclassification_read_requires_live_discovery(
    lead_sessions: async_sessionmaker[AsyncSession],
) -> None:
    store, service, ports = await setup(lead_sessions)
    with pytest.raises(ValueError, match="unexpired_discovery_required"):
        await service.read_source("undiscovered")
    await service.scan()
    assert (await service.read_source("archived-a")).raw is None
    await store.purge(datetime.now(UTC) + timedelta(days=91))
    with pytest.raises(ValueError, match="unexpired_discovery_required"):
        await service.read_source("archived-a")
    assert ports.writes == []
