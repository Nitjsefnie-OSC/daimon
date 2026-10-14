"""Maintenance authority, immutable source and secret-handling boundaries."""

from __future__ import annotations

import copy
import importlib.util
import runpy
from pathlib import Path

import pytest
import yaml

pytest_plugins = ["daimon.testing.db"]

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "maintenance", ROOT / "scripts/ci/staging_maintenance.py"
)
assert SPEC and SPEC.loader
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)
SHA = "a" * 40
IMAGE = "us-east1-docker.pkg.dev/pymc-daimon-staging/staging-daimon/daimon@sha256:" + "b" * 64


def source_job():
    return yaml.safe_load((ROOT / "tests/fixtures/cloud_run_migration_job.yaml").read_text())


@pytest.fixture
def manifest_environment(monkeypatch):
    for name, value in {
        "ENVIRONMENT": "staging",
        "PROJECT_ID": maintenance.PROJECT,
        "REPO": maintenance.REPOSITORY,
        "PROJECT_NUMBER": "1234567890",
        "DATABASE_HOSTS": '["10.1.2.3"]',
    }.items():
        monkeypatch.setenv(name, value)


def test_dispatch_is_separate_from_normal_deploy_and_inherits_staging_protection():
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_call", "workflow_dispatch"}
    assert "downgrade_revision" not in triggers["workflow_call"]["inputs"]
    jobs = workflow["jobs"]
    job = jobs["staging-maintenance"]
    assert (
        job["if"] == "github.event_name == 'workflow_dispatch' && inputs.downgrade_revision != ''"
    )
    assert jobs["deploy"]["if"] == "inputs.downgrade_revision == ''"
    assert job["environment"] == "staging"
    assert job["concurrency"]["group"] == "deploy-gcp-staging"
    assert job["concurrency"]["cancel-in-progress"] is False
    steps = job["steps"]
    assert 'test "$ENVIRONMENT" = staging' in steps[0]["run"]
    auth = next(step for step in steps if step.get("id") == "auth")
    assert auth["with"]["service_account"] == "${{ vars.GCP_DEPLOY_SA }}"
    assert steps.index(auth) > 0
    artifact = next(step for step in steps if step.get("uses") == "actions/upload-artifact@v4")
    assert artifact["with"]["path"] == "${{ runner.temp }}/maintenance-receipt.json"


@pytest.mark.parametrize(
    "field,value",
    [
        ("environment", "production"),
        ("project", "pymc-daimon-prod"),
        ("repository", "production-daimon"),
        ("sha", "main"),
        ("sha", "a" * 39),
        ("sha", "$(id)"),
        ("target", "base"),
        ("target", "0098_external_github_session; id"),
    ],
)
def test_non_staging_or_unpinned_inputs_are_refused(field, value):
    inputs = dict(
        environment="staging",
        project=maintenance.PROJECT,
        repository=maintenance.REPOSITORY,
        sha=SHA,
        target=maintenance.TARGET,
    )
    inputs[field] = value
    with pytest.raises(ValueError):
        maintenance.require_staging(**inputs)


def test_ephemeral_executor_keeps_only_owned_database_binding_and_original_unchanged(
    manifest_environment,
):
    original = source_job()
    before = copy.deepcopy(original)
    result = maintenance.manifest(
        original, SHA, maintenance.TARGET, IMAGE, "staging-daimon-maintenance-123-1"
    )
    assert original == before
    assert result["metadata"]["namespace"] == "1234567890"
    task = result["spec"]["template"]["spec"]["template"]["spec"]
    annotations = result["spec"]["template"]["metadata"]["annotations"]
    assert annotations["run.googleapis.com/secrets"] == (
        "maintenance-database:projects/1234567890/secrets/database-url"
    )
    assert annotations["run.googleapis.com/cloudsql-instances"] == (
        "pymc-daimon-staging:us-east1:database"
    )
    assert annotations["run.googleapis.com/vpc-access-connector"] == (
        "projects/1234567890/locations/us-east1/connectors/staging"
    )
    assert task["containers"][0]["env"][0] == {
        "name": "DAIMON_DATABASE__URL",
        "valueFrom": {"secretKeyRef": {"name": "maintenance-database", "key": "latest"}},
    }
    assert not any(item["name"] == "UNRELATED_SECRET" for item in task["containers"][0]["env"])
    assert "volumes" not in task
    assert "volumeMounts" not in task["containers"][0]
    assert task["containers"][0]["command"] == ["/app/.venv/bin/python"]
    assert task["maxRetries"] == 0


