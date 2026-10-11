import json
import subprocess
from datetime import datetime, timedelta

import pytest

from qa.live.config import Pricing
from qa.live.cost import Ledger
from qa.live.deploy_quiet import COMPLETED_JOBS, DEPLOY_JOB, DeployNotQuiet, QuietGate
from qa.live.deployment import run_with_deploy_retry
from qa.live.runner import Executor
from qa.live.schema import Scenario
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.types import Pending, utcnow


@pytest.fixture(autouse=True)
def isolated_completed_cache() -> None:
    COMPLETED_JOBS.clear()


def test_active_deploy_outside_recent_list_blocks_and_completed_age_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = utcnow()
    age = [179]
    active = [True]
    calls = []

    def command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert args[-2:] == ["--repo", "pymc-labs/daimon"]
        if args[1:3] == ["run", "list"]:
            rows = [{"databaseId": 1, "status": "completed", "attempt": 1}]
            if "--status" in args:
                rows = (
                    [{"databaseId": 200, "status": "in_progress", "attempt": 1}]
                    if active[0]
                    else []
                )
            data = rows
        else:
            assert args[1:3] == ["run", "view"]
            job = (
                {"name": DEPLOY_JOB, "status": "in_progress"}
                if args[3] == "200"
                else {
                    "name": DEPLOY_JOB,
                    "status": "completed",
                    "completedAt": (now - timedelta(seconds=age[0])).isoformat(),
                }
            )
            data = {"jobs": [job]}
        return subprocess.CompletedProcess(args, 0, json.dumps(data), "")

    monkeypatch.setattr(subprocess, "run", command)
    monkeypatch.setattr("qa.live.deploy_quiet.utcnow", lambda: now)
    gate = QuietGate()
    assert gate.snapshot()["quiet"] is False
    active[0] = False
    assert gate.snapshot()["quiet"] is False
    # Completion timestamps are immutable; fresh evidence for the next case.
    age[0] = 180
    COMPLETED_JOBS.clear()
    assert QuietGate().snapshot()["quiet"] is True
    assert any("--status" in c for c in calls)


