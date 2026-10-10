from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import JsonValue

from qa.live.config import Config, Pricing, Target
from qa.live.cost import Ledger
from qa.live.discord import DiscordBackend, fingerprint, load_driver
from qa.live.schema import Assertion, Step
from qa.live.types import Message, Pending, Turn, utcnow


class FakeDriver:
    TERMINAL = frozenset({"answered", "error", "over_cap"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.current: list[Message] = []
        self.baseline: list[Message] = []
        self.role = "user"
        self.permissions = ~0
        self.category_guild = "1435062989119295640"
        self.trigger: Message = {"id": "trigger", "reactions": []}
        self.thread_metadata: Message = {}
        self.qa_id = "1533049261032341668"

    def set_role(self, role: str) -> None:
        self.role = role

    def call(self, method: str, path: str, *, body: Message | None = None) -> JsonValue:
        self.calls.append((method, path))
        if path == "/users/@me":
            return {"id": self.qa_id}
        if path == "/channels/1558361838960382032":
            return {"guild_id": self.category_guild, "type": 4}
        if path.startswith("/guilds/") and method == "POST":
            assert body and body["parent_id"] == "1558361838960382032"
            return {"id": "parent"}
        if path.endswith("/messages") and method == "POST":
            return {"id": "123"}
        if "/reactions/" in path and method == "GET":
            return [{"id": "1530628070405308456"}]
        if "/messages/" in path and method == "GET":
            for message in self.current:
                if path.endswith("/" + str(message.get("id"))):
                    return message
            return self.trigger
        if path == "/channels/thread":
            return self.thread_metadata
        return {}

    def messages(self, channel_id: str, *, after: str | None, limit: int = 50) -> list[Message]:
        return self.baseline

    def turn_messages(self, channel_id: str, *, after: str, daimon_id: str) -> list[Message]:
        return self.current

    def exists(self, channel: str) -> bool:
        return False

    def classify(self, message: Message) -> str:
        return "working" if message.get("working") else "answered"

    def cmd_say(self, args: argparse.Namespace) -> None:
        assert args.file
        print("123")

    def cmd_warm(self, args: argparse.Namespace) -> None:
        self.calls.append(("WARM", ""))

    def cmd_access(self, args: argparse.Namespace) -> None:
        self.calls.append(("ACCESS", args.guild))

    def cmd_selftest(self, args: argparse.Namespace) -> None:
        self.calls.append(("SELFTEST", ""))

    def effective_permissions(
        self, *, guild: Message, overwrites: list[Message], user_id: str, api_guild_id: str
    ) -> int:
        return self.permissions


@pytest.fixture
def driver() -> FakeDriver:
    return FakeDriver()


@pytest.fixture
def backend(
    pricing: Pricing, driver: FakeDriver, monkeypatch: pytest.MonkeyPatch
) -> DiscordBackend:
    config = Config(
        pricing=pricing, staging=Target(enabled=True), poll_interval_s=0.001, settle_s=0.001
    )
    monkeypatch.setenv(config.staging.database_env, "postgresql+asyncpg://offline-only")
    backend = DiscordBackend(config, "staging", driver=driver)
    monkeypatch.setattr(backend, "_wait_for_logs", lambda turn: None)
    return backend


def test_driver_bridge_reuses_functions_without_calls(tmp_path: Path) -> None:
    path = tmp_path / "qa.py"
    path.write_text("""
_role = 'user'
TERMINAL = frozenset({'answered'})
def _call(method, path, *, body=None): return {'role': _role}
def _messages(channel_id, *, after, limit=50): return []
def _exists(channel_id): return False
def _effective_permissions(**kwargs): return 123
def classify(message): return 'answered'
""")
    driver = load_driver(path)
    driver.set_role("admin")
    assert driver.call("GET", "unused") == {"role": "admin"}
    assert driver.classify({}) == "answered"


def test_preflight_permissions_identity_and_category(
    backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.preflight({"user"})
    assert driver.calls[-1] == ("WARM", "")
    driver.permissions = 0
    with pytest.raises(ValueError, match="permissions"):
        backend.preflight({"user"})
    driver.permissions = ~0
    driver.qa_id = "not-allowed"
    with pytest.raises(ValueError, match="identity"):
        backend.preflight({"user"})
    driver.category_guild = "customer"
    with pytest.raises(ValueError, match="category"):
        backend.preflight({"user"})


def test_only_owned_channel_writes_and_upload(backend: DiscordBackend, driver: FakeDriver) -> None:
    for action in [
        lambda: backend.delete_channel("customer"),
        lambda: backend.send("customer", Step(do="mention", text="x"), mention=True),
        lambda: backend.react("customer", "1", "👍"),
    ]:
        with pytest.raises(ValueError, match="outside"):
            action()
    assert not driver.calls
    channel = backend.create_channel("qa-test")
    assert (
        backend.send(channel, Step(do="mention", text="x", file="fake.csv"), mention=True) == "123"
    )
    backend.react(channel, "123", "👍")
    backend.delete_channel(channel)
    assert not backend.owned


def test_collection_excludes_previous_turn_and_settles(
    backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.create_channel("qa-test")
    old: Message = {"id": "old", "channel_id": "thread", "content": "previous"}
    driver.baseline = [old]
    backend.threads.add("thread")
    trigger = backend.send("thread", Step(do="thread_reply", text="new"), mention=False)
    fresh: Message = {"id": "new", "channel_id": "thread", "content": "APPLE"}
    driver.current = [old, fresh]
    turn = Turn(2, trigger, "thread", utcnow())
    backend.collect(turn, 0.1)
    assert turn.messages == [fresh]
    assert turn.thread_id == "thread"
    assert turn.ended_at and turn.done_s is not None
    assert turn.settled
    assert not turn.parent_messages
    assert fingerprint(old) != fingerprint(fresh)


def test_queue_reaction_is_visible_without_terminal(
    backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.create_channel("qa-test")
    driver.trigger["reactions"] = [{"emoji": {"name": "⌛"}}]
    turn = Turn(1, "123", "parent", utcnow(), settled=True)
    with pytest.raises(Pending, match="timed out"):
        backend.collect(turn, 0.01)
    assert turn.first_visible_s is not None
    assert turn.done_s is None
    assert turn.ended_at and not turn.settled
    assert turn.trigger_reactions == driver.trigger["reactions"]


def test_collection_trusts_only_webhooks_owned_by_daimon_application(
    monkeypatch: pytest.MonkeyPatch, backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.create_channel("qa-test")
    answer: Message = {
        "id": "answer",
        "channel_id": "thread",
        "content": "APPLE",
        "webhook_id": "webhook",
        "author": {"id": "webhook-author"},
        "application_id": backend.target.daimon_id,
    }
    driver.baseline = [answer]
    queried: list[str] = []

    def selected(channel_id: str, *, after: str, daimon_id: str) -> list[Message]:
        queried.append(daimon_id)
        return [answer] if daimon_id == "webhook-author" else []

    monkeypatch.setattr(driver, "turn_messages", selected)
    turn = Turn(1, "123", "parent", utcnow())
    backend.collect(turn, 0.1)
    assert turn.messages == [answer]
    assert "webhook-author" in queried
    answer["application_id"] = "unrelated-application"
    queried.clear()
    with pytest.raises(Pending):
        backend.collect(Turn(2, "456", "parent", utcnow()), 0.01)
    assert "webhook-author" not in queried


def test_reply_reference_and_mention_are_sent_to_owned_thread(
    monkeypatch: pytest.MonkeyPatch, backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.create_channel("qa-test")
    backend.threads.add("thread")
    payloads: list[Message] = []
    original = driver.call

    def capture(method: str, path: str, *, body: Message | None = None) -> JsonValue:
        if method == "POST" and path.endswith("/messages"):
            assert body
            payloads.append(body)
        return original(method, path, body=body)

    monkeypatch.setattr(driver, "call", capture)
    backend.send(
        "thread",
        Step(do="thread_reply", text="follow up"),
        mention=True,
        reply_message_id="prior-chunk",
    )
    assert payloads == [
        {
            "content": f"<@{backend.target.daimon_id}> follow up",
            "message_reference": {"message_id": "prior-chunk", "channel_id": "thread"},
        }
    ]


def test_working_card_cannot_settle(backend: DiscordBackend, driver: FakeDriver) -> None:
    backend.create_channel("qa-test")
    driver.current = [{"id": "x", "channel_id": "thread", "working": True}]
    turn = Turn(1, "123", "parent", utcnow())
    with pytest.raises(Pending):
        backend.collect(turn, 0.01)
    assert turn.done_s is None


def test_log_query_is_scoped_and_fields_filtered(
    monkeypatch: pytest.MonkeyPatch, backend: DiscordBackend
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                [
                    {
                        "jsonPayload": {
                            "event": "turn.message_missing",
                            "delivered": False,
                            "thread_id": "thread",
                        }
                    },
                    {
                        "jsonPayload": {
                            "event": "turn.message_missing",
                            "delivered": True,
                            "thread_id": "thread",
                        }
                    },
                ]
            ),
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    turn = Turn(1, "123", "parent", utcnow(), ended_at=utcnow(), thread_id="thread")
    rows = backend.logs(
        Assertion(
            kind="log_present", turn=1, event="turn.message_missing", fields={"delivered": False}
        ),
        turn,
    )
    assert len(rows) == 1
    assert "--project=pymc-daimon-staging" in calls[0]
    assert "timestamp>=" in calls[0][3] and "timestamp<=" in calls[0][3]
    assert 'jsonPayload.thread_id="thread"' in calls[0][3]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))
    with pytest.raises(Pending):
        backend.logs(Assertion(kind="log_absent", turn=1, event="a"), turn)


def test_unconfigured_hooks_and_prod_admin_are_refused(backend: DiscordBackend) -> None:
    backend.create_channel("qa-test")
    with pytest.raises(Pending, match="hook"):
        backend.admin(Step(do="admin", tool="change"), "parent")
    backend.env = "prod"
    with pytest.raises(ValueError, match="production"):
        backend.admin(Step(do="restart_workers"), "parent")


def test_usage_footer_and_unavailable_db(
    backend: DiscordBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DAIMON_QA_STAGING_DATABASE_URL", raising=False)
    turn = Turn(
        1,
        "123",
        "parent",
        utcnow(),
        messages=[
            {"embeds": [{"footer": {"text": "daimon · 42s · 1.2k in / 340 out · $0.0123"}}]},
        ],
    )
    usage = backend.usage(turn)
    assert usage.usd == 0.0123 and usage.input_tokens == 1200 and usage.output_tokens == 340
    with pytest.raises(Pending, match="database unavailable"):
        backend.db_check("select 1")


def test_request_correlated_log_absence_is_required(
    monkeypatch: pytest.MonkeyPatch, backend: DiscordBackend
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        records = (
            [{"jsonPayload": {"event": "turn.completed", "rid": "qa-rid", "thread_id": "thread"}}]
            if len(calls) % 2
            else []
        )
        return subprocess.CompletedProcess(command, 0, json.dumps(records), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    turn = Turn(1, "123", "parent", utcnow(), ended_at=utcnow(), thread_id="thread")
    assertion = Assertion(kind="log_absent", turn=1, event="session_preparation.replaced")
    assert backend.logs(assertion, turn) == []
    assert 'jsonPayload.rid="qa-rid"' in calls[1][3]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "[]"))
    with pytest.raises(Pending, match="correlated"):
        backend.logs(assertion, turn)


def test_readonly_queries_bind_context_and_extract_scalar(
    backend: DiscordBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, object] | None]] = []

    async def query(sql: str, params: dict[str, object] | None = None) -> JsonValue:
        calls.append((sql, params))
        return [{"id": "qa-tenant"}] if "FROM tenants" in sql else [{"count": 0}]

    monkeypatch.setattr(backend, "_query", query)
    backend.parent = "parent"
    turn = Turn(1, "trigger", "parent", utcnow(), thread_id="thread")
    result = backend.db_check("SELECT count(*) FROM x WHERE tenant_id = :tenant_id", turn)
    assert result == 0
    assert calls[-1][1] == {
        "tenant_id": "qa-tenant",
        "guild_id": "1435062989119295640",
        "channel_id": "parent",
        "thread_id": "thread",
        "trigger_message_id": "trigger",
    }


def test_measured_usage_records_real_token_and_model_evidence(
    backend: DiscordBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    from qa.live.models import STAGING_LEGACY_MODEL

    async def query(sql: str, params: dict[str, object] | None = None) -> JsonValue:
        assert params and params["thread"] == "thread"
        return [
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 0,
                "cost_usd": "0.00023",
                "model_ids": [STAGING_LEGACY_MODEL],
            }
        ]

    monkeypatch.setattr(backend, "_query", query)
    turn = Turn(1, "123", "parent", utcnow(), ended_at=utcnow(), thread_id="thread")
    usage = backend.usage(turn)
    assert usage.input_tokens == 100 and usage.cache_read_input_tokens == 300
    assert usage.usd == 0.00023 and usage.models == [STAGING_LEGACY_MODEL]


def test_null_cost_preserves_measured_model_and_tokens(
    backend: DiscordBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def query(sql: str, params: dict[str, object] | None = None) -> JsonValue:
        assert params and params["thread"] == "thread"
        return [
            {
                "model_ids": ["claude-haiku-4-5-20251001"],
                "cost_usd": None,
                "input_tokens": 28,
                "output_tokens": 510,
                "cache_read_input_tokens": 51282,
                "cache_creation_input_tokens": 18445,
            }
        ]

    monkeypatch.setattr(backend, "_query", query)
    turn = Turn(1, "trigger", "parent", utcnow(), thread_id="thread", ended_at=utcnow())
    usage = backend.usage(turn)
    assert usage.models == ["claude-haiku-4-5-20251001"]
    assert usage.source == "turn_outcomes" and usage.usd is None
    assert usage.input_tokens == 28 and usage.output_tokens == 510
    assert usage.cache_read_input_tokens == 51282
    assert usage.cache_creation_input_tokens == 18445


def test_progress_reaction_survives_cleanup_in_history(
    backend: DiscordBackend, driver: FakeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.owned.add("parent")
    driver.trigger["reactions"] = [{"emoji": {"name": "👀"}}]
    driver.current = [{"id": "answer", "channel_id": "thread", "content": "DONE"}]
    original = driver.turn_messages

    def messages(channel_id: str, *, after: str, daimon_id: str) -> list[Message]:
        # Simulate a fast answer clearing its marker during the slower reads.
        driver.trigger["reactions"] = []
        return original(channel_id, after=after, daimon_id=daimon_id)

    monkeypatch.setattr(driver, "turn_messages", messages)
    turn = Turn(1, "trigger", "parent", utcnow())
    backend.collect(turn, 1)
    assert turn.trigger_reactions == []
    assert turn.trigger_reaction_history[0]["reactions"] == [{"emoji": {"name": "👀"}}]
    assert turn.trigger_reaction_history[-1]["reactions"] == []
    assert float(str(turn.trigger_reaction_history[0]["elapsed_s"])) < (turn.done_s or 0)


def test_unpriced_model_preserves_larger_rounded_footer_cost(
    backend: DiscordBackend, monkeypatch: pytest.MonkeyPatch, ledger: Ledger
) -> None:
    async def query(sql: str, params: dict[str, object] | None = None) -> JsonValue:
        return [{"model_ids": ["claude-haiku-4-5-20251001"], "cost_usd": None}]

    monkeypatch.setattr(backend, "_query", query)
    turn = Turn(1, "trigger", "parent", utcnow(), thread_id="thread")
    turn.messages = [{"embeds": [{"footer": {"text": "Haiku  12s  $0.500 used  $8.00 left"}}]}]
    usage = backend.usage(turn)
    assert usage.models == ["claude-haiku-4-5-20251001"]
    assert usage.usd == 0.5 and usage.source == "turn_outcomes+footer_rounded"
    ledger.reserve("rounded", 0.1)
    ledger.receipt("rounded", [usage], 0.1)
    receipt = json.loads(ledger.path.read_text())
    assert receipt["usd"] == 0.5
    assert receipt["accounting"] == "conservative_estimate" and receipt["actual_usd"] is None


def test_thread_name_is_refetched_from_the_owned_thread(
    backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.owned.add("parent")
    backend.threads.add("thread")
    backend.thread_parents["thread"] = "parent"
    turn = Turn(1, "trigger", "parent", utcnow(), thread_id="thread")
    driver.thread_metadata = {
        "id": "thread",
        "guild_id": backend.target.guild_id,
        "parent_id": "parent",
        "type": 11,
        "name": "Inventory example",
    }
    assert backend.thread_name(turn) == "Inventory example"
    driver.thread_metadata["name"] = "Renamed by the agent"
    assert backend.thread_name(turn) == "Renamed by the agent"
    assert driver.calls[-1] == ("GET", "/channels/thread")


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "another-thread"),
        ("guild_id", "customer-guild"),
        ("parent_id", "unowned-channel"),
        ("type", 0),
        ("name", None),
    ],
)
def test_thread_name_refuses_unverified_metadata(
    backend: DiscordBackend, driver: FakeDriver, field: str, value: JsonValue
) -> None:
    backend.owned.add("parent")
    backend.threads.add("thread")
    backend.thread_parents["thread"] = "parent"
    driver.thread_metadata = {
        "id": "thread",
        "guild_id": backend.target.guild_id,
        "parent_id": "parent",
        "type": 11,
        "name": "Inventory example",
    }
    driver.thread_metadata[field] = value
    with pytest.raises(Pending, match="readback"):
        backend.thread_name(Turn(1, "trigger", "parent", utcnow(), thread_id="thread"))


@pytest.mark.parametrize("mismatch", [None, "guild", "channel", "time", "event", "missing"])
def test_model_skip_requires_positive_scoped_pre_admission_log(
    monkeypatch: pytest.MonkeyPatch, backend: DiscordBackend, mismatch: str | None
) -> None:
    from datetime import timedelta

    start = utcnow()
    turn = Turn(
        1,
        "123",
        "parent",
        start,
        ended_at=start + timedelta(seconds=20),
        guild_id=backend.target.guild_id,
    )
    payload: Message = {
        "event": "turn.skipped.writers_none",
        "guild_id": turn.guild_id,
        "channel_id": "parent",
        "timestamp": (start + timedelta(seconds=1)).isoformat(),
    }
    if mismatch == "guild":
        payload["guild_id"] = "foreign"
    if mismatch == "channel":
        payload["channel_id"] = "foreign"
    if mismatch == "event":
        payload["event"] = "turn.started"
    if mismatch == "time":
        payload["timestamp"] = (start - timedelta(seconds=1)).isoformat()
    queries: list[str] = []

    def logs(query: str) -> list[Message]:
        queries.append(query)
        return [] if mismatch == "missing" else [{"jsonPayload": payload}]

    monkeypatch.setattr(backend, "_read_logs", logs)
    monkeypatch.setattr(backend, "_wait_for_logs", lambda turn: None)
    usage = backend._skipped_usage(turn)
    assert len(queries) == 1
    assert all(
        part in queries[0]
        for part in [
            "timestamp>=",
            "timestamp<=",
            "guild_id",
            "channel_id",
            "turn.skipped.writers_none",
        ]
    )
    if mismatch is None:
        assert usage and usage.usd == 0 and usage.models == []
        assert usage.skipped_reason == "turn.skipped.writers_none"
        assert usage.skip_evidence["channel_id"] == "parent"
    else:
        assert usage is None


@pytest.mark.parametrize(
    "exit_code,output,allow,process_code,expected",
    [
        (1, "ConnectionError: database transport unavailable", True, 0, RuntimeError),
        (1, "skill is still attached to qa-test", True, 0, None),
        (2, "skill is still attached to qa-test", True, 0, RuntimeError),
        (1, "skill is still attached to qa-test", False, 0, RuntimeError),
        (0, "ok", False, 1, RuntimeError),
        (1, "skill is still attached to qa-test", True, 1, RuntimeError),
        (True, "skill is still attached to qa-test", True, 0, ValueError),
        (-1, "skill is still attached to qa-test", True, 0, ValueError),
    ],
)
def test_admin_expected_refusal_never_hides_transport_or_unexpected_exit(
    backend: DiscordBackend,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: object,
    output: str,
    allow: bool,
    process_code: int,
    expected: type[Exception] | None,
) -> None:
    backend.owned.add("parent")
    backend.config.admin_hooks["cli"] = ["fixture"]

    def command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, process_code, json.dumps({"exit_code": exit_code, "stdout": output}), ""
        )

    monkeypatch.setattr(subprocess, "run", command)
    step = Step(
        do="admin",
        tool="cli",
        args="daimon skills delete qa-test",
        allow_fail=allow,
        allow_fail_pattern="(?i)skill is still attached|still attached to" if allow else None,
    )
    if expected:
        with pytest.raises(expected):
            backend.admin(step, "parent")
    else:
        backend.admin(step, "parent")
        assert "last_admin_exit_code" not in backend.bindings


def test_refresh_and_empty_thread_inventory_require_owned_matching_evidence(
    backend: DiscordBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn = Turn(1, "111", "parent", utcnow(), settled=True)
    turn.messages = [{"id": "message", "channel_id": "foreign"}]
    with pytest.raises(ValueError, match="outside"):
        backend.current_messages(turn)
    backend.owned.add("parent")
    turn.messages[0]["channel_id"] = "parent"
    monkeypatch.setattr(backend.driver, "call", lambda method, path, **kwargs: {"id": "different"})
    with pytest.raises(Pending, match="matching"):
        backend.current_messages(turn)
    turn.messages = []
    other = Turn(2, "222", "parent", utcnow(), settled=True)
    monkeypatch.setattr(
        backend.driver,
        "call",
        lambda method, path, **kwargs: {
            "id": path.rsplit("/", 1)[1],
            "type": 11,
            "parent_id": "parent",
        },
    )
    assert backend.created_threads([turn, other]) == {"111", "222"}
    from qa.live.evaluate import evaluate
    from qa.live.tests.conftest import FakeJudge

    assert (
        evaluate(
            Assertion(kind="threads_created", maximum=1), [turn, other], backend, FakeJudge()
        ).status
        == "FAIL"
    )

    def denied(method: str, path: str, **kwargs: object) -> JsonValue:
        raise SystemExit("HTTP 403 on GET " + path + ": denied")

    monkeypatch.setattr(backend.driver, "call", denied)
    with pytest.raises(Pending, match="unavailable"):
        backend.created_threads([turn])

    def missing(method: str, path: str, **kwargs: object) -> JsonValue:
        raise SystemExit("HTTP 404 on GET " + path + ": absent")

    monkeypatch.setattr(backend.driver, "call", missing)
    assert backend.created_threads([turn]) == set()


def test_collect_records_distinct_running_edits_and_baseline_ids(
    backend: DiscordBackend, driver: FakeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    backend.owned.add("parent")
    started = utcnow()
    backend.baseline_ids["123"] = {"old-card"}
    working: Message = {
        "id": "card",
        "channel_id": "thread",
        "working": True,
        "edited_timestamp": started.isoformat(),
    }
    second: Message = {**working, "edited_timestamp": (started + timedelta(seconds=1)).isoformat()}
    terminal: Message = {
        **working,
        "working": False,
        "edited_timestamp": (started + timedelta(seconds=2)).isoformat(),
    }
    rows = iter([[working], [working], [second], [terminal]])

    def listed(*args: object, **kwargs: object) -> list[Message]:
        driver.current = next(rows, [terminal])
        return driver.current

    monkeypatch.setattr(driver, "turn_messages", listed)
    turn = Turn(1, "123", "parent", started)
    backend.collect(turn, 0.1)
    assert turn.settled and turn.baseline_message_ids == ["old-card"]
    assert len(turn.card_history) == 3
    assert {e["edited_timestamp"] for e in turn.card_history} == {
        working["edited_timestamp"],
        second["edited_timestamp"],
    }
    assert all(e["phase"] == "running" for e in turn.card_history)
    assert all(e["observed_at"] and e["message"] for e in turn.card_history)


def test_running_card_samples_refresh_footer_and_keep_unchanged_samples(
    backend: DiscordBackend, driver: FakeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.owned.add("parent")
    backend.threads.add("thread")
    backend.thread_parents["thread"] = "parent"
    turn = Turn(1, "123", "parent", utcnow(), thread_id="thread")
    stale: Message = {"id": "card", "channel_id": "thread", "working": True}
    refreshed: Message = {
        **stale,
        "embeds": [{"footer": {"text": "Working · 10s"}}],
    }
    monkeypatch.setattr(driver, "call", lambda *args, **kwargs: refreshed)
    backend.sample_running_card(turn, stale)
    backend.sample_running_card(turn, stale)
    refreshed["embeds"] = [{"footer": {"text": "Working · 20s"}}]
    backend.sample_running_card(turn, stale)
    assert len(turn.card_history) == 3
    assert all(e["edited_timestamp"] is None for e in turn.card_history)
    assert turn.card_history[0]["message"] == {
        **stale,
        "embeds": [{"footer": {"text": "Working · 10s"}}],
    }
    assert turn.card_history[2]["message"] == refreshed


def test_transient_card_readback_failure_is_unavailable_evidence_and_collection_settles(
    backend: DiscordBackend, driver: FakeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend.owned.add("parent")
    working: Message = {"id": "card", "channel_id": "parent", "working": True}
    terminal: Message = {**working, "working": False, "content": "DONE"}
    rows = iter([[working], [working], [terminal]])
    failures = [1]
    real_call = driver.call

    def listed(*args: object, **kwargs: object) -> list[Message]:
        driver.current = next(rows, [terminal])
        return driver.current

    def call(method: str, path: str, **kwargs: object) -> JsonValue:
        if path == "/channels/parent/messages/card" and failures:
            failures.pop()
            raise SystemExit("HTTP 503 on GET " + path + ": unavailable")
        return real_call(method, path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(driver, "turn_messages", listed)
    monkeypatch.setattr(driver, "call", call)
    turn = Turn(1, "123", "parent", utcnow())
    backend.collect(turn, 0.1)
    assert turn.settled
    assert [("unavailable" in e, "message" in e) for e in turn.card_history] == [
        (True, False),
        (False, True),
    ]
    assert turn.card_history[0]["unavailable"] == "running card readback unavailable: SystemExit"
    assert turn.card_history[0]["observed_at"] and turn.card_history[0]["poll_interval_s"]


@pytest.mark.parametrize(
    "readback",
    [
        {"id": "other", "channel_id": "parent", "working": True},
        {"id": "card", "channel_id": "elsewhere", "working": True},
    ],
)
def test_mismatched_card_readback_is_never_accepted_as_a_sample(
    backend: DiscordBackend,
    driver: FakeDriver,
    monkeypatch: pytest.MonkeyPatch,
    readback: Message,
) -> None:
    backend.owned.add("parent")
    monkeypatch.setattr(driver, "call", lambda *args, **kwargs: readback)
    turn = Turn(1, "123", "parent", utcnow())
    backend.sample_running_card(turn, {"id": "card", "channel_id": "parent", "working": True})
    assert len(turn.card_history) == 1 and "message" not in turn.card_history[0]
    assert "did not match" in str(turn.card_history[0]["unavailable"])


def test_terminal_readback_race_is_excluded_and_unowned_card_refused(
    backend: DiscordBackend, driver: FakeDriver
) -> None:
    backend.owned.add("parent")
    driver.current = [{"id": "card", "channel_id": "parent", "content": "SLEPT"}]
    turn = Turn(1, "123", "parent", utcnow())
    backend.sample_running_card(turn, {"id": "card", "channel_id": "parent", "working": True})
    # The race is kept as the card's observed end, never as a payload or edit.
    assert len(turn.card_history) == 1
    race = turn.card_history[0]
    assert race["ended"] is True and "message" not in race and race["listed_at"]
    calls = len(driver.calls)
    with pytest.raises(ValueError, match="outside a channel created by this run"):
        backend.sample_running_card(turn, {"id": "card", "channel_id": "foreign", "working": True})
    assert len(driver.calls) == calls and len(turn.card_history) == 1


@pytest.mark.parametrize(
    "failure",
    [
        "psycopg.OperationalError: connection failed: Connection refused",
        "httpx.ConnectError: [Errno 111] Connection refused",
        "ssh: connect to host 10.0.0.2 port 22: Connection refused",
        "connection reset",
        "ECONNRESET",
        "timed out",
        "TimeoutError",
        "DNS lookup failed",
        "temporary failure in name resolution",
        "TLS handshake failed",
        "SSL error",
        "HTTP 503",
        "5xx error",
        "Traceback (most recent call last)",
        "unhandled exception",
    ],
)
def test_transport_denylist_precedes_even_matching_refusal_pattern(
    backend: DiscordBackend, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    backend.owned.add("parent")
    backend.config.admin_hooks["cli"] = ["fixture"]

    def command(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 0, json.dumps({"exit_code": 1, "stderr": failure + "\nrefused"}), ""
        )

    monkeypatch.setattr(subprocess, "run", command)
    step = Step(
        do="admin",
        tool="cli",
        args="daimon skills delete qa-test",
        allow_fail=True,
        allow_fail_pattern="(?i)still attached|refus",
    )
    with pytest.raises(RuntimeError, match="transport failure"):
        backend.admin(step, "parent")


@pytest.mark.parametrize(
    "chunk2_s, answer_edit_s, expected",
    [(17.237, 17, "PASS"), (23, 17, "FAIL"), (17.237, None, "PENDING")],
)
def test_unedited_working_card_converted_into_chunk_one_is_timed_from_its_edit(
    backend: DiscordBackend,
    driver: FakeDriver,
    monkeypatch: pytest.MonkeyPatch,
    chunk2_s: float,
    answer_edit_s: float | None,
    expected: str,
) -> None:
    """Collector-to-evaluator replay of the saved NEW10 shape (REVIEW-739-0f6d204)."""
    from datetime import timedelta

    from qa.live.evaluate import evaluate
    from qa.live.schema import Assertion
    from qa.live.tests.conftest import FakeJudge

    backend.owned.add("parent")
    started = utcnow()

    def at(seconds: float) -> str:
        return (started + timedelta(seconds=seconds)).isoformat()

    # Both running snapshots have no edit timestamp, exactly like NEW10.
    working: Message = {
        "id": "card",
        "channel_id": "parent",
        "working": True,
        "content": "Working",
        "timestamp": at(1),
        "edited_timestamp": None,
    }
    chunk1: Message = {**working, "working": False, "content": "chunk one"}
    if answer_edit_s is not None:
        chunk1["edited_timestamp"] = at(answer_edit_s)
    chunk2: Message = {
        "id": "chunk2",
        "channel_id": "parent",
        "content": "chunk two",
        "timestamp": at(chunk2_s),
    }
    rows = iter([[working], [working], [chunk1, chunk2]])

    def listed(*args: object, **kwargs: object) -> list[Message]:
        driver.current = next(rows, [chunk1, chunk2])
        return driver.current

    monkeypatch.setattr(driver, "turn_messages", listed)
    turn = Turn(1, "123", "parent", started)
    backend.collect(turn, 1)
    assert turn.settled
    # Both unedited working polls are sampled (per-poll sampling, #730).
    assert len(turn.card_history) == 2
    assert all(
        e["message_id"] == "card" and e["phase"] == "running" and e["edited_timestamp"] is None
        for e in turn.card_history
    )
    gap = Assertion(kind="chunks_gap_max_s", turn=1, maximum=5)
    result = evaluate(gap, [turn], backend, FakeJudge())
    assert result.status == expected, result.reason
    if expected == "PASS":
        assert "0.237" in result.reason
    # An unedited running card is a working observation, not an edit.
    edits = Assertion(kind="card_edits_min", turn=1, minimum=1, during="running")
    counted = evaluate(edits, [turn], backend, FakeJudge())
    assert counted.status != "PASS"


def latency_backend(driver: FakeDriver) -> DiscordBackend:
    backend = DiscordBackend(
        Config(
            pricing=Pricing(
                per_turn_usd=0.25, judge_input_per_million=1, judge_output_per_million=5
            ),
            staging=Target(enabled=True),
            poll_interval_s=4,
        ),
        "staging",
        driver=driver,
    )
    backend.owned.add("parent")
    return backend


@pytest.mark.parametrize("first_fails", [False, True])
def test_slow_first_read_keeps_the_known_running_start(
    driver: FakeDriver, monkeypatch: pytest.MonkeyPatch, first_fails: bool
) -> None:
    """REVIEW-730-03a6bb7 P1: listed working at +0, direct read blocked until +30."""
    from datetime import timedelta

    import qa.live.discord as discord
    from qa.live.evaluate import evaluate
    from qa.live.tests.conftest import FakeBackend, FakeJudge

    started = utcnow()
    clock = [0.0]
    backend = latency_backend(driver)
    turn = Turn(1, "trigger", "parent", started, settled=True, done_s=50)
    working: Message = {"id": "card", "channel_id": "parent", "working": True}
    first = [True]

    def read(*args: object, **kwargs: object) -> JsonValue:
        if first:
            first.pop()
            clock[0] = 30.0
            if first_fails:
                raise SystemExit("offline simulated timeout")
        footer = f"Working · {int(clock[0] // 10) * 10}s"
        return {**working, "embeds": [{"footer": {"text": footer}}]}

    monkeypatch.setattr(driver, "call", read)
    monkeypatch.setattr(discord, "utcnow", lambda: started + timedelta(seconds=clock[0]))
    backend.sample_running_card(turn, working)
    for seconds in (34, 38, 42, 46):
        clock[0] = seconds
        backend.sample_running_card(turn, working)
    first_entry = turn.card_history[0]
    assert first_entry["listed_at"] == started.isoformat()
    assert first_entry["observed_at"] == (started + timedelta(seconds=30)).isoformat()
    turn.messages = [{"id": "card", "content": "SLEPT"}]
    check = evaluate(
        Assertion(kind="card_edits_min", turn=1, minimum=3, during="running"),
        [turn],
        FakeBackend(),
        FakeJudge(),
    )
    assert check.status == "PENDING" and "capture gap" in check.reason, check.reason


def test_later_read_in_same_poll_does_not_end_another_card(
    driver: FakeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REVIEW-730-03a6bb7 P1: B's slow same-poll read must not end A's window."""
    from datetime import timedelta

    import qa.live.discord as discord
    from qa.live.evaluate import evaluate
    from qa.live.tests.conftest import FakeBackend, FakeJudge

    started = utcnow()
    clock = [0.0]
    backend = latency_backend(driver)
    turn = Turn(1, "trigger", "parent", started, settled=True, done_s=30)
    cards: dict[str, Message] = {
        i: {"id": i, "channel_id": "parent", "working": True} for i in ("a", "b")
    }

    def read(method: str, path: str, **kwargs: object) -> JsonValue:
        identity = path.rsplit("/", 1)[1]
        if identity == "a" and clock[0] == 26:
            return {**cards[identity], "working": False, "content": "DONE"}
        if identity == "b":
            clock[0] += 6 if clock[0] == 12 else (0 if clock[0] == 26 else 1)
            state = "initial" if clock[0] < 18 else "changed"
        else:
            state = f"Working · {int(clock[0] // 10) * 10}s"
        return {**cards[identity], "embeds": [{"footer": {"text": state}}]}

    monkeypatch.setattr(driver, "call", read)
    monkeypatch.setattr(discord, "utcnow", lambda: started + timedelta(seconds=clock[0]))
    for poll in (0, 4, 8, 12, 26):
        clock[0] = poll
        listed = started + timedelta(seconds=poll)
        backend.sample_running_card(turn, cards["a"], listed_at=listed)
        backend.sample_running_card(turn, cards["b"], listed_at=listed)
    race = [e for e in turn.card_history if e.get("ended")]
    assert [e["message_id"] for e in race] == ["a"]
    turn.messages = [{"id": "a", "content": "DONE"}, {"id": "b", "content": "DONE"}]
    check = evaluate(
        Assertion(kind="card_edits_min", turn=1, minimum=3, during="running"),
        [turn],
        FakeBackend(),
        FakeJudge(),
    )
    assert check.status == "PENDING" and "card a capture gap 14.0s" in check.reason, check.reason


def test_collect_gives_every_card_in_a_poll_the_same_listing_time(
    backend: DiscordBackend, driver: FakeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    backend.owned.add("parent")
    cards: list[Message] = [{"id": i, "channel_id": "parent", "working": True} for i in ("a", "b")]
    done: list[Message] = [{**c, "working": False} for c in cards]
    rows = iter([cards])

    def listed(*args: object, **kwargs: object) -> list[Message]:
        driver.current = next(rows, done)
        return driver.current

    real_call = driver.call

    def slow(method: str, path: str, **kwargs: object) -> JsonValue:
        if path.endswith("/messages/a"):
            time.sleep(0.05)  # a slow direct read inside the poll
        return real_call(method, path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(driver, "turn_messages", listed)
    monkeypatch.setattr(driver, "call", slow)
    turn = Turn(1, "123", "parent", utcnow())
    backend.collect(turn, 1)
    a, b = turn.card_history
    assert a["listed_at"] == b["listed_at"]
    assert a["listed_at"] <= a["observed_at"] < b["observed_at"]

    assert evaluate(edits, [turn], backend, FakeJudge()).status == "FAIL"


def test_progress_text_snapshot_survives_final_card_edit(
    backend: DiscordBackend,
    driver: FakeDriver,
) -> None:
    from contextlib import suppress

    from qa.live.types import WatchTimeout

    polls = 0

    def messages(channel_id: str, *, after: str, daimon_id: str) -> list[Message]:
        nonlocal polls
        polls += 1
        return [
            {
                "id": "x",
                "channel_id": "thread",
                "content": "Queued" if polls == 1 else "Done",
                "working": polls == 1,
            }
        ]

    driver.turn_messages = messages  # type: ignore[method-assign]
    turn = Turn(1, "trigger", "parent", utcnow())
    backend.owned.add("parent")
    with suppress(WatchTimeout):
        backend.collect(turn, 0.1)
    assert turn.messages[0]["content"] == "Done"
    assert len(turn.progress_text_history) == 1
    snapshot = turn.progress_text_history[0]
    assert snapshot["classification"] == "working"
    assert isinstance(snapshot["elapsed_s"], float)
    frozen = snapshot["message"]
    assert isinstance(frozen, dict) and frozen["content"] == "Queued"
    assert turn.progress_first_poll_s is not None
