from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PIL import Image

from qa.journeys.evidence import (
    Check,
    Observation,
    RunReport,
    compare_screenshots,
    create_artifact_dir,
    write_report,
)


def test_missing_required_native_observation_is_pending(tmp_path: Path) -> None:
    started = datetime.now(UTC) - timedelta(seconds=90)
    report = RunReport("offline-example", "discord-turn", "scripted", started)
    report.finished_at = started + timedelta(seconds=90)
    report.checks = [
        Check("delivered reply", "PASS", [Observation("adapter_entry", True, True, "reply")]),
        Check("native card", "PASS", [Observation("native_client", True, False, "no client")]),
    ]
    assert report.finalize() == "PENDING"
    assert report.minutes == 1.5
    path = write_report(report, tmp_path)
    document = json.loads(path.read_text())
    assert document["status"] == "PENDING"
    assert document["checks"][1]["status"] == "PENDING"
    assert document["minutes"] == 1.5
    assert document["app_input_tokens"] == 0
    assert document["judge_output_tokens"] == 0


def test_explicit_failure_survives_missing_observation() -> None:
    report = RunReport("failed", "journey", "browser", datetime.now(UTC))
    report.checks = [Check("backend", "FAIL", [Observation("browser_dom", True, False, "missing")])]
    assert report.finalize() == "FAIL"


def test_private_artifact_directory_rejects_checkout(tmp_path: Path) -> None:
    directory = create_artifact_dir("example-1", tmp_path)
    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError, match="outside the repository"):
        create_artifact_dir("bad", Path(__file__).resolve().parents[1])
    with pytest.raises(ValueError, match="plain path component"):
        create_artifact_dir("../escape", tmp_path)


def test_screenshot_diff_flags_only_deliberately_changed_step(tmp_path: Path) -> None:
    accepted = tmp_path / "accepted.png"
    unchanged = tmp_path / "unchanged.png"
    changed = tmp_path / "changed.png"
    Image.new("RGB", (4, 3), "white").save(accepted)
    Image.new("RGB", (4, 3), "white").save(unchanged)
    image = Image.new("RGB", (4, 3), "white")
    image.putpixel((2, 1), (0, 0, 0))
    image.save(changed)
    output = tmp_path / "diffs"

    same = compare_screenshots(unchanged, accepted, output, "same-step")
    different = compare_screenshots(changed, accepted, output, "changed-step")
    missing = compare_screenshots(changed, None, output, "no-baseline")

    assert same.status == "PASS" and same.changed is False and same.artifact is None
    assert different.status == "FAIL" and different.changed_pixels == 1
    assert different.artifact is not None
    with Image.open(different.artifact) as panel:
        assert panel.size == (12, 3)
        assert panel.getpixel((10, 1)) == (255, 255, 255)
    assert missing.status == "PENDING"
    assert sorted(path.name for path in output.iterdir()) == ["changed-step-diff.png"]