@pytest.mark.parametrize(
    "fault", ["image", "account", "plaintext", "secret_project", "sql_project", "alias"]
)
def test_foreign_or_plaintext_template_is_refused_before_job_creation(manifest_environment, fault):
    source = source_job()
    template = source["spec"]["template"]
    task = template["spec"]["template"]["spec"]
    if fault == "image":
        task["containers"][0]["image"] = IMAGE.replace("b" * 64, "c" * 64)
    elif fault == "account":
        task["serviceAccountName"] = "runtime@pymc-daimon-prod.iam.gserviceaccount.com"
    elif fault == "plaintext":
        task["containers"][0]["env"] = [
            {"name": "DAIMON_DATABASE__URL", "value": "not-a-secret-ref"}
        ]
    elif fault == "secret_project":
        task["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]["name"] = (
            "projects/pymc-daimon-prod/secrets/database-url"
        )
    elif fault == "sql_project":
        template["metadata"] = {
            "annotations": {
                "run.googleapis.com/cloudsql-instances": "pymc-daimon-prod:us-east1:database"
            }
        }
    else:
        template["metadata"] = {
            "annotations": {
                "run.googleapis.com/secrets": "foreign:projects/other/secrets/database-url"
            }
        }
    with pytest.raises(ValueError):
        maintenance.manifest(
            source, SHA, maintenance.TARGET, IMAGE, "staging-daimon-maintenance-123-1"
        )


@pytest.mark.parametrize(
    "binding,value",
    [
        ("secrets", "database-url:projects/pymc-daimon-prod/secrets/database-url"),
        ("secrets", "database-url:projects/9876543210/secrets/database-url"),
        (
            "secrets",
            "database-url:projects/1234567890/secrets/database-url,"
            "unused:projects/pymc-daimon-prod/secrets/unrelated",
        ),
        (
            "secrets",
            "database-url:projects/1234567890/secrets/database-url,"
            "database-url:projects/1234567890/secrets/other",
        ),
        ("secrets", "database-url:unrecognized/resource"),
        ("cloudsql-instances", "pymc-daimon-prod:us-east1:database"),
        ("cloudsql-instances", "9876543210:us-east1:database"),
        ("cloudsql-instances", "pymc-daimon-staging-other:us-east1:database"),
        (
            "cloudsql-instances",
            "pymc-daimon-staging:us-east1:database,pymc-daimon-prod:us-east1:database",
        ),
        (
            "vpc-access-connector",
            "projects/pymc-daimon-prod/locations/us-east1/connectors/production",
        ),
        (
            "encryption-key",
            "projects/9876543210/locations/us-east1/keyRings/ring/cryptoKeys/key",
        ),
        (
            "network-interfaces",
            '[{"network":"projects/pymc-daimon-prod/global/networks/production"}]',
        ),
        (
            "network-interfaces",
            r'[{"network":"projects\u002fpymc-daimon-prod\u002fglobal\u002fnetworks\u002fproduction"}]',
        ),
    ],
)
def test_execution_bindings_refuse_foreign_or_ambiguous_authority(
    manifest_environment, binding, value
):
    source = source_job()
    source["spec"]["template"]["metadata"]["annotations"][f"run.googleapis.com/{binding}"] = value
    with pytest.raises(ValueError, match="foreign"):
        maintenance.manifest(
            source, SHA, maintenance.TARGET, IMAGE, "staging-daimon-maintenance-123-1"
        )


@pytest.mark.parametrize("owner", ["job", "task"])
@pytest.mark.parametrize("binding", ["secrets", "cloudsql-instances"])
def test_bindings_at_unsupported_metadata_levels_are_refused(manifest_environment, owner, binding):
    source = source_job()
    execution = source["spec"]["template"]
    metadata = (
        source["metadata"]
        if owner == "job"
        else execution["spec"]["template"].setdefault("metadata", {})
    )
    metadata["annotations"] = {
        f"run.googleapis.com/{binding}": execution["metadata"]["annotations"][
            f"run.googleapis.com/{binding}"
        ]
    }
    with pytest.raises(ValueError, match="misplaced"):
        maintenance.manifest(
            source, SHA, maintenance.TARGET, IMAGE, "staging-daimon-maintenance-123-1"
        )


