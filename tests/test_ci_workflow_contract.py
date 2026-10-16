"""Keep the CI verdict and image-promotion gates linked as workflows evolve."""

import glob
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_ROOT = _WORKFLOWS.parents[1]


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOWS / name).read_text())


def test_ci_pytest_commands_collect_top_level_and_performance_tests() -> None:
    """Expand the paths passed to pytest, so newly added tests remain selected."""
    selected: set[Path] = set()
    for job in _workflow("ci.yml")["jobs"].values():
        for step in job.get("steps", []):
            command = step.get("run", "")
            if "uv run pytest" not in command:
                continue
            tokens = shlex.split(command)
            paths = tokens[tokens.index("pytest") + 1 :]
            for token in paths:
                if token.startswith("-"):
                    break
                for match in glob.glob(str(_ROOT / token)):
                    path = Path(match)
                    if path.is_dir():
                        selected.update(path.rglob("test_*.py"))
                    elif path.is_file():
                        selected.add(path)

    expected = {
        *(_ROOT / "tests").glob("test_*.py"),
        *(_ROOT / "tests/performance").glob("test_*.py"),
    }
    assert expected, "the selection check needs test files to inspect"
    assert expected <= selected, f"CI omits: {sorted(expected - selected)}"


def test_shards_and_candidate_are_all_deploy_prerequisites() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    deploy = jobs["deploy-gcp"]
    assert jobs["pytest-core"]["strategy"]["matrix"]["shard"] == [0, 1, 2, 3]
    assert jobs["pytest-mcp"]["strategy"]["matrix"]["shard"] == [0, 1]
    for job_name in ("pytest-core", "pytest-mcp"):
        job = jobs[job_name]
        shards = job["strategy"]["matrix"]["shard"]
        command = next(
            step["run"] for step in job["steps"] if "uv run pytest" in step.get("run", "")
        )
        assert f"--ci-shard-count={len(shards)}" in command
    assert set(deploy["needs"]) == {
        "lint",
        "pytest-core",
        "pytest-mcp",
        "pytest-discord",
        "pytest-slack",
        "pytest-teams",
        "pytest-parity",
        "pytest-notebook-host",
        "pytest-report-host",
        "pytest-mux",
        "docker-publish-candidate",
    }
    # No status override: GitHub's default needs gate skips after failure/skip.
    assert not any(status in deploy["if"] for status in ("always()", "failure()", "cancelled()"))
    assert "github.event_name == 'push'" in jobs["docker-publish-candidate"]["if"]
    assert "refs/heads/main" in jobs["docker-publish-candidate"]["if"]
    assert "vars.GCP_WIF_PROVIDER == ''" in jobs["docker-build"]["if"]
    assert "docker-publish-candidate" not in jobs["docker-build"].get("needs", [])


def test_candidate_is_not_promotable_until_staging_gates_pass() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    candidate = jobs["docker-publish-candidate"]
    steps = candidate["steps"]
    tag_script = next(step["run"] for step in steps if step.get("name") == "Compute candidate tags")
    assert 'TAG="ci-${{ github.run_id }}-${{ github.run_attempt }}"' in tag_script
    assert "$SHORT" not in tag_script
    for image in ("daimon", "notebook", "report"):
        assert candidate["outputs"][f"{image}-digest"].endswith(".outputs.digest }}")
        assert jobs["deploy-gcp"]["with"][f"{image}_digest"] == (
            f"${{{{ needs.docker-publish-candidate.outputs.{image}-digest }}}}"
        )

    deploy = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in deploy]
    promotable = names.index("Mark tested staging images promotable")
    for gate in (
        "Run migrations (blocking gate)",
        "Deploy mcp revision",
        "Refresh worker VM containers (recreate in place)",
        "Wait for notebook-host VM refresh",
        "Health gate",
        "Post-deploy smoke turn",
        "Let the adapters' boot reconcile settle",
        "Verify deployed installs match shipped defaults",
    ):
        assert names.index(gate) < promotable
    script = deploy[promotable]["run"]
    assert "gcloud artifacts tags create" in script
    assert "gcloud artifacts docker tags add" not in script
    assert script.rfind("steps.tags.outputs.daimon") > script.rfind("steps.tags.outputs.report")
    assert deploy[promotable]["if"] == "steps.images.outputs.prebuilt == 'true'"


def test_defaults_verify_qa_exception_is_staging_only() -> None:
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    verify = next(
        step
        for step in steps
        if step.get("name") == "Verify deployed installs match shipped defaults"
    )
    assert verify["env"]["STAGING_QA_TENANT"] == "${{ vars.STAGING_QA_TENANT }}"
    script = verify["run"]
    assert (
        'if [ "${{ inputs.environment }}" = staging ] && [ -n "$STAGING_QA_TENANT" ]; then'
        in script
    )
    assert "--warn-tenant $STAGING_QA_TENANT" in script
    assert "defaults verify $verify_tenant_arg" in script


