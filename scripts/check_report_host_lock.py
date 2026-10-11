"""Compare the report-host environment with its locked uv requirements export."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


def expected_versions(requirements: str, project: dict[str, str]) -> dict[str, str]:
    """Select requirements whose markers apply to this build platform."""
    expected = {canonicalize_name(project["name"]): project["version"]}
    for line in requirements.splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        requirement = Requirement(line.rstrip().removesuffix("\\").strip())
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        pins = list(requirement.specifier)
        if len(pins) != 1 or pins[0].operator != "==":
            raise ValueError(f"expected one exact pin: {requirement}")
        name = canonicalize_name(requirement.name)
        version = pins[0].version
        if name in expected:
            raise ValueError(f"duplicate active requirement: {name}")
        expected[name] = version
    return expected


def installed_versions(python: str) -> dict[str, str]:
    """Read metadata from the actual runtime Python, not the checker venv."""
    code = (
        "import importlib.metadata as m, json; "
        "print(json.dumps([(d.metadata['Name'], d.version) for d in m.distributions()]))"
    )
    result = subprocess.run([python, "-c", code], check=True, capture_output=True, text=True)
    installed: dict[str, str] = {}
    for raw_name, version in json.loads(result.stdout):
        name = canonicalize_name(raw_name)
        if name in installed:
            raise ValueError(f"duplicate installed distribution: {name}")
        installed[name] = version
    return installed


def differences(expected: dict[str, str], installed: dict[str, str]) -> list[str]:
    errors = []
    for name in sorted(expected.keys() | installed.keys()):
        want, got = expected.get(name), installed.get(name)
        if want is None:
            errors.append(f"extra {name}=={got}")
        elif got is None:
            errors.append(f"missing {name}=={want}")
        elif Version(want) != Version(got):
            errors.append(f"{name}: locked {want}, installed {got}")
    return errors


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: check_report_host_lock.py REQUIREMENTS PYTHON PYPROJECT", file=sys.stderr)
        return 2
    requirements, python, pyproject = sys.argv[1:]
    project = tomllib.loads(Path(pyproject).read_text())["project"]
    expected = expected_versions(Path(requirements).read_text(), project)
    errors = differences(expected, installed_versions(python))
    if errors:
        print("report-host lock mismatch:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print(f"report-host lock matches {len(expected)} installed distributions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