def test_busy_wait_is_bounded_and_never_posts_or_retries(
    monkeypatch: pytest.MonkeyPatch, scenario: Scenario, ledger: Ledger, pricing: Pricing
) -> None:
    clock = [0.0]
    sleeps = []
    monkeypatch.setattr("qa.live.deploy_quiet.time.monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("qa.live.deploy_quiet.time.sleep", sleep)
    gate = QuietGate()
    monkeypatch.setattr(gate, "snapshot", lambda: {"quiet": False})
    backend = FakeBackend()
    monkeypatch.setattr(backend, "deployment_quiet", lambda: gate.wait(timeout_s=65))
    results = run_with_deploy_retry(
        scenario,
        lambda: Executor(backend, FakeJudge(), ledger, pricing, "staging"),
        lambda result: None,
        retry_allowed=lambda: True,
    )
    assert len(results) == 1 and results[0].status == "PENDING"
    assert results[0].deployment and results[0].deployment.interrupted
    assert not backend.events and ledger.charged({results[0].run_id}) == 0
    assert sleeps == [30, 30, 5]


def test_unavailable_github_never_proves_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = QuietGate()

    def unavailable() -> dict[str, bool]:
        raise Pending("GitHub unavailable")

    monkeypatch.setattr(gate, "snapshot", unavailable)
    with pytest.raises(Pending, match="evidence unavailable") as exc:
        gate.wait(timeout_s=900)
    assert not isinstance(exc.value, DeployNotQuiet)


@pytest.mark.parametrize(
    "bad",
    [
        {},
        [{"status": "completed"}],
        [{"databaseId": 1, "status": "completed"}],
        [{"databaseId": 1, "status": "completed", "attempt": 0}],
        [{"databaseId": 1, "status": "completed", "attempt": True}],
    ],
)
def test_malformed_run_inventory_never_proves_quiet(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    monkeypatch.setattr(QuietGate, "query", lambda self, args: bad)
    with pytest.raises(Pending, match="inventory is incomplete"):
        QuietGate().snapshot()


@pytest.mark.parametrize(
    "job",
    [
        {"name": DEPLOY_JOB, "status": "completed"},
        {
            "name": DEPLOY_JOB,
            "status": "completed",
            "conclusion": "skipped",
            "completedAt": "2026-10-10T00:00:00Z",
        },
    ],
)
def test_missing_or_skipped_completion_cannot_supply_deploy_age(
    monkeypatch: pytest.MonkeyPatch, job: object
) -> None:
    def query(self: QuietGate, args: list[str]) -> object:
        if args[:2] == ["run", "view"]:
            return {"jobs": [job]}
        return (
            [] if "--status" in args else [{"databaseId": 1, "status": "completed", "attempt": 1}]
        )

    monkeypatch.setattr(QuietGate, "query", query)
    gate = QuietGate()
    if isinstance(job, dict) and job.get("conclusion") == "skipped":
        with pytest.raises(Pending, match="no actual staging deployment"):
            gate.snapshot()
    else:
        with pytest.raises(Pending, match="timestamp is missing"):
            gate.snapshot()


def test_real_reusable_deploy_shape_and_shared_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.fromisoformat("2026-10-10T14:00:00+00:00")
    active = [True]
    views: list[str] = []

    def query(self: QuietGate, args: list[str]) -> object:
        if args[:2] == ["run", "list"]:
            if "--status" in args:
                if args[args.index("--status") + 1] == "queued" and active[0]:
                    return [{"databaseId": 200, "status": "queued", "attempt": 1}]
                return []
            return [{"databaseId": 38056819962, "status": "completed", "attempt": 1}]
        identity = args[2]
        views.append(identity)
        if identity == "200":
            return {"jobs": [{"name": "Deploy to GCP (staging)", "status": "queued"}]}
        # Literal job metadata from real main run 38056819962.
        return {
            "jobs": [
                {
                    "name": "Deploy to GCP (staging) / Deploy to GCP (staging)",
                    "status": "completed",
                    "conclusion": "success",
                    "startedAt": "2026-10-10T13:48:10Z",
                    "completedAt": "2026-10-10T13:53:36Z",
                }
            ]
        }

    monkeypatch.setattr(QuietGate, "query", query)
    monkeypatch.setattr("qa.live.deploy_quiet.utcnow", lambda: now)
    assert QuietGate().snapshot()["quiet"] is False
    active[0] = False
    second = QuietGate().snapshot()
    assert second["quiet"] is True and second["age_s"] == 384
    assert views.count("38056819962") == 1
    assert views.count("200") == 1


def test_unavailable_evidence_fails_fast_without_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def unavailable(self: QuietGate) -> object:
        calls.append(1)
        raise Pending("authentication unavailable")

    def sleep(seconds: float) -> None:
        pytest.fail("permanent evidence failure must not burn the observation window")

    monkeypatch.setattr(QuietGate, "snapshot", unavailable)
    monkeypatch.setattr("qa.live.deploy_quiet.time.sleep", sleep)
    with pytest.raises(Pending, match="authentication unavailable"):
        QuietGate().wait()
    assert calls == [1]


@pytest.mark.parametrize("duration, wait_s", [(0, 65), (2, 55)])
def test_real_query_busy_deploy_outlasts_wait_without_harness_error(
    monkeypatch: pytest.MonkeyPatch,
    scenario: Scenario,
    ledger: Ledger,
    pricing: Pricing,
    duration: int,
    wait_s: int,
) -> None:
    clock = [0.0]
    budgets: list[float] = []
    sleeps: list[float] = []
    now = utcnow()
    monkeypatch.setattr("qa.live.deploy_quiet.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("qa.live.deploy_quiet.utcnow", lambda: now)

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    def command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        budget = kwargs["timeout"]
        assert isinstance(budget, (float, int)) and budget > 0
        budgets.append(float(budget))
        if budget < duration:
            clock[0] += float(budget)
            raise subprocess.TimeoutExpired(args, float(budget))
        clock[0] += duration
        if args[1:3] == ["run", "list"]:
            rows = [{"databaseId": 1, "status": "completed", "attempt": 1}]
            if "--status" in args:
                rows = (
                    [{"databaseId": 2, "status": "in_progress", "attempt": 1}]
                    if args[args.index("--status") + 1] == "in_progress"
                    else []
                )
            data = rows
        else:
            job = (
                {"name": DEPLOY_JOB + " / " + DEPLOY_JOB, "status": "in_progress"}
                if args[3] == "2"
                else {
                    "name": DEPLOY_JOB + " / " + DEPLOY_JOB,
                    "status": "completed",
                    "conclusion": "success",
                    "completedAt": (now - timedelta(seconds=600)).isoformat(),
                }
            )
            data = {"jobs": [job]}
        return subprocess.CompletedProcess(args, 0, json.dumps(data), "")

    monkeypatch.setattr("qa.live.deploy_quiet.time.sleep", sleep)
    monkeypatch.setattr(subprocess, "run", command)
    backend = FakeBackend()
    monkeypatch.setattr(backend, "deployment_quiet", lambda: QuietGate().wait(timeout_s=wait_s))
    results = run_with_deploy_retry(
        scenario,
        lambda: Executor(backend, FakeJudge(), ledger, pricing, "staging"),
        lambda result: None,
        retry_allowed=lambda: True,
    )
    assert len(results) == 1 and results[0].status == "PENDING"
    assert results[0].deployment and results[0].deployment.interrupted
    assert not results[0].errors and not backend.events
    assert ledger.charged({results[0].run_id}) == 0
    assert 0 < min(budgets) <= max(budgets) <= 30
    assert wait_s <= clock[0] <= wait_s + 60
    if duration:
        # Deadline lies inside the second real snapshot, not a sleep.
        assert sleeps == [30] and clock[0] == 60
    else:
        assert sleeps == [30, 30, 5] and clock[0] == 65


def test_real_github_outage_during_query_grace_is_alertable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    gate = QuietGate()
    snapshots = [0]
    monkeypatch.setattr("qa.live.deploy_quiet.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(
        "qa.live.deploy_quiet.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def snapshot() -> dict[str, bool]:
        snapshots[0] += 1
        if snapshots[0] == 1:
            clock[0] = 10
            return {"quiet": False}
        clock[0] = 56
        # This is an actual authenticated transport query, not a mocked
        # exhausted-budget Pending. It retains grace despite the wait expiring.
        gate.query(["run", "list"])
        pytest.fail("GitHub auth error must refuse the observation")

    def command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(args, 4, "", "authentication required")

    monkeypatch.setattr(gate, "snapshot", snapshot)
    monkeypatch.setattr(subprocess, "run", command)
    with pytest.raises(Pending, match="GitHub deployment evidence unavailable") as exc:
        gate.wait(timeout_s=55)
    assert not isinstance(exc.value, DeployNotQuiet)


class RerunCI:
    """Real `gh` subprocess shape for one run id across workflow rerun attempts."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.attempt = 1
        self.status = "completed"
        self.deploy_age = {1: 600}
        self.views: list[tuple[str, str]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[-2:] == ["--repo", "pymc-labs/daimon"]
        if args[1:3] == ["run", "list"]:
            assert args[args.index("--json") + 1] == "databaseId,status,attempt"
            rows = [
                {"databaseId": 42, "status": self.status, "attempt": self.attempt},
                {"databaseId": 41, "status": "completed", "attempt": 1},
            ]
            if "--status" in args:
                rows = [r for r in rows if r["status"] == args[args.index("--status") + 1]]
            data: object = rows
        else:
            identity, attempt = args[3], args[args.index("--attempt") + 1]
            self.views.append((identity, attempt))
            name = DEPLOY_JOB + " / " + DEPLOY_JOB
            if identity == "41":
                job = {"name": name, "status": "completed", "conclusion": "success"}
                job["completedAt"] = (self.now - timedelta(seconds=900)).isoformat()
            elif int(attempt) == self.attempt and self.status != "completed":
                job = {"name": name, "status": "in_progress"}
            else:
                age = self.deploy_age[int(attempt)]
                job = {"name": name, "status": "completed", "conclusion": "success"}
                job["completedAt"] = (self.now - timedelta(seconds=age)).isoformat()
            data = {"jobs": [job]}
        return subprocess.CompletedProcess(args, 0, json.dumps(data), "")


def test_cached_run_rerun_active_then_completed_refuses_until_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [datetime.fromisoformat("2026-10-11T14:00:00+00:00")]
    ci = RerunCI(now[0])
    monkeypatch.setattr(subprocess, "run", ci)
    monkeypatch.setattr("qa.live.deploy_quiet.utcnow", lambda: now[0])
    first = QuietGate().snapshot()
    assert first["quiet"] is True and first["age_s"] == 600
    ci.attempt, ci.status = 2, "in_progress"
    active = QuietGate().snapshot()
    assert active["quiet"] is False
    assert active["busy"] == [{"run_id": "42", "attempt": 2, "status": "in_progress"}]
    ci.status, ci.deploy_age[2] = "completed", 60
    done = QuietGate().snapshot()
    assert done["quiet"] is False and done["age_s"] == 60
    with pytest.raises(DeployNotQuiet):
        QuietGate().wait(timeout_s=0)
    # Once the new attempt's deploy has settled, the cached attempt-2 jobs suffice.
    now[0] += timedelta(seconds=120)
    settled = QuietGate().snapshot()
    assert settled["quiet"] is True and settled["age_s"] == 180
    assert ci.views == [("42", "1"), ("41", "1"), ("42", "2"), ("42", "2")]


def test_rerun_completed_entirely_between_snapshots_uses_new_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.fromisoformat("2026-10-11T14:00:00+00:00")
    ci = RerunCI(now)
    monkeypatch.setattr(subprocess, "run", ci)
    monkeypatch.setattr("qa.live.deploy_quiet.utcnow", lambda: now)
    assert QuietGate().snapshot()["age_s"] == 600
    ci.attempt, ci.deploy_age[2] = 2, 179
    rerun = QuietGate().snapshot()
    assert rerun["quiet"] is False and rerun["age_s"] == 179
    assert rerun["last_completed"] == (now - timedelta(seconds=179)).isoformat()
    with pytest.raises(DeployNotQuiet):
        QuietGate().wait(timeout_s=0)
    assert ("42", "2") in ci.views and set(COMPLETED_JOBS) == {("41", 1), ("42", 1), ("42", 2)}


def test_rerun_starting_between_inventories_uses_newest_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    views: list[list[str]] = []

    def query(self: QuietGate, args: list[str]) -> object:
        if args[:2] == ["run", "list"]:
            if "--status" not in args:
                return [{"databaseId": 7, "status": "completed", "attempt": 1}]
            if args[args.index("--status") + 1] == "queued":
                return [{"databaseId": 7, "status": "queued", "attempt": 2}]
            return []
        views.append(args)
        return {"jobs": [{"name": DEPLOY_JOB, "status": "queued"}]}

    monkeypatch.setattr(QuietGate, "query", query)
    with pytest.raises(Pending, match="no actual staging deployment"):
        QuietGate().snapshot()
    assert views == [["run", "view", "7", "--attempt", "2", "--json", "jobs"]]
