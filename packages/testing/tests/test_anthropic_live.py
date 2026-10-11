"""Mock-transport proof only: private temporary ledgers, no key/paid calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import httpx
import pytest
from daimon.testing import anthropic_live as live
from daimon.testing.isolated_sdk import TLS_PROXY_ENV_VARS
from daimon.testing.ma import send_events_response
from daimon.testing.ma_models import ma_session
from mux.conformance.budget import BudgetConfig
from mux.conformance.test_budget import receipts
from mux.conformance.test_exact_spend import anthropic_session_guard
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import SessionSpec
from mux.errors import ProviderError

SCOPE = Scope(
    tenant_id="test-tenant",
    account_id="test-account",
    principal_id="probe",
    authorization_id="offline-owned",
)


def ref(id_: str, kind: str) -> ResourceRef:
    return ResourceRef(
        id=id_,
        kind=kind,
        provider="anthropic",
        account_scope_id="test-owned",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in tuple(os.environ):
        if (
            name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GEMINI_", "GOOGLE_"))
            or name.upper() in TLS_PROXY_ENV_VARS
        ):
            monkeypatch.delenv(name)


def admission(tmp_path, monkeypatch, package="A7"):
    guard = anthropic_session_guard(tmp_path)
    config = BudgetConfig.load(guard.config_path)
    monkeypatch.setattr(
        live, "HOST_CONFIG_SHA256", hashlib.sha256(guard.config_path.read_bytes()).hexdigest()
    )
    # Guard's public checkpoint exposes its ledger identity; no private API used.
    checkpoint = json.loads(guard.checkpoint_path.read_text())
    monkeypatch.setattr(live, "LEDGER_ID", checkpoint["binding"]["ledger_id"])
    args = live.Admission(
        package,
        {"credential_file": "not-read.env"},
        "a" * 64,
        guard.config_path,
        guard.spend_path,
        live.LiveBinding(
            scope=SCOPE,
            account_scope_id="test-owned",
            environment=ref("env-test", "environment"),
            agent=ref("agent-test", "agent"),
            database_url="unused-owned-db",
        ),
        tmp_path / "evidence",
        300 if package == "A7" else 600,
        "b" * 40,
    )
    return args, guard, config


class Native:
    def __init__(self, fault=""):
        self.fault = fault
        self.requests = []
        self.created = 0

    async def handle(self, request):
        path = request.url.path
        self.requests.append((request.method, path))
        if path == "/v1/sessions":
            self.created += 1
            if self.fault == "lost-create":
                raise httpx.ReadError("ack lost", request=request)
            return httpx.Response(
                200,
                json=ma_session(
                    id=f"sess-{self.created}", agent_id="agent-test", model=live.MODEL
                ).model_dump(mode="json"),
            )
        if path.endswith("/archive"):
            if self.fault == "lost-archive":
                raise httpx.ReadError("archive ack lost", request=request)
            return httpx.Response(
                200,
                json=ma_session(id=path.split("/")[-2], model=live.MODEL).model_dump(mode="json"),
            )
        if path.endswith("/events"):
            if self.fault == "lost-send" and any(
                item["type"] == "user.message" for item in json.loads(request.content)["events"]
            ):
                raise httpx.ReadError("send ack lost", request=request)
            return send_events_response()
        raise AssertionError("unexpected mock-native request")


async def create(context):
    return await context.driver.sessions.create(
        SCOPE,
        SessionSpec(
            agent=ref("agent-test", "agent"),
            agent_revision=Revision(local=1, native="1"),
            environment=ref("env-test", "environment"),
            config_revision=0,
        ),
        key="one-attempt",
    )


async def test_reservation_precedes_key_client_and_sessions_and_settles_once(tmp_path, monkeypatch):
    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()
    order = []

    def key(path):
        assert len(guard.report()) == 1 and guard.report()[0].held_usd == Decimal(".35")
        order.append("key")
        return "offline-explicit-key"

    def transport():
        assert order == ["key"]
        order.append("client")
        return httpx.MockTransport(native.handle)

    async def invoke(context):
        assert context.client.max_retries == 0
        await create(context)
        return {"status": "pass"}

    result = await live.run_live(args, invoke, secret_loader=key, transport_factory=transport)
    assert order == ["key", "client"]
    assert native.requests == [
        ("POST", "/v1/sessions"),
        ("POST", "/v1/sessions/sess-1/events"),
        ("POST", "/v1/sessions/sess-1/archive"),
    ]
    rows = receipts(guard)
    assert len(rows) == 2
    assert result["spend"]["actual_usd"] is None and Decimal(
        result["spend"]["held_usd"]
    ) == Decimal(".35")
    receipt = json.loads((args.output / "receipt.json").read_text())
    assert (
        receipt["native_budget_sent"] is False and receipt["provider_token_caps_enforced"] is False
    )
    assert receipt["created_resources"] == [["session", "sess-1"]]
    assert receipt["archived_sessions"] == ["sess-1"]
    assert "offline-explicit-key" not in (args.output / "receipt.json").read_text()
    with pytest.raises(live.LiveRefused, match="already has an attempt"):
        await live.run_live(
            replace(args, output=tmp_path / "different"),
            invoke,
            secret_loader=Mock(side_effect=AssertionError),
        )
    assert native.created == 1 and len(guard.report()) == 1


@pytest.mark.parametrize("fault", ["lost-create", "lost-send", "lost-archive"])
async def test_lost_ack_never_reposts_and_keeps_full_hold(tmp_path, monkeypatch, fault):
    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native(fault)

    async def invoke(context):
        session = await create(context)
        from mux.contracts.actions import UserMessage
        from mux.contracts.events import TextPart

        await context.driver.events.send(
            SCOPE, session.ref, (UserMessage(content=(TextPart(text="probe"),)),), key="one-send"
        )
        return {"status": "pass"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(native.handle),
    )
    assert native.requests.count(("POST", "/v1/sessions")) == 1
    assert native.requests.count(("POST", "/v1/sessions/sess-1/archive")) <= 1
    assert guard.report()[0].actual_usd is None and guard.report()[0].held_usd == Decimal(".35")


async def test_total_deadline_interrupts_archives_then_settles(tmp_path, monkeypatch):
    args, guard, _ = admission(tmp_path, monkeypatch)
    args = replace(args, deadline_s=1)
    native = Native()
    cancelled = asyncio.Event()

    async def invoke(context):
        await create(context)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    result = await asyncio.wait_for(
        live.run_live(
            args,
            invoke,
            secret_loader=lambda _: "offline-key",
            transport_factory=lambda: httpx.MockTransport(native.handle),
        ),
        2,
    )
    assert cancelled.is_set(), result
    assert native.requests[-2:] == [
        ("POST", "/v1/sessions/sess-1/events"),
        ("POST", "/v1/sessions/sess-1/archive"),
    ]
    assert guard.report()[0].status == "failed" and guard.report()[0].held_usd == Decimal(".35")


def usage(id_, incoming=1000, output=100):
    return {
        "id": id_,
        "type": "span.model_request_end",
        "model_usage": {
            "input_tokens": incoming,
            "output_tokens": output,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
        },
    }


@pytest.mark.parametrize("package,threshold", [("A7", ".28"), ("F2-Anthropic", ".52")])
def test_stream_monitor_prices_tiers_ttl_and_cumulative_stop(
    tmp_path, monkeypatch, package, threshold
):
    args, _, config = admission(tmp_path, monkeypatch, package)
    provider = config.providers["anthropic"]
    monitor = live.UsageMonitor(
        provider.models[live.MODEL], provider.session_prices[0], Decimal(threshold)
    )
    monitor.observe(usage("first", 200000, 100))
    assert monitor.cost > Decimal(".10") and monitor.observed_thresholds == ["first"]
    with pytest.raises(live.LiveStopped, match="stop threshold"):
        monitor.observe(usage("second", 1000000, 100))
    assert len(monitor.observations) == 2


def test_missing_counter_is_unknown_and_not_below_threshold(tmp_path, monkeypatch):
    _, _, config = admission(tmp_path, monkeypatch)
    provider = config.providers["anthropic"]
    monitor = live.UsageMonitor(
        provider.models[live.MODEL], provider.session_prices[0], Decimal(".28")
    )
    event = usage("partial")
    event["model_usage"]["input_tokens"] = None
    with pytest.raises(live.LiveStopped, match="unknown"):
        monitor.observe(event)
    assert monitor.observations["partial"]["model_usage"]["input_tokens"] is None


@pytest.mark.parametrize(
    "content", [b"ANTHROPIC_API_KEY=offline-test-key\n", b"offline-test-bearer"]
)
def test_explicit_secret_never_sources_file(tmp_path, content):
    path = tmp_path / "synthetic.env"
    path.write_bytes(content)
    path.chmod(0o600)
    assert live.explicit_secret(path, env_file=content.startswith(b"ANTHROPIC")) in {
        "offline-test-key",
        "offline-test-bearer",
    }
    path.chmod(0o644)
    with pytest.raises(live.LiveRefused):
        live.explicit_secret(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        live.explicit_secret(link)


@pytest.mark.parametrize(
    "name", ["ANTHROPIC_API_KEY", "OPENAI_CUSTOM_HEADERS", "SSLKEYLOGFILE", "HTTPS_PROXY"]
)
async def test_hostile_environment_refuses_before_reserve_or_key(tmp_path, monkeypatch, name):
    args, guard, _ = admission(tmp_path, monkeypatch)
    monkeypatch.setenv(name, "unread-hostile")
    loader = Mock(side_effect=AssertionError("key must not be read"))
    with pytest.raises(ValueError):
        await live.run_live(args, Mock(), secret_loader=loader)
    assert not guard.report()
    loader.assert_not_called()


def test_changed_go_and_financial_envelope_refuse_without_effects(tmp_path):
    approval = tmp_path / "approval.json"
    approval.write_text("{}")
    go = tmp_path / "go.json"
    go.write_text('{"live_go":true}')
    with pytest.raises(live.LiveRefused):
        live.admit(
            package="A7",
            approval_path=approval,
            go_path=go,
            go_digest="0" * 64,
            config_path=tmp_path / "missing-config",
            ledger_path=tmp_path / "missing-ledger",
            binding_path=tmp_path / "missing-binding",
            output=tmp_path / "output",
            source_head="a" * 40,
            source_clean=True,
        )
    assert not (tmp_path / "output").exists()


async def test_real_f2_scripted_flow_uploads_default_skills_and_uses_ports(tmp_path, monkeypatch):
    """Same executable callback as live; only HTTP replies/credentials are synthetic."""
    import importlib.util
    from pathlib import Path

    from daimon.testing.anthropic_live_cases import default_manifest, f2_default
    from daimon.testing.ma import sse_response
    from mux.contracts.ids import ModelRef

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "f2_live_script", root / "packages/mux/tests/test_default_capability_anthropic.py"
    )
    script_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script_module)
    script_module.MODEL = ModelRef(provider="anthropic", id=live.MODEL)
    manifest = default_manifest(root, "https://mcp.owned.example/mcp")
    script = script_module.Script(manifest)
    args, guard, _ = admission(tmp_path, monkeypatch, "F2-Anthropic")
    bearer = tmp_path / "synthetic-bearer"
    bearer.write_text("offline-only-bearer")
    bearer.chmod(0o600)
    args = replace(
        args,
        binding=args.binding.model_copy(
            update={
                "mcp_url": manifest.mcp.url,
                "mcp_bearer_file": bearer,
                "environment": ref(script_module.ENVIRONMENT.id, "environment"),
            }
        ),
    )
    calls = []

    async def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "id": "vault_f2",
                    "type": "vault",
                    "display_name": "f2-offline",
                    "created_at": script_module.NOW,
                    "updated_at": script_module.NOW,
                    "metadata": {},
                },
            )
        if path == "/v1/vaults/vault_f2/credentials":
            body = json.loads(request.content)
            assert body["auth"]["token"] == "offline-only-bearer"
            return httpx.Response(
                200,
                json={
                    "id": "credential_f2",
                    "type": "vault_credential",
                    "vault_id": "vault_f2",
                    "display_name": "daimon-mcp",
                    "created_at": script_module.NOW,
                    "updated_at": script_module.NOW,
                    "metadata": {},
                    "auth": {"type": "static_bearer", "mcp_server_url": manifest.mcp.url},
                },
            )
        if (
            request.method == "POST"
            and path.endswith("/events")
            and any(e["type"] == "user.interrupt" for e in json.loads(request.content)["events"])
        ):
            return send_events_response()
        if path.endswith("/archive") or request.method == "DELETE":
            return httpx.Response(200, json={})
        if request.method == "GET" and path == "/v1/sessions/sess_f1":
            return httpx.Response(
                200, json=ma_session(id="sess_f1", model=live.MODEL).model_dump(mode="json")
            )
        if request.method == "GET" and path == "/v1/sessions/sess_f1/events":
            return httpx.Response(200, json={"data": [], "next_page": None})
        if path.endswith("/events/stream"):
            events = script_module.native_turn(manifest)
            for event in events:
                event["processed_at"] = datetime.now(UTC).isoformat()
            script.replies.popleft()
            return sse_response(events)
        return script.dispatch(request)

    result = await live.run_live(
        args,
        f2_default,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    assert result["result"]["status"] == "pass", result
    assert calls.count(("POST", "/v1/skills")) == 11
    assert calls.count(("POST", "/v1/sessions")) == 1
    assert ("POST", "/v1/sessions/sess_f1/archive") in calls
    assert ("POST", "/v1/vaults/vault_f2/archive") in calls
    assert not script.replies and not script.violations
    assert guard.report()[0].actual_usd is None
    assert (args.output / "f2-normalized.json").exists()


async def test_sse_monitor_triggers_interrupt_archive_before_another_session(tmp_path, monkeypatch):
    from daimon.testing.ma import sse_response

    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def handler(request):
        if request.url.path.endswith("/events/stream"):
            return sse_response([usage("expensive", 1000000, 1)])
        return await native.handle(request)

    async def invoke(context):
        session = await create(context)
        stream = await context.driver.events.open_stream(SCOPE, session.ref)
        async for _ in stream:
            pass
        await create(context)
        return {"status": "pass"}

    result = await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    assert result["result"]["status"] == "failed"
    assert native.created == 1
    assert native.requests[-2:] == [
        ("POST", "/v1/sessions/sess-1/events"),
        ("POST", "/v1/sessions/sess-1/archive"),
    ]
    evidence = json.loads((args.output / "receipt.json").read_text())
    assert evidence["stream_stop_reason"] == "streamed cost reached the stop threshold"
    assert guard.report()[0].held_usd > Decimal(".35")
    assert guard.report()[0].admission_blocked is True


async def test_second_session_requires_first_archive_ack_and_third_is_refused(
    tmp_path, monkeypatch
):
    args, _, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def invoke(context):
        first = await create(context)
        with pytest.raises(ProviderError):
            await create(context)
        await context.driver.session_admin.archive(SCOPE, first.ref, key="leg1-cleanup")
        second = await create(context)
        await context.driver.session_admin.archive(SCOPE, second.ref, key="leg2-cleanup")
        with pytest.raises(ProviderError):
            await create(context)
        return {"status": "pass"}

    result = await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(native.handle),
    )
    assert result["result"]["status"] == "pass", result
    assert native.created == 2


@pytest.mark.parametrize("package", ["A7", "F2-Anthropic"])
def test_missing_go_live_cli_pending_never_reads_key_or_reserves(tmp_path, monkeypatch, package):
    import argparse

    loader = Mock(side_effect=AssertionError("no key read"))
    monkeypatch.setattr(live, "explicit_secret", loader)
    args = argparse.Namespace(
        live_binding=None,
        authorization=None,
        go_receipt=None,
        go_sha256=None,
        budget_config=None,
        spend_ledger=None,
        output=tmp_path / "pending",
    )
    live.live_command(package, args)
    receipt = json.loads((args.output / "receipt.json").read_text())
    assert (
        receipt["status"] == "PENDING"
        and receipt["credential_reads"] == 0
        and receipt["reserved_usd"] == "0"
    )
    loader.assert_not_called()


async def test_actual_a7_host_pair_uses_two_fresh_sessions_and_mux_journal(
    db_preflight, db_engine, db_clean, tmp_path, monkeypatch
):
    import uuid

    from daimon.testing import anthropic_live_cases as cases
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.factories import make_tenant
    from daimon.testing.ma import sse_response
    from daimon.testing.ma_models import ma_agent
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    require_owned_sessionmaker(sm)
    tenant = uuid.uuid4()
    async with sm.begin() as db:
        await make_tenant(db, id=tenant)
    scope = Scope(
        tenant_id=str(tenant), account_id="a7", principal_id="probe", authorization_id="offline"
    )
    args, guard, _ = admission(tmp_path, monkeypatch)

    def scoped(id_, kind):
        return ResourceRef(
            id=id_,
            kind=kind,
            provider="anthropic",
            account_scope_id="test-owned",
            tenant_id=str(tenant),
            account_id="a7",
        )

    args = replace(
        args,
        binding=args.binding.model_copy(
            update={
                "scope": scope,
                "environment": scoped("env-test", "environment"),
                "agent": scoped("agent-test", "agent"),
            }
        ),
    )
    monkeypatch.setattr(cases, "create_async_engine", lambda _: db_engine)
    native = Native()

    async def handler(request):
        path = request.url.path
        if path == "/v1/agents/agent-test":
            return httpx.Response(
                200, json=ma_agent(id="agent-test", model=live.MODEL).model_dump(mode="json")
            )
        if path.endswith("/events/stream"):
            stamp = datetime.now(UTC).isoformat()
            return sse_response(
                [
                    {
                        "type": "agent.message",
                        "id": "message-" + path.split("/")[3],
                        "processed_at": stamp,
                        "content": [{"type": "text", "text": "a7-paired-probe-ok"}],
                    },
                    {
                        "type": "session.status_idle",
                        "id": "end-" + path.split("/")[3],
                        "processed_at": stamp,
                        "stop_reason": {"type": "end_turn"},
                    },
                ]
            )
        if request.method == "GET" and path.endswith("/events"):
            return httpx.Response(200, json={"data": [], "next_page": None})
        if request.method == "GET" and path.startswith("/v1/sessions/"):
            return httpx.Response(
                200,
                json=ma_session(id=path.split("/")[3], model=live.MODEL).model_dump(mode="json"),
            )
        return await native.handle(request)

    result = await live.run_live(
        args,
        cases.a7_pair,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    assert result["result"]["status"] == "pass", result
    legs = result["result"]["legs"]
    assert [leg["path"] for leg in legs] == ["legacy", "mux"]
    assert legs[0]["session_id"] != legs[1]["session_id"]
    assert legs[0]["journal_records"] == 0 < legs[1]["journal_records"]
    assert native.created == 2
    assert native.requests.index(("POST", "/v1/sessions/sess-1/archive")) < native.requests.index(
        ("POST", "/v1/sessions"), 1
    )
    assert guard.report()[0].held_usd == Decimal(".35")


@pytest.mark.parametrize("package", ["A7", "F2-Anthropic"])
@pytest.mark.parametrize(
    "corruption", [None, "go", "expiry", "source", "recipe", "config", "binding"]
)
def test_exact_signed_admission_and_stale_pins(tmp_path, package, corruption):
    """Public signed metadata snapshots only; no canonical ledger/key access."""
    from datetime import timedelta
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures/anthropic-live"
    approval_path = fixtures / (package + ".json")
    approval = json.loads(approval_path.read_text())
    config = tmp_path / "config.json"
    config.write_bytes((fixtures / "host-config.json").read_bytes())
    binding = tmp_path / "binding.json"
    binding.write_text(
        live.LiveBinding(
            scope=SCOPE,
            account_scope_id="test-owned",
            environment=ref("env-test", "environment"),
            agent=ref("agent-test", "agent"),
            database_url="postgresql+asyncpg://fixture:fixture@localhost/daimon_test_nc_offline",
            mcp_url="https://mcp.owned.example/mcp",
            mcp_bearer_file=tmp_path / "never-read-bearer",
        ).model_dump_json()
    )
    recipe = tmp_path / "reviewed-recipe.json"
    recipe.write_text('{"model":"claude-haiku-5-5"}')
    install = tmp_path / "install.json"
    install.write_text('{"installed":true}')
    go = {
        "live_go": True,
        "package": package,
        "model": live.MODEL,
        "approval_digest": live.digest_json(approval),
        "merged_source_head": "0bfa591a7ee680f4e5e97d9aad72f204c48edbde",
        "binding_sha256": hashlib.sha256(binding.read_bytes()).hexdigest(),
        "config_sha256": live.HOST_CONFIG_SHA256,
        "ledger_id": live.LEDGER_ID,
        "install_receipt_path": str(install),
        "install_receipt_sha256": hashlib.sha256(install.read_bytes()).hexdigest(),
        "recipe_path": str(recipe),
        "recipe_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
    }
    go_path = tmp_path / "go.json"
    go_path.write_text(json.dumps(go))
    now = datetime(2026, 10, 11, 13, tzinfo=UTC)
    clean = True
    if corruption == "go":
        go_path.write_text("{}")
    elif corruption == "expiry":
        now += timedelta(days=1)
    elif corruption == "source":
        clean = False
    elif corruption == "recipe":
        recipe.write_text("{}")
    elif corruption == "config":
        config.write_text("{}")
    elif corruption == "binding":
        binding.write_text("{}")

    def invoke():
        return live.admit(
            package=package,
            approval_path=approval_path,
            go_path=go_path,
            go_digest=live.digest_json(go),
            config_path=config,
            ledger_path=Path(approval["ledger_path"]),
            binding_path=binding,
            output=tmp_path / "not-created",
            source_head=go["merged_source_head"],
            source_clean=clean,
            now=now,
        )

    if corruption:
        with pytest.raises(live.LiveRefused):
            invoke()
    else:
        admitted = invoke()
        assert admitted.hold == Decimal(".35" if package == "A7" else ".65")
        assert admitted.plan.container_allowance.sessions == (2 if package == "A7" else 1)
        assert admitted.deadline_s == (300 if package == "A7" else 600)
    assert not (tmp_path / "not-created").exists() and not (tmp_path / "never-read-bearer").exists()


async def test_export_credential_refusal_preserves_settlement_and_no_secret(tmp_path, monkeypatch):
    args, guard, _ = admission(tmp_path, monkeypatch)

    async def invoke(context):
        return {"status": "pass", "leaked": "offline-guarded-key"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-guarded-key",
        transport_factory=lambda: httpx.MockTransport(Native().handle),
    )
    receipt = (args.output / "receipt.json").read_text()
    assert "offline-guarded-key" not in receipt
    assert json.loads(receipt)["guard_settlement_persisted"] is True
    assert guard.report()[0].held_usd == Decimal(".35")


async def test_f2_lost_credential_ack_retires_vault_without_retry_or_session(tmp_path, monkeypatch):
    from daimon.testing.anthropic_live_cases import f2_default

    args, guard, _ = admission(tmp_path, monkeypatch, "F2-Anthropic")
    bearer = tmp_path / "synthetic-bearer"
    bearer.write_text("offline-only-bearer")
    bearer.chmod(0o600)
    args = replace(
        args,
        binding=args.binding.model_copy(
            update={
                "mcp_url": "https://mcp.owned.example/mcp",
                "mcp_bearer_file": bearer,
            }
        ),
    )
    calls = []

    async def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "id": "vault-lost",
                    "type": "vault",
                    "display_name": "offline",
                    "created_at": "2026-10-11T12:00:00Z",
                    "updated_at": "2026-10-11T12:00:00Z",
                    "metadata": {},
                },
            )
        if path == "/v1/vaults/vault-lost/credentials":
            raise httpx.ReadError("credential acknowledgement lost", request=request)
        if path == "/v1/vaults/vault-lost/archive":
            return httpx.Response(200, json={})
        raise AssertionError("unexpected provider request after credential failure")

    result = await live.run_live(
        args,
        f2_default,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    assert result["result"]["status"] == "failed"
    assert calls == [
        ("POST", "/v1/vaults"),
        ("POST", "/v1/vaults/vault-lost/credentials"),
        ("POST", "/v1/vaults/vault-lost/archive"),
    ]
    assert guard.report()[0].held_usd == Decimal(".65")
    assert len(receipts(guard)) == 2


async def test_caught_stream_stop_cannot_start_a_second_leg_after_archive(tmp_path, monkeypatch):
    from daimon.testing.ma import sse_response

    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def handler(request):
        if request.url.path.endswith("/events/stream"):
            return sse_response([usage("expensive", 1000000, 1)])
        return await native.handle(request)

    async def invoke(context):
        session = await create(context)
        stream = await context.driver.events.open_stream(SCOPE, session.ref)
        with pytest.raises(live.LiveStopped):
            async for _ in stream:
                pass
        await context.driver.session_admin.archive(SCOPE, session.ref, key="cleanup")
        with pytest.raises(ProviderError):
            await create(context)
        return {"status": "fail"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    assert native.created == 1
    assert guard.report()[0].held_usd > Decimal(".35")
    assert guard.report()[0].admission_blocked is True


async def test_monitor_signal_cleans_up_a_catching_callback_and_preserves_overrun(
    tmp_path, monkeypatch
):
    from daimon.testing.anthropic_live_cases import collect_usage
    from daimon.testing.ma import sse_response

    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def handler(request):
        if request.url.path.endswith("/events/stream"):
            return sse_response([usage("proof-overrun", 1000000, 1)])
        if request.method == "GET":
            raise httpx.ReadError("terminal walk failed", request=request)
        return await native.handle(request)

    async def invoke(context):
        session = await create(context)
        stream = await context.driver.events.open_stream(SCOPE, session.ref)
        try:
            async for _ in stream:
                pass
        except live.LiveStopped:
            with suppress(ProviderError):
                await collect_usage(context, session.ref)
            await asyncio.Event().wait()
        return {"status": "pass"}

    result = await asyncio.wait_for(
        live.run_live(
            args,
            invoke,
            secret_loader=lambda _: "offline-key",
            transport_factory=lambda: httpx.MockTransport(handler),
        ),
        timeout=5,
    )
    row = guard.report()[0]
    assert result["result"]["status"] == "failed"
    assert row.actual_usd is None and row.held_usd > Decimal(".35")
    assert row.admission_blocked is True
    assert len(receipts(guard)) == 2
    assert native.requests[-2:] == [
        ("POST", "/v1/sessions/sess-1/events"),
        ("POST", "/v1/sessions/sess-1/archive"),
    ]


async def test_cancellation_resistant_callback_cannot_grow_inventory_or_mutate_after_settlement(
    tmp_path, monkeypatch
):
    args, guard, _ = admission(tmp_path, monkeypatch)
    args = replace(args, deadline_s=1)
    native = Native()
    release = asyncio.Event()
    escaped = []
    finished = asyncio.Event()

    async def invoke(context):
        await create(context)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            escaped.append(context)
            with pytest.raises(ProviderError):
                await create(context)
            await release.wait()
            with pytest.raises(ProviderError):
                await create(context)
            finished.set()
        return {"status": "fail"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(native.handle),
    )
    assert escaped and escaped[0].transport.closed
    assert native.created == 1 and len(receipts(guard)) == 2
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=2)
    assert native.created == 1 and len(receipts(guard)) == 2


async def test_stream_and_final_walk_merge_each_native_request_once(tmp_path, monkeypatch):
    from daimon.testing.anthropic_live import merged_usage
    from mux.conformance.usage_settlement import AnthropicSessionSpend
    from mux.drivers.anthropic.usage import observation_from_event

    args, _, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def invoke(context):
        session = await create(context)
        event = usage("same-proof", 100, 10)
        context.transport.monitor.observe(event, session.ref.id)
        context.evidence.append(
            AnthropicSessionSpend(
                session.ref,
                (
                    observation_from_event(
                        event, session.ref, observed_at=datetime.now(UTC), model_id=live.MODEL
                    ),
                ),
                None,
            )
        )
        evidence = merged_usage(context)
        assert len(evidence) == 1 and len(evidence[0].observations) == 1
        assert evidence[0].usage_complete is False and evidence[0].runtime is None
        return {"status": "pass"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(native.handle),
    )
