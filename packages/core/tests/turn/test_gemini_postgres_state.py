"""Real state-DB transactions behind the existing Gemini record/hold owners."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from daimon.core._models import GeminiRuntimeState
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini_state import PostgresFlashAccounting, PostgresGeminiStorage
from daimon.testing.factories import make_account, make_tenant
from mux.contracts.ids import ModelRef, ResourceRef, Scope
from mux.contracts.resources import Artifact
from mux.drivers.gemini.flash import FLASH_PRIMARY, FlashAttempt
from mux.drivers.gemini.storage import ArtifactRecord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
async def postgres_state(db_nullpool_engine, db_clean):
    sessions = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    async with sessions.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
        other = await make_account(db, tenant=tenant)
        foreign_tenant = await make_tenant(db)
        foreign = await make_account(db, tenant=foreign_tenant)
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(account.id),
        principal_id="caller",
        authorization_id="admitted",
    )
    return (
        sessions,
        scope,
        scope.model_copy(update={"account_id": str(other.id)}),
        scope.model_copy(
            update={"tenant_id": str(foreign_tenant.id), "account_id": str(foreign.id)}
        ),
    )


def holds(sessions, scope, *, project="project"):
    return PostgresFlashAccounting(
        sessions, scope=scope, project=project, hold_usd=Decimal(".49"), cap_usd=Decimal(".49")
    )


async def test_postgres_full_records_restart_and_rollback(postgres_state):
    sessions, scope, _, _ = postgres_state
    first = PostgresGeminiStorage(sessions, scope=scope, project="project")
    ref = ResourceRef(
        id="artifact",
        kind="artifact",
        provider="gemini",
        account_scope_id="project",
        tenant_id=scope.tenant_id,
        account_id=scope.account_id,
    )
    async with first.transaction() as records:
        records.creations[(scope.tenant_id, scope.account_id, "key")] = ("p", "digest", "agent")
        records.artifacts["artifact"] = ArtifactRecord(
            Artifact(
                ref=ref,
                filename="binary.bin",
                media_type="application/octet-stream",
                size_bytes=3,
                created_at=datetime.now(UTC),
            ),
            "digest",
            b"\x00\xff\x80",
        )
    with pytest.raises(RuntimeError):
        async with first.transaction() as records:
            records.creations.clear()
            raise RuntimeError("rollback")
    restarted = PostgresGeminiStorage(sessions, scope=scope, project="project")
    async with restarted.transaction() as records:
        assert len(records.creations) == 1
        assert records.artifacts["artifact"].body == b"\x00\xff\x80"


@pytest.mark.parametrize("existing", [False, True])
async def test_postgres_serializes_first_insert_and_releases_cancelled_writer(
    postgres_state, existing
):
    sessions, scope, _, _ = postgres_state
    first, second = (
        PostgresGeminiStorage(sessions, scope=scope, project="project") for _ in range(2)
    )
    if existing:
        async with first.transaction():
            pass
    entered = asyncio.Event()

    async def competing():
        async with second.transaction() as records:
            entered.set()
            records.creations[("t", "a", "two")] = ("p", "d", "two")

    async with first.transaction() as records:
        records.creations[("t", "a", "one")] = ("p", "d", "one")
        task = asyncio.create_task(competing())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(entered.wait(), timeout=0.1)
        assert not task.done()
    await asyncio.wait_for(task, timeout=5)
    with pytest.raises(asyncio.CancelledError):
        async with first.transaction() as records:
            records.creations.clear()
            raise asyncio.CancelledError
    async with second.transaction() as records:
        assert len(records.creations) == 2


@pytest.mark.parametrize("outcome", ["accepted", "refused", "unknown"])
async def test_postgres_concurrent_holds_and_receipts_survive_restart(postgres_state, outcome):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)

    async def attempt():
        owner = holds(sessions, scope)
        await owner.before_attempt(model=model, attempt=1)
        await owner.after_attempt(receipt=FlashAttempt(model, 1, outcome))

    result = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    assert sum(value is None for value in result) == 1
    assert sum(isinstance(value, AdmissionDenied) for value in result) == 1
    with pytest.raises(AdmissionDenied, match="channel_budget_exceeded"):
        await holds(sessions, scope).before_attempt(model=model, attempt=1)
    async with sessions() as db:
        bodies = (await db.scalars(select(GeminiRuntimeState.body))).all()
    assert len(bodies) == 1 and '"held_usd": "0.49"' in bodies[0]
    assert f'"outcome": "{outcome}"' in bodies[0]


async def test_postgres_hold_commits_inside_records_transaction_and_survives_rollback(
    postgres_state,
):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    storage = PostgresGeminiStorage(sessions, scope=scope, project="project")
    with pytest.raises(RuntimeError):
        async with storage.transaction() as records:
            records.creations[("t", "a", "pending")] = ("p", "d", "agent")
            await asyncio.wait_for(holds(sessions, scope).before_attempt(model=model, attempt=1), 5)
            # A second process sees the committed hold before any model send.
            with pytest.raises(AdmissionDenied):
                await holds(sessions, scope).before_attempt(model=model, attempt=1)
            raise RuntimeError("crash before native receipt")
    async with storage.transaction() as records:
        assert not records.creations
    with pytest.raises(AdmissionDenied):
        await holds(sessions, scope).before_attempt(model=model, attempt=1)


async def test_postgres_namespaces_isolate_tenant_account_and_project(postgres_state):
    sessions, scope, other, foreign = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    await holds(sessions, scope).before_attempt(model=model, attempt=1)
    async with PostgresGeminiStorage(
        sessions, scope=scope, project="project"
    ).transaction() as records:
        records.creations[("t", "a", "source")] = ("p", "d", "agent")
    for selected, project in ((other, "project"), (foreign, "project"), (scope, "other-project")):
        await holds(sessions, selected, project=project).before_attempt(model=model, attempt=1)
        async with PostgresGeminiStorage(
            sessions, scope=selected, project=project
        ).transaction() as records:
            assert not records.creations
    forged = scope.model_copy(update={"account_id": foreign.account_id})
    with pytest.raises(ValueError, match="account is unavailable"):
        await holds(sessions, forged).before_attempt(model=model, attempt=1)
    with pytest.raises(ValueError, match="account is unavailable"):
        async with PostgresGeminiStorage(
            sessions, scope=scope.model_copy(update={"account_id": str(uuid4())}), project="project"
        ).transaction():
            pytest.fail("unbound account admitted")


@pytest.mark.fresh_schema
async def test_postgres_state_migration_matches_models_and_refuses_state_loss(db_session):
    import importlib.util
    from pathlib import Path

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory
    from daimon.core._models import Base
    from sqlalchemy import inspect

    directory = Path(__file__).parents[2] / "alembic"
    spec = importlib.util.spec_from_file_location(
        "gemini_state_migration", directory / "versions/0106_gemini_runtime_state.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert ScriptDirectory(str(directory)).get_heads() == ["0106_gemini_runtime_state"]

    def check(connection):
        GeminiRuntimeState.__table__.drop(connection)
        before = set(inspect(connection).get_table_names())
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            assert set(inspect(connection).get_table_names()) == before | {"gemini_runtime_state"}
            differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
            assert not [diff for diff in differences if "gemini_runtime_state" in str(diff)]
            with pytest.raises(RuntimeError, match="unresolved financial holds"):
                module.downgrade()
            assert "gemini_runtime_state" in inspect(connection).get_table_names()

    connection = await db_session.connection()
    await connection.run_sync(check)