@pytest.mark.parametrize("owner", ["database", "unused_env", "volume"])
@pytest.mark.parametrize("project", ["pymc-daimon-prod", "9876543210"])
def test_all_secret_references_are_checked_even_when_removed(manifest_environment, owner, project):
    source = source_job()
    task = source["spec"]["template"]["spec"]["template"]["spec"]
    resource = f"projects/{project}/secrets/database-url"
    if owner == "volume":
        task["volumes"][0]["secret"]["secretName"] = resource
    else:
        task["containers"][0]["env"][0 if owner == "database" else 1]["valueFrom"]["secretKeyRef"][
            "name"
        ] = resource
    with pytest.raises(ValueError, match="foreign"):
        maintenance.manifest(
            source, SHA, maintenance.TARGET, IMAGE, "staging-daimon-maintenance-123-1"
        )


@pytest.mark.parametrize("style", ["project_alias", "plain", "project_resource", "number_resource"])
def test_owned_secret_forms_resolve_to_one_explicit_staging_binding(manifest_environment, style):
    source = source_job()
    execution = source["spec"]["template"]
    reference = execution["spec"]["template"]["spec"]["containers"][0]["env"][0]["valueFrom"][
        "secretKeyRef"
    ]
    if style == "project_alias":
        execution["metadata"]["annotations"]["run.googleapis.com/secrets"] = (
            "database-url:projects/pymc-daimon-staging/secrets/database-url"
        )
    elif style == "plain":
        execution["metadata"]["annotations"].pop("run.googleapis.com/secrets")
    else:
        reference["name"] = (
            "projects/"
            + ("pymc-daimon-staging" if style == "project_resource" else "1234567890")
            + "/secrets/database-url"
        )
    result = maintenance.manifest(
        source, SHA, maintenance.TARGET, IMAGE, "staging-daimon-maintenance-123-1"
    )
    assert result["spec"]["template"]["metadata"]["annotations"]["run.googleapis.com/secrets"] == (
        "maintenance-database:projects/1234567890/secrets/database-url"
    )


@pytest.mark.parametrize("project", ["pymc-daimon-staging", "1234567890"])
def test_database_lookup_resolves_a_differently_named_owned_secret(manifest_environment, project):
    source = source_job()
    source["spec"]["template"]["metadata"]["annotations"]["run.googleapis.com/secrets"] = (
        f"database-url:projects/{project}/secrets/actual-database-secret"
    )
    result = maintenance.manifest(
        source, SHA, maintenance.TARGET, IMAGE, "staging-daimon-maintenance-123-1"
    )
    execution = result["spec"]["template"]
    assert execution["metadata"]["annotations"]["run.googleapis.com/secrets"] == (
        "maintenance-database:projects/1234567890/secrets/actual-database-secret"
    )
    assert execution["spec"]["template"]["spec"]["containers"][0]["env"][0]["valueFrom"] == {
        "secretKeyRef": {"name": "maintenance-database", "key": "latest"}
    }


def fixture_config(tmp_path):
    """Exercise real Alembic commands with the production connection owner."""
    from alembic.config import Config

    scripts = tmp_path / "migrations"
    (scripts / "versions").mkdir(parents=True)
    (scripts / "env.py").write_text((ROOT / "packages/core/alembic/env.py").read_text())
    revisions = [
        (
            "0086",
            None,
            'op.execute("CREATE TABLE maintenance_main (payload text NOT NULL)"); '
            "op.execute(\"INSERT INTO maintenance_main VALUES ('retained seed')\")",
            "op.execute('DROP TABLE maintenance_main')",
        ),
        (maintenance.TARGET, "0086", "pass", "pass"),
        (
            "0087_neutral_state",
            "0086",
            'op.execute("CREATE TABLE maintenance_neutral (payload text NOT NULL)"); '
            "op.execute(\"INSERT INTO maintenance_neutral VALUES ('retained evidence')\") "
            "if context.config.attributes.get('neutral_evidence') else None",
            "op.execute('DELETE FROM maintenance_main') if context.config.attributes.get('lose_seed') else None; "
            "op.execute('DROP TABLE maintenance_neutral')",
        ),
        ("0089_neutral_catalog", "0087_neutral_state", "pass", "pass"),
        ("0096_agent_fleet_grant", "0089_neutral_catalog", "pass", "pass"),
        (
            "0099_merge_main_neutral",
            ("0096_agent_fleet_grant", maintenance.TARGET),
            "if context.config.attributes.get('fail_upgrade'): raise RuntimeError('test re-upgrade failure')",
            "pass",
        ),
    ]
    for identity, parent, upgrade, downgrade in revisions:
        (scripts / "versions" / f"{identity}.py").write_text(
            "from alembic import op, context\n"
            f"revision={identity!r}\ndown_revision={parent!r}\nbranch_labels=None\ndepends_on=None\n"
            f"def upgrade():\n    {upgrade}\ndef downgrade():\n    {downgrade}\n"
        )
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(scripts))
    config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://unused/owned")
    return config


