"""Small, explicit evidence contract shared by scripted and browser journeys."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from PIL import Image, ImageChops

Status = Literal["PASS", "FAIL", "PENDING"]
Boundary = Literal[
    "scripted_transport",
    "adapter_entry",
    "backend_effect",
    "browser_dom",
    "browser_pixel",
    "native_client",
    "provider",
]


@dataclass(frozen=True)
class Observation:
    boundary: Boundary
    required: bool
    observed: bool
    detail: str
    artifact: str | None = None


@dataclass
class Check:
    name: str
    status: Status
    observations: list[Observation] = field(default_factory=list[Observation])
    detail: str = ""

    def effective_status(self) -> Status:
        if self.status == "FAIL":
            return "FAIL"
        if any(item.required and not item.observed for item in self.observations):
            return "PENDING"
        return self.status


@dataclass
class RunReport:
    run_id: str
    journey: str
    layer: str
    started_at: datetime
    finished_at: datetime | None = None
    app_input_tokens: int = 0
    app_output_tokens: int = 0
    judge_input_tokens: int = 0
    judge_output_tokens: int = 0
    checks: list[Check] = field(default_factory=list[Check])
    status: Status = "PENDING"

    @property
    def minutes(self) -> float:
        end = self.finished_at or datetime.now(UTC)
        return max(0.0, (end - self.started_at).total_seconds() / 60)

    def finalize(self) -> Status:
        statuses = [check.effective_status() for check in self.checks]
        self.status = (
            "FAIL"
            if "FAIL" in statuses
            else "PASS"
            if statuses and all(item == "PASS" for item in statuses)
            else "PENDING"
        )
        return self.status


def create_artifact_dir(run_id: str, root: Path | None = None) -> Path:
    """Create a private run directory outside the source checkout."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", run_id):
        raise ValueError("run_id must be a plain path component")
    target_root = root or Path(
        os.environ.get("DAIMON_QA_ARTIFACT_ROOT", Path.home() / ".local/state/daimon/qa-journeys")
    )
    checkout = Path(__file__).resolve().parents[2]
    if target_root.resolve().is_relative_to(checkout):
        raise ValueError("QA artifacts must be outside the repository")
    target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = target_root / run_id
    path.mkdir(mode=0o700, exist_ok=False)
    return path


def write_report(report: RunReport, artifact_dir: Path) -> Path:
    report.finalize()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "report.json"
    payload = asdict(report)
    payload["minutes"] = report.minutes
    payload["checks"] = [
        {**asdict(check), "status": check.effective_status()} for check in report.checks
    ]
    path.write_text(json.dumps(payload, default=str, indent=2) + "\n")
    return path


@dataclass(frozen=True)
class ScreenshotDiff:
    step_id: str
    status: Status
    changed: bool | None
    changed_pixels: int | None
    artifact: str | None
    detail: str


def compare_screenshots(
    current: Path, accepted_baseline: Path | None, output_dir: Path, step_id: str
) -> ScreenshotDiff:
    """Write a baseline/current/difference panel only for a changed step.

    The caller selects an already accepted baseline. This function never
    accepts, updates, or silently creates one.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", step_id):
        raise ValueError("step_id must be a plain path component")
    if not current.is_file():
        return ScreenshotDiff(step_id, "PENDING", None, None, None, "current screenshot missing")
    if accepted_baseline is None or not accepted_baseline.is_file():
        return ScreenshotDiff(step_id, "PENDING", None, None, None, "accepted baseline missing")
    with Image.open(accepted_baseline) as baseline_image, Image.open(current) as current_image:
        baseline = baseline_image.convert("RGB")
        observed = current_image.convert("RGB")
    if baseline.size != observed.size:
        return ScreenshotDiff(
            step_id,
            "FAIL",
            True,
            None,
            None,
            f"image dimensions differ: baseline={baseline.size}, current={observed.size}",
        )
    difference = ImageChops.difference(baseline, observed)
    if difference.getbbox() is None:
        return ScreenshotDiff(step_id, "PASS", False, 0, None, "pixels match")
    changed_pixels = sum(1 for pixel in difference.get_flattened_data() if pixel != (0, 0, 0))
    panel = Image.new("RGB", (baseline.width * 3, baseline.height))
    panel.paste(baseline, (0, 0))
    panel.paste(observed, (baseline.width, 0))
    panel.paste(difference, (baseline.width * 2, 0))
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact = output_dir / f"{step_id}-diff.png"
    panel.save(artifact)
    return ScreenshotDiff(step_id, "FAIL", True, changed_pixels, str(artifact), "pixels changed")
