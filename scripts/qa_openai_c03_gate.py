"""Offline admission for one separately lead-pinned C03 OptionA attempt."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

ENVELOPE_SHA256 = "91a4bd4b2159e7b3d00533a6aa2c44edcc4168d9a492b6df6eefced52ddb90b1"
CONFIG_SHA256 = "28abe4be3fcedb3a980ee1fb1f3b9f32cff94e9339b7454abaae2f080b9c65f1"
ATTEMPT = "C03-OPENAI-OPTION-A-20261012T053523Z"
LEDGER_ID = "091ee642b92141f88a8f3026edaa1a5a"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(args):
    """Validate before DB/guard/key access; the expected GO digest comes from lead."""
    path = getattr(args, "go_file", None)
    expected = getattr(args, "go_sha256", None)
    if path is None or not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("C03 requires the exact separate lead GO pin")
    raw_go = path.read_bytes()
    if hashlib.sha256(raw_go).hexdigest() != expected:
        raise ValueError("C03 requires the exact separate lead GO pin")
    go = json.loads(raw_go)
    if (
        go.get("fixture_id") != "C03"
        or go.get("model") != "gpt-6-luna"
        or go.get("profile") != "openai.conversation_only"
        or go.get("integration_sha") != args.integration_sha
        or go.get("attempt_identity") != ATTEMPT
        or go.get("output") != str(args.output.resolve())
        or go.get("ledger_id") != LEDGER_ID
        or go.get("envelope_sha256") != ENVELOPE_SHA256
        or go.get("config_sha256") != CONFIG_SHA256
        or digest(args.budget_config) != CONFIG_SHA256
        or go.get("runner_pin_sha256") != digest(args.runner_pin)
    ):
        raise ValueError("C03 GO scope, source, budget or runner pin differs")
    envelope = Path(go["envelope_path"])
    raw_envelope = envelope.read_bytes()
    approval = json.loads(raw_envelope)
    if hashlib.sha256(raw_envelope).hexdigest() != ENVELOPE_SHA256:
        raise ValueError("C03 financial envelope differs from lead signature")
    now = datetime.now(UTC)
    expiry = datetime.fromisoformat(go["expires_at"])
    if (
        expiry.tzinfo is None
        or now >= expiry
        or expiry > datetime.fromisoformat(approval["expires_at"])
    ):
        raise ValueError("C03 GO expired or exceeds envelope expiry")
    binding_path = Path(go["binding_path"])
    raw_binding = binding_path.read_bytes()
    if hashlib.sha256(raw_binding).hexdigest() != go.get("binding_sha256"):
        raise ValueError("C03 scoped binding differs from lead pin")
    binding = json.loads(raw_binding)
    from uuid import UUID

    UUID(binding["tenant_id"])
    UUID(binding["account_id"])
    if (
        binding.get("provider") != "openai"
        or binding.get("profile") != "openai.conversation_only"
        or binding.get("principal_id") != "n11-cert-caller"
        or binding.get("authorization_id") != "lead-go"
        or binding.get("generation") != 1
        or binding.get("config_revision") != 1
        or binding.get("credential_file") != approval["credential_file"]
        or binding.get("attempt_identity") != ATTEMPT
        or not isinstance(binding.get("invocation"), str)
        or not binding["invocation"].startswith("n11:cert:")
    ):
        raise ValueError("C03 binding scope differs from admitted attempt")
    args.c03_binding = binding
    return binding


def verify_harness(checkout: Path, lane: Path):
    """Freeze the lane-only recipe in the reviewed repository head."""
    manifest = checkout / "packages/mux/tests/drivers/openai/c03_runner_manifest.json"
    pinned = json.loads(manifest.read_text())
    required = {
        "live_pending.py",
        "live_smoke.py",
        "live_c03.sh",
        "prepare_live_c03.py",
        "offline_live_pending_test.py",
        "offline_live_c03_test.py",
    }
    if set(pinned["files"]) != required:
        raise ValueError("C03 reviewed recipe omits a changed dependency")
    for name, expected in pinned["files"].items():
        if digest(lane / name) != expected:
            raise ValueError("C03 lane recipe differs from reviewed commit")
