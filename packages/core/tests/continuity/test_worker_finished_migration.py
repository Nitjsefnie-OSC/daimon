"""Widening wake reasons preserves existing rows across an upgrade/downgrade."""

from __future__ import annotations

import importlib.util
import re
import uuid
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from daimon.core._models import Base
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

_LEGACY_REASONS = ("task_handoff", "private_input_applied", "timer", "github_access_ready")


@pytest.mark.fresh_schema
async def test_worker_finished_reason_upgrade_downgrade_upgrade(
    db_session: AsyncSession,
) -> None:
    path = Path(__file__).parents[2] / "alembic/versions/0094_worker_finished_wakes.py"
    spec = importlib.util.spec_from_file_location("worker_finished_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0094_worker_finished_wakes"
    assert module.down_revision == "0093_fleet_runs"

    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    connection = await db_session.connection()
    insert = text(
        "INSERT INTO task_continuations (tenant_id, platform, parent_channel_id, "
        "thread_id, requester_account_id, requester_external_user_id, target_ma_agent_id, "
        "target_name, reason, idempotency_key) VALUES (:tenant, 'discord', 'channel', "
        "'thread', :account, 'human', 'agent', 'orchestrator', :reason, :key)"
    )

    def cycle(sync: Connection) -> None:
        schema = sync.scalar(text("SELECT current_schema()"))
        before = set(inspect(sync).get_table_names(schema=schema))
        with Operations.context(MigrationContext.configure(sync)):
            module.upgrade()
            assert sync.scalar(text("SHOW lock_timeout")) == "5s"
            for reason in (*_LEGACY_REASONS, "worker_finished"):
                sync.execute(
                    insert,
                    {
                        "tenant": tenant.id,
                        "account": account.id,
                        "reason": reason,
                        "key": uuid.uuid4(),
                    },
                )
            with pytest.raises(IntegrityError), sync.begin_nested():
                sync.execute(
                    insert,
                    {
                        "tenant": tenant.id,
                        "account": account.id,
                        "reason": "unknown",
                        "key": uuid.uuid4(),
                    },
                )
            module.downgrade()
            assert tuple(
                sync.scalars(text("SELECT reason FROM task_continuations ORDER BY reason"))
            ) == tuple(sorted(_LEGACY_REASONS))
            with pytest.raises(IntegrityError), sync.begin_nested():
                sync.execute(
                    insert,
                    {
                        "tenant": tenant.id,
                        "account": account.id,
                        "reason": "worker_finished",
                        "key": uuid.uuid4(),
                    },
                )
            module.upgrade()
            sync.execute(
                insert,
                {
                    "tenant": tenant.id,
                    "account": account.id,
                    "reason": "worker_finished",
                    "key": uuid.uuid4(),
                },
            )
            assert tuple(
                sync.scalars(text("SELECT reason FROM task_continuations ORDER BY reason"))
            ) == tuple(sorted((*_LEGACY_REASONS, "worker_finished")))
            assert set(inspect(sync).get_table_names(schema=schema)) == before
            checks = inspect(sync).get_check_constraints("task_continuations", schema=schema)
            reason_check = next(
                check["sqltext"]
                for check in checks
                if check["name"] == "ck_task_continuations_reason"
            )
            assert "worker_finished" in reason_check
            model_check = next(
                constraint
                for constraint in Base.metadata.tables["task_continuations"].constraints
                if constraint.name == "ck_task_continuations_reason"
            )
            assert re.findall(r"'([^']+)'", reason_check) == re.findall(
                r"'([^']+)'", str(model_check.sqltext)
            )
            differences = compare_metadata(MigrationContext.configure(sync), Base.metadata)
            assert not [diff for diff in differences if "task_continuations" in str(diff)]

    await connection.run_sync(cycle)
