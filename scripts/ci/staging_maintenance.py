"""Staging-only migration proof using the existing migration job's secret bindings."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import gzip
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT = "pymc-daimon-staging"
REPOSITORY = "staging-daimon"
TARGET = "0098_external_github_session"
SCHEMA_HASH = "4f7fc55ce6c66ae2b8afe81de946fea255d4e78bcd04a44d9388e4892d658b37"
DOWN_STEPS = (TARGET, "0089_neutral_catalog", "0087_neutral_state", "0087_neutral_state@-1")
SCHEMA_QUERIES = {
    "tables": (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema=current_schema() ORDER BY 1"
    ),
    "columns": (
        "SELECT table_name||'.'||column_name||' '||data_type||' null='||is_nullable||"
        "' default='||coalesce(column_default,'') FROM information_schema.columns "
        "WHERE table_schema=current_schema() ORDER BY 1"
    ),
    "indexes": (
        "SELECT tablename||' '||indexname||' '||indexdef FROM pg_indexes "
        "WHERE schemaname=current_schema() ORDER BY 1"
    ),
    "constraints": (
        "SELECT conrelid::regclass::text||' '||conname||' '||pg_get_constraintdef(oid) "
        "FROM pg_constraint WHERE connamespace=current_schema()::regnamespace ORDER BY 1"
    ),
    "alembic_version": "SELECT version_num FROM alembic_version ORDER BY 1",
}


def require_staging(environment: str, project: str, repository: str, sha: str, target: str) -> None:
    if (environment, project, repository) != ("staging", PROJECT, REPOSITORY):
        raise ValueError("maintenance is restricted to the staging estate")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or target != TARGET:
        raise ValueError("unsupported expected commit or downgrade plan")


def gh_json(*arguments: str):
    return json.loads(subprocess.check_output(["gh", *arguments], stderr=subprocess.DEVNULL))


def expected_image(sha: str, registry: str) -> str:
    repository = os.environ["GITHUB_REPOSITORY"]
    runs = gh_json(
        "run",
        "list",
        "--repo",
        repository,
        "--workflow",
        "ci.yml",
        "--branch",
        "main",
        "--event",
        "push",
        "--commit",
        sha,
        "--json",
        "databaseId,headSha,status,conclusion",
    )
    run = next(
        item
        for item in runs
        if item["headSha"] == sha
        and item["status"] == "completed"
        and item["conclusion"] == "success"
    )
    receipt = gh_json(
        "run",
        "view",
        str(run["databaseId"]),
        "--repo",
        repository,
        "--json",
        "jobs,headSha,conclusion",
    )
    if receipt["headSha"] != sha or receipt["conclusion"] != "success":
        raise ValueError("expected commit has no successful CI receipt")
    jobs = [job for job in receipt["jobs"] if job["name"].endswith("Deploy to GCP (staging)")]
    if len(jobs) != 1 or jobs[0]["conclusion"] != "success":
        raise ValueError("expected commit has no successful staging deploy")
    for name in (
        "Run migrations (blocking gate)",
        "Post-deploy smoke turn",
        "Verify deployed installs match shipped defaults",
    ):
        steps = [step for step in jobs[0]["steps"] if step["name"] == name]
        if len(steps) != 1 or steps[0]["conclusion"] != "success":
            raise ValueError("expected staging deploy skipped a required gate")
    log = subprocess.check_output(
        ["gh", "run", "view", "--repo", repository, "--job", str(jobs[0]["databaseId"]), "--log"],
        stderr=subprocess.DEVNULL,
    ).decode()
    images = {
        match.group()
        for line in log.splitlines()
        if "Run migrations (blocking gate)" in line
        for match in re.finditer(re.escape(registry + "/daimon@sha256:") + r"[0-9a-f]{64}", line)
    }
    if len(images) != 1:
        raise ValueError("CI receipt must identify one immutable migration image")
    return images.pop()


def manifest(source: dict, sha: str, target: str, image: str, name: str) -> dict:
    require_staging(
        os.environ["ENVIRONMENT"], os.environ["PROJECT_ID"], os.environ["REPO"], sha, target
    )
    if not re.fullmatch(r"staging-daimon-maintenance-[0-9]+-[0-9]+", name):
        raise ValueError("invalid isolated maintenance job name")
    number = os.environ["PROJECT_NUMBER"]
    if not number.isdigit() or source["metadata"].get("namespace") not in (PROJECT, number):
        raise ValueError("migration job is not in the staging project namespace")
    result = {
        "apiVersion": "run.googleapis.com/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": number},
        "spec": copy.deepcopy(source["spec"]),
    }
    template = result["spec"]["template"]
    task = template["spec"]["template"]["spec"]
    containers = task["containers"]
    if len(containers) != 1 or containers[0]["image"] != image or "@sha256:" not in image:
        raise ValueError("migration template differs from the expected immutable CI image")
    account = task.get("serviceAccountName", "")
    if not (
        account.endswith(f"@{PROJECT}.iam.gserviceaccount.com")
        or account == f"{number}-compute@developer.gserviceaccount.com"
    ):
        raise ValueError("migration runtime must use its staging service account")
    env = containers[0].get("env", [])
    database = [
        item for item in env if item["name"] in ("DAIMON_DATABASE__URL", "DAIMON_DATABASE_URL")
    ]
    if len(database) != 1 or "secretKeyRef" not in database[0].get("valueFrom", {}):
        raise ValueError("database configuration must come from Secret Manager")
    for item in env:
        secret = item.get("valueFrom", {}).get("secretKeyRef", {}).get("name", "")
        if secret.startswith("projects/") and not any(
            secret.startswith(f"projects/{project}/secrets/") for project in (PROJECT, number)
        ):
            raise ValueError("foreign secret project")
    annotations = template["spec"]["template"].get("metadata", {}).get("annotations", {})
    for instance in annotations.get("run.googleapis.com/cloudsql-instances", "").split(","):
        if instance and not instance.startswith(PROJECT + ":"):
            raise ValueError("foreign database instance")
    for alias in annotations.get("run.googleapis.com/secrets", "").split(","):
        if alias and not re.fullmatch(
            rf"[A-Za-z0-9_-]+:projects/(?:{re.escape(PROJECT)}|{number})/secrets/[A-Za-z0-9_-]+",
            alias,
        ):
            raise ValueError("foreign or malformed secret alias")
    payload = base64.b64encode(gzip.compress(Path(__file__).read_bytes(), mtime=0)).decode()
    containers[0]["command"] = ["/app/.venv/bin/python"]
    containers[0]["args"] = [
        "-c",
        f"import base64,gzip;exec(gzip.decompress(base64.b64decode({payload!r})))",
        "probe",
    ]
    containers[0]["env"] = [
        *database,
        {"name": "MAINTENANCE_SHA", "value": sha},
        {"name": "MAINTENANCE_TARGET", "value": target},
        {"name": "MAINTENANCE_PROJECT", "value": PROJECT},
        {"name": "MAINTENANCE_DATABASE_HOSTS", "value": os.environ["DATABASE_HOSTS"]},
    ]
    task["maxRetries"] = 0
    task["timeoutSeconds"] = "600"
    template["spec"]["taskCount"] = 1
    template["spec"]["parallelism"] = 1
    return result


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def schema(connection) -> dict:
    from sqlalchemy import text

    return {
        key: list(connection.execute(text(sql)).scalars()) for key, sql in SCHEMA_QUERIES.items()
    }


def main_data(connection, columns: list[str]) -> dict:
    from sqlalchemy import text

    projection: dict[str, list[str]] = {}
    for column in columns:
        table, name = column.split(" ", 1)[0].split(".", 1)
        if table != "alembic_version":
            projection.setdefault(table, []).append(name)
    quote = connection.dialect.identifier_preparer.quote
    result = {}
    for table, names in sorted(projection.items()):
        selected = ", ".join(quote(name) for name in names)
        row = connection.execute(
            text(
                "SELECT count(*), md5(coalesce(string_agg(r::text, '|' ORDER BY r::text), '')) "
                f"FROM (SELECT {selected} FROM {quote(table)}) r"
            )
        ).one()
        result[table] = [row[0], row[1]]
    return result


def roundtrip(connection, config) -> dict:
    from alembic import command
    from alembic.script import ScriptDirectory
    from sqlalchemy import text

    config.attributes["connection"] = connection
    head = ScriptDirectory.from_config(config).get_current_head()
    connection.execute(text("SET LOCAL lock_timeout = '5s'"))
    connection.execute(text("SET LOCAL statement_timeout = '60s'"))
    tables = connection.execute(text(SCHEMA_QUERIES["tables"])).scalars().all()
    quote = connection.dialect.identifier_preparer.quote
    connection.execute(
        text("LOCK TABLE " + ", ".join(map(quote, tables)) + " IN ACCESS EXCLUSIVE MODE")
    )
    before = schema(connection)
    before_data = main_data(connection, before["columns"])
    if before["alembic_version"] != [head] or head == TARGET:
        raise ValueError("database must be at the expected image's single newer head")
    if "task_continuations" in tables and connection.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM task_continuations WHERE reason='worker_finished' "
            "AND status IN ('pending','claimed'))"
        )
    ):
        raise ValueError("retire active completion wakes through their owners before maintenance")

    def downgrade():
        for revision in DOWN_STEPS:
            command.downgrade(config, revision)

    probe = connection.begin_nested()
    downgrade()
    restored = schema(connection)
    if restored["alembic_version"] != [TARGET] or digest(restored) != SCHEMA_HASH:
        raise ValueError("restored schema differs from the accepted main proof")
    down_data = main_data(connection, restored["columns"])
    probe.rollback()
    original_data = main_data(connection, restored["columns"])
    if original_data != down_data:
        raise ValueError("downgrade changed original main or seed data")
    downgrade()
    if (
        schema(connection) != restored
        or main_data(connection, restored["columns"]) != original_data
    ):
        raise ValueError("restored fingerprint is not deterministic")
    command.upgrade(config, "head")
    if schema(connection) != before or main_data(connection, restored["columns"]) != original_data:
        raise ValueError("re-upgrade changed schema or original main data")
    if main_data(connection, before["columns"]) != before_data:
        raise ValueError("neutral evidence needs its owner's export/drain plan before maintenance")
    return {
        "heads": [head, TARGET, head],
        "restored_schema_sha256": digest(restored),
        "roundtrip_schema_sha256": digest(before),
        "main_seed_sha256": digest(original_data),
        "main_rows": sum(value[0] for value in original_data.values()),
        "complete_data_sha256": digest(before_data),
    }


async def run_probe() -> dict:
    from alembic.config import Config
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine

    require_staging(
        "staging",
        os.environ["MAINTENANCE_PROJECT"],
        REPOSITORY,
        os.environ["MAINTENANCE_SHA"],
        os.environ["MAINTENANCE_TARGET"],
    )
    url = os.environ.get("DAIMON_DATABASE__URL") or os.environ["DAIMON_DATABASE_URL"]
    endpoint = make_url(url)
    host = endpoint.query.get("host") or endpoint.host
    if host not in json.loads(os.environ["MAINTENANCE_DATABASE_HOSTS"]):
        raise ValueError("database endpoint does not belong to a staging Cloud SQL instance")
    if endpoint.port not in (None, 5432) or endpoint.query.get("port", "5432") != "5432":
        raise ValueError("unsupported database route override")
    engine = create_async_engine(url, hide_parameters=True, echo=False)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                result = await connection.run_sync(roundtrip, Config("alembic.ini"))
                await transaction.commit()
            except BaseException:
                # Never clear neutral evidence or claims to manufacture a
                # successful fingerprint; failed proofs restore original data.
                await transaction.rollback()
                raise
        return {
            "success": True,
            "expected_sha": os.environ["MAINTENANCE_SHA"],
            "committed": True,
            **result,
        }
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        if sys.argv[1] == "manifest":
            source = json.loads(Path(sys.argv[2]).read_text())
            hosts = set()
            for instance in json.loads(Path(sys.argv[4]).read_text()):
                if not instance["connectionName"].startswith(PROJECT + ":"):
                    raise ValueError("foreign Cloud SQL project")
                hosts.add("/cloudsql/" + instance["connectionName"])
                hosts.update(item["ipAddress"] for item in instance.get("ipAddresses", []))
            if not hosts:
                raise ValueError("staging database ownership is unavailable")
            os.environ["DATABASE_HOSTS"] = json.dumps(sorted(hosts))
            image = expected_image(os.environ["EXPECTED_SHA"], os.environ["REGISTRY"])
            Path(sys.argv[3]).write_text(
                json.dumps(
                    manifest(
                        source,
                        os.environ["EXPECTED_SHA"],
                        os.environ["DOWNGRADE_REVISION"],
                        image,
                        os.environ["JOB_NAME"],
                    )
                )
            )
        elif sys.argv[1] == "probe":
            logging.disable(logging.CRITICAL)
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = asyncio.run(run_probe())
            print("STAGING_MAINTENANCE_RESULT " + json.dumps(result, sort_keys=True))
        else:
            raise ValueError("unsupported maintenance command")
    except Exception as error:
        # Neither subprocess output nor driver exceptions are safe to echo.
        print("Staging maintenance refused: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