@pytest.mark.parametrize("attributes", [{}, {"connection": None}])
def test_default_online_migrations_still_use_the_async_engine(monkeypatch, attributes):
    import sqlalchemy.ext.asyncio
    from alembic import context
    from alembic.config import Config
    from sqlalchemy import pool

    config = Config()
    config.set_main_option("sqlalchemy.url", "postgresql+asyncpg://unused/default")
    config.attributes.update(attributes)
    monkeypatch.delenv("DAIMON_DATABASE_URL", raising=False)
    monkeypatch.delenv("DAIMON_DATABASE__URL", raising=False)
    monkeypatch.setattr(context, "config", config, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    calls = []

    class Engine:
        def connect(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def run_sync(self, function):
            calls.append(function.__name__)

        async def dispose(self):
            calls.append("dispose")

    def factory(section, **options):
        assert section["sqlalchemy.url"] == "postgresql+asyncpg://unused/default"
        assert options == dict(prefix="sqlalchemy.", poolclass=pool.NullPool, hide_parameters=True)
        calls.append("engine")
        return Engine()

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "async_engine_from_config", factory)
    # Execute the real module entry point; asyncio.run must own this path.
    runpy.run_path(str(ROOT / "packages/core/alembic/env.py"))
    assert calls == ["engine", "do_run_migrations", "dispose"]


@pytest.mark.parametrize(
    "fault", [None, "lose_seed", "fail_upgrade", "schema_hash", "neutral_evidence"]
)
async def test_real_migration_roundtrip_preserves_all_evidence_on_success_or_failure(
    db_session, tmp_path, monkeypatch, fault
):
    from alembic import command
    from sqlalchemy import text

    connection = await db_session.connection()
    config = fixture_config(tmp_path)
    if fault == "neutral_evidence":
        config.attributes[fault] = True

    def prepare(conn):
        config.attributes["connection"] = conn
        # The shared fixture is migrated already. Replace its version only
        # inside this test's rollback transaction with the temporary graph.
        conn.execute(text("DELETE FROM alembic_version"))
        command.upgrade(config, maintenance.TARGET)
        monkeypatch.setattr(
            maintenance, "SCHEMA_HASH", maintenance.digest(maintenance.schema(conn))
        )
        command.upgrade(config, "head")
        return maintenance.schema(conn)

    original = await connection.run_sync(prepare)
    # Re-upgrade creates an empty neutral table; it cannot reconstruct the
    # retained evidence which the downgrade discarded.
    config.attributes["neutral_evidence"] = False
    if fault == "schema_hash":
        monkeypatch.setattr(maintenance, "SCHEMA_HASH", "invalid accepted schema")
    elif fault and fault != "neutral_evidence":
        config.attributes[fault] = True
    transaction = await connection.begin_nested()
    try:
        if fault:
            with pytest.raises((ValueError, RuntimeError)):
                await connection.run_sync(maintenance.roundtrip, config)
        else:
            receipt = await connection.run_sync(maintenance.roundtrip, config)
            assert receipt["heads"] == [
                "0099_merge_main_neutral",
                maintenance.TARGET,
                "0099_merge_main_neutral",
            ]
            assert receipt["main_rows"] == 1
    finally:
        await transaction.rollback()
    assert await connection.run_sync(maintenance.schema) == original
    assert list(
        (await connection.execute(text("SELECT payload FROM maintenance_main"))).scalars()
    ) == ["retained seed"]
    assert list(
        (await connection.execute(text("SELECT payload FROM maintenance_neutral"))).scalars()
    ) == (["retained evidence"] if fault == "neutral_evidence" else [])


def test_runtime_denial_never_prints_secret_environment_or_driver_details(monkeypatch):
    import os
    import subprocess
    import sys

    secret = "opaque-private-database-value"
    environment = os.environ | {
        "MAINTENANCE_PROJECT": "pymc-daimon-prod",
        "MAINTENANCE_SHA": SHA,
        "MAINTENANCE_TARGET": maintenance.TARGET,
        "DAIMON_DATABASE__URL": secret,
    }
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ci/staging_maintenance.py"), "probe"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "Staging maintenance refused: ValueError\n"
    assert secret not in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://user@192.0.2.9/production",
        "postgresql+asyncpg://user@10.1.2.3/database?host=192.0.2.9",
        "postgresql+asyncpg://user@10.1.2.3:6432/database",
    ],
)
async def test_foreign_database_route_refuses_before_any_engine_or_connection(monkeypatch, url):
    import sqlalchemy.ext.asyncio

    for name, value in {
        "MAINTENANCE_PROJECT": maintenance.PROJECT,
        "MAINTENANCE_SHA": SHA,
        "MAINTENANCE_TARGET": maintenance.TARGET,
        "MAINTENANCE_DATABASE_HOSTS": '["10.1.2.3"]',
        "DAIMON_DATABASE__URL": url,
    }.items():
        monkeypatch.setenv(name, value)

    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized engine construction")

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", forbidden)
    with pytest.raises(ValueError):
        await maintenance.run_probe()