def test_prebuilt_staging_is_pinned_and_production_rebuilds() -> None:
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    select = next(step for step in steps if step.get("id") == "images")
    assert "Prebuilt digests are restricted to staging" in select["run"]
    assert "^sha256:[0-9a-f]{64}$" in select["run"]
    assert "gcloud artifacts docker images describe" in select["run"]
    for step in steps:
        if step.get("name", "").startswith("Build + push"):
            assert step["if"] == "steps.images.outputs.prebuilt != 'true'"
    promote = _workflow("promote.yml")["jobs"]
    assert promote["deploy"]["needs"] == "validate"
    assert promote["deploy"]["with"]["environment"] == "production"
    assert "daimon_digest" not in promote["deploy"]["with"]


@pytest.mark.parametrize(
    ("environment", "digests", "prebuilt"),
    [
        ("staging", ("", "", ""), False),
        ("staging", ("sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64), True),
        ("staging", ("sha256:" + "a" * 64, "", ""), None),
        ("staging", ("sha256:" + "A" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64), None),
        (
            "staging",
            ("sha256:" + "a" * 64 + "\nextra", "sha256:" + "b" * 64, "sha256:" + "c" * 64),
            None,
        ),
        ("production", ("sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64), None),
    ],
)
def test_image_selection_shell_validation(
    tmp_path: Path,
    environment: str,
    digests: tuple[str, str, str],
    prebuilt: bool | None,
) -> None:
    """Execute the workflow's Bash, including its full-value digest check."""
    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "images")
    for expression, value in {
        "${{ inputs.environment }}": environment,
        "${{ vars.GCP_PROJECT_ID }}": "test-project",
        "${{ steps.tags.outputs.daimon }}": "example/daimon:abc1234",
        "${{ steps.tags.outputs.notebook }}": "example/notebook-host:abc1234",
        "${{ steps.tags.outputs.report }}": "example/report-host:abc1234",
    }.items():
        script = script.replace(expression, value)
    output = tmp_path / "outputs"
    env = {
        **os.environ,
        "GITHUB_OUTPUT": str(output),
        "AR_HOST": "example.test",
        "REPO": "images",
        "DAIMON_DIGEST": digests[0],
        "NOTEBOOK_DIGEST": digests[1],
        "REPORT_DIGEST": digests[2],
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", "gcloud() { :; }\n" + script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if prebuilt is None:
        assert result.returncode != 0
        assert not output.exists() or "prebuilt=true" not in output.read_text()
    else:
        assert result.returncode == 0, result.stderr
        lines = output.read_text().splitlines()
        assert f"prebuilt={str(prebuilt).lower()}" in lines
        if prebuilt:
            assert f"daimon=example.test/test-project/images/daimon@{digests[0]}" in lines
        else:
            assert "daimon=example/daimon:abc1234" in lines


@pytest.mark.parametrize("state", ["first", "repeat", "missing-minters"])
def test_mcp_cutover_shell_drains_legacy_turns_before_verifier(
    tmp_path: Path,
    state: str,
) -> None:
    """Execute the actual cutover Bash against an offline clock and Cloud Run receipt."""
    import json

    from daimon.core.turn.ceiling import TURN_CEILING_S

    steps = _workflow("deploy.yml")["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in steps]
    cutover_name = "Drain legacy MCP turns before first verifier cutover"
    assert names.index("Refresh worker VM containers (recreate in place)") < names.index(
        cutover_name
    )
    assert names.index("Wait for notebook-host VM refresh") < names.index(cutover_name)
    assert names.index(cutover_name) < names.index("Deploy mcp revision")
    script = next(step["run"] for step in steps if step.get("id") == "mcp_cutover")
    script = script.replace("${{ vars.GCP_AR_REPO }}", "offline")
    clock = tmp_path / "clock"
    clock.write_text("1000")
    if state != "missing-minters":
        (tmp_path / "workers-refreshed-at").write_text("1000")
    env_rows = [{"name": "UNRELATED_SECRET", "value": "do-not-print"}]
    cutoff = "2026-10-16T00:00:00Z"
    if state == "repeat":
        env_rows.append({"name": "DAIMON_MCP__LEGACY_TOKEN_CUTOFF", "value": cutoff})
    receipt = {"spec": {"template": {"spec": {"containers": [{"env": env_rows}]}}}}
    (tmp_path / "service.json").write_text(json.dumps(receipt))
    commands = {
        "gcloud": '#!/bin/sh\ncat "$RUNNER_TEMP/service.json"\n',
        "date": '#!/bin/sh\nif [ "$1" = +%s ]; then cat "$RUNNER_TEMP/clock"; else echo 2026-10-16T01:00:00Z; fi\n',
        "sleep": '#!/bin/sh\necho "$(( $(cat "$RUNNER_TEMP/clock") + $1 ))" > "$RUNNER_TEMP/clock"\n',
    }
    for command, body in commands.items():
        executable = tmp_path / command
        executable.write_text(body)
        executable.chmod(0o700)
    output = tmp_path / "outputs"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "REGION": "offline",
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "do-not-print" not in result.stdout + result.stderr
    if state == "missing-minters":
        assert result.returncode != 0
        assert not output.exists()
    elif state == "first":
        assert result.returncode == 0, result.stderr
        assert int(clock.read_text()) >= 1000 + TURN_CEILING_S + 60
        assert output.read_text() == "cutoff=2026-10-16T01:00:00Z\n"
    else:
        assert result.returncode == 0, result.stderr
        assert clock.read_text() == "1000", "a completed cutover does not drain again"
        assert output.read_text() == f"cutoff={cutoff}\n"
