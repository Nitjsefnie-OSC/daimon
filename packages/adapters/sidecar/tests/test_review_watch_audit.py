"""Startup refuses effective grants outside the first search-path schema."""

import uuid

import pytest
from daimon.core.stores.watches import audit_sidecar_identity
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from . import test_callbacks

registered = test_callbacks.registered

pytestmark = [pytest.mark.fresh_schema, pytest.mark.usefixtures("watch_privilege_audit_lock")]


@pytest.fixture(scope="module")
async def db_engine(db_preflight):
    """Privilege probes need a clean database, not just a clean first schema."""
    import os
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from daimon.testing.db import build_test_engine, require_owned_sessionmaker
    from sqlalchemy.engine import make_url

    name = f"daimon_test_nc_watch_audit_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    admin = build_test_engine(db_preflight.url, db_preflight.schema)
    require_owned_sessionmaker(admin)
    async with admin.connect() as db:
        db = await db.execution_options(isolation_level="AUTOCOMMIT")
        await db.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(db_preflight.url).set(database=name).render_as_string(hide_password=False)
    engine = build_test_engine(url, "public", pool_size=2, max_overflow=0)
    require_owned_sessionmaker(engine)
    try:
        async with engine.connect() as db:

            def migrate(sync):
                root = Path(__file__).resolve().parents[4]
                config = Config(str(root / "alembic.ini"))
                config.set_main_option("script_location", str(root / "packages/core/alembic"))
                config.attributes["connection"] = sync
                command.upgrade(config, "head")

            await db.run_sync(migrate)
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as db:
            db = await db.execution_options(isolation_level="AUTOCOMMIT")
            await db.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.fixture
async def db_session_factory(db_engine):
    from daimon.testing.db import _fresh_schema_session

    url = db_engine.url.render_as_string(hide_password=False)
    async with _fresh_schema_session(url) as session:
        yield async_sessionmaker(bind=session.bind, expire_on_commit=False)


@pytest.mark.parametrize(
    "grant",
    [
        "table",
        "public",
        "column",
        "inherited",
        "definer",
        "shadow",
        "missing_ports",
        "schema_create",
    ],
)
async def test_review_audit_sees_privileges_after_leading_schema(
    db_engine,
    db_session_factory,
    registered,
    grant,
):
    role = "review_" + uuid.uuid4().hex
    decoy = "review_" + uuid.uuid4().hex
    async with db_session_factory() as db:
        schema = await db.scalar(text("select current_schema()"))
    async with db_engine.begin() as db:
        await db.execute(
            text(f"CREATE ROLE {role} LOGIN PASSWORD 'review-only' IN ROLE daimon_sidecar")
        )
        await db.execute(text(f"CREATE SCHEMA {decoy}"))
        await db.execute(text(f'GRANT USAGE ON SCHEMA {decoy},"{schema}" TO {role}'))

    engine = create_async_engine(
        db_engine.url.set(username=role, password="review-only"),
        connect_args={"server_settings": {"search_path": f'{decoy},"{schema}",public'}},
    )
    try:
        async with async_sessionmaker(engine).begin() as db:
            assert await db.scalar(text("select current_schema()")) == decoy
            await audit_sidecar_identity(db)
        async with db_engine.begin() as db:
            if grant == "table":
                await db.execute(text(f'GRANT SELECT ON "{schema}".watch_capabilities TO {role}'))
            elif grant in ("public", "column", "inherited"):
                await db.execute(text(f"CREATE TABLE {decoy}.foreign_credentials(digest text)"))
                target = "PUBLIC" if grant == "public" else role
                if grant == "inherited":
                    target = role + "_parent"
                    await db.execute(text(f"CREATE ROLE {target}"))
                    await db.execute(text(f"GRANT {target} TO {role}"))
                rights = "SELECT" if grant == "public" else "SELECT(digest)"
                await db.execute(text(f"GRANT {rights} ON {decoy}.foreign_credentials TO {target}"))
            elif grant == "definer":
                await db.execute(
                    text(
                        f"CREATE FUNCTION {decoy}.prune_watch_evidence() RETURNS void "
                        "LANGUAGE sql SECURITY DEFINER AS 'SELECT NULL::void'"
                    )
                )
            elif grant == "shadow":
                await db.execute(text(f"CREATE TABLE {decoy}.watch_capabilities(digest text)"))
            elif grant == "schema_create":
                await db.execute(text(f"GRANT CREATE ON SCHEMA {decoy} TO {role}"))
        async with async_sessionmaker(engine).begin() as db:
            if grant == "missing_ports":
                await db.execute(text(f"SET LOCAL search_path={decoy}"))
            if grant == "table":
                assert await db.scalar(text("select count(*) from watch_capabilities")) == 1
            with pytest.raises(ValueError, match="Unsafe"):
                await audit_sidecar_identity(db)
    finally:
        await engine.dispose()
        async with db_engine.begin() as db:
            await db.execute(text(f"DROP OWNED BY {role}"))
            await db.execute(text(f"DROP ROLE {role}"))
            if grant == "inherited":
                await db.execute(text(f"DROP OWNED BY {role}_parent"))
                await db.execute(text(f"DROP ROLE {role}_parent"))
            await db.execute(text(f"DROP SCHEMA {decoy} CASCADE"))


async def test_privilege_audit_refuses_an_indeterminate_catalog_result():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    with pytest.raises(ValueError, match="Unsafe"):
        await audit_sidecar_identity(SimpleNamespace(scalar=AsyncMock(return_value=None)))