@pytest.mark.parametrize("fault", [None, "missing_image", "skipped_smoke", "foreign_sha"])
def test_image_is_joined_to_the_full_successful_ci_deploy_receipt(monkeypatch, fault):
    import subprocess

    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repository")
    receipt = {
        "headSha": SHA,
        "conclusion": "success",
        "jobs": [
            {
                "databaseId": 2,
                "name": "deploy-gcp / Deploy to GCP (staging)",
                "conclusion": "success",
                "steps": [
                    {"name": name, "conclusion": "success"}
                    for name in (
                        "Run migrations (blocking gate)",
                        "Post-deploy smoke turn",
                        "Verify deployed installs match shipped defaults",
                    )
                ],
            }
        ],
    }
    if fault == "skipped_smoke":
        receipt["jobs"][0]["steps"][1]["conclusion"] = "skipped"
    if fault == "foreign_sha":
        receipt["headSha"] = "c" * 40
    monkeypatch.setattr(
        maintenance,
        "gh_json",
        lambda *args: (
            [{"databaseId": 1, "headSha": SHA, "conclusion": "success", "status": "completed"}]
            if args[1] == "list"
            else receipt
        ),
    )
    log = f"deploy-gcp Run migrations (blocking gate) --image {IMAGE}\n"
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *a, **kw: ("missing digest" if fault == "missing_image" else log).encode(),
    )
    if fault:
        with pytest.raises(ValueError):
            maintenance.expected_image(SHA, IMAGE.split("/daimon@")[0])
    else:
        assert maintenance.expected_image(SHA, IMAGE.split("/daimon@")[0]) == IMAGE


@pytest.mark.parametrize("status", ["pending", "claimed"])
async def test_active_wake_refusal_preserves_head_and_claim(db_session, tmp_path, status):
    from uuid import uuid4

    from alembic import command
    from daimon.testing.factories import make_tenant
    from sqlalchemy import text

    tenant = await make_tenant(db_session)
    connection = await db_session.connection()
    config = fixture_config(tmp_path)

    def setup(conn):
        config.attributes["connection"] = conn
        conn.execute(text("DELETE FROM alembic_version"))
        command.upgrade(config, "head")
        conn.execute(
            text("ALTER TABLE task_continuations DROP CONSTRAINT ck_task_continuations_reason")
        )
        conn.execute(
            text(
                "ALTER TABLE task_continuations ADD CONSTRAINT ck_task_continuations_reason "
                "CHECK (reason IN ('task_handoff','private_input_applied','timer','github_access_ready','worker_finished'))"
            )
        )

    await connection.run_sync(setup)
    await connection.execute(
        text(
            "INSERT INTO task_continuations "
            "(tenant_id, platform, parent_channel_id, thread_id, requester_account_id, "
            "requester_external_user_id, target_ma_agent_id, target_name, reason, status, "
            "idempotency_key, lease_owner) VALUES (:tenant,'discord','parent','thread',:account,"
            "'requester','agent','agent','worker_finished',:status,:key,'owned-claim')"
        ),
        {"tenant": tenant.id, "account": uuid4(), "status": status, "key": uuid4()},
    )
    with pytest.raises(ValueError, match="retire active completion"):
        await connection.run_sync(maintenance.roundtrip, config)
    assert list(
        (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars()
    ) == ["0099_merge_main_neutral"]
    assert (
        await connection.execute(text("SELECT status,lease_owner FROM task_continuations"))
    ).one() == (status, "owned-claim")
