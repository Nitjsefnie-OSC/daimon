"""Tests for Discord CI failure artifacts."""

from types import SimpleNamespace

from packages.adapters.discord.tests import conftest as discord_conftest


def test_failure_report_is_written_once_with_traceback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DAIMON_PYTEST_HANG_DIR", str(tmp_path))
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    monkeypatch.setattr(discord_conftest, "_xdist_enabled", True)
    monkeypatch.setattr(discord_conftest, "_xdist_worker", True)
    report = SimpleNamespace(
        failed=True,
        when="call",
        nodeid="test_example.py::test_failure",
        longreprtext="AssertionError: expected result\ntrace line",
    )

    discord_conftest.pytest_runtest_logreport(report)  # type: ignore[arg-type]

    files = list(tmp_path.glob("discord-pytest-failure-gw1-*.txt"))
    assert len(files) == 1
    contents = files[0].read_text(encoding="utf-8")
    assert "worker: gw1" in contents
    assert "phase: call" in contents
    assert "nodeid: test_example.py::test_failure" in contents
    assert "AssertionError: expected result\ntrace line" in contents


def test_xdist_controller_does_not_duplicate_worker_report(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DAIMON_PYTEST_HANG_DIR", str(tmp_path))
    monkeypatch.setattr(discord_conftest, "_xdist_enabled", True)
    monkeypatch.setattr(discord_conftest, "_xdist_worker", False)
    report = SimpleNamespace(
        failed=True,
        when="teardown",
        nodeid="test_example.py::test_failure",
        longreprtext="teardown error",
    )

    discord_conftest.pytest_runtest_logreport(report)  # type: ignore[arg-type]

    assert list(tmp_path.glob("discord-pytest-failure-*.txt")) == []


def test_serial_failure_report_uses_main_worker(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DAIMON_PYTEST_HANG_DIR", str(tmp_path))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setattr(discord_conftest, "_xdist_enabled", False)
    monkeypatch.setattr(discord_conftest, "_xdist_worker", False)
    report = SimpleNamespace(
        failed=True,
        when="setup",
        nodeid="test_example.py::test_setup_failure",
        longreprtext="fixture setup failed",
    )

    discord_conftest.pytest_runtest_logreport(report)  # type: ignore[arg-type]

    files = list(tmp_path.glob("discord-pytest-failure-main-*.txt"))
    assert len(files) == 1
    assert "phase: setup" in files[0].read_text(encoding="utf-8")
