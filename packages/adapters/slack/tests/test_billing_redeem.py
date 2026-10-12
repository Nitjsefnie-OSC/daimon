"""Slack /billing: redeem-code button, form, submission and timed-credit lines."""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from daimon.adapters.slack.billing_panel import redeem
from daimon.adapters.slack.billing_panel.redeem import (
    evaluate_redeem_submission,
    handle_redeem_open,
    redeem_result_text,
    run_redeem_submission,
)
from daimon.adapters.slack.billing_panel.views import build_billing_container
from daimon.core.billing_panel import BillingPanelState
from daimon.core.promo_codes import build_promo_code_terms, hash_promo_code, normalize_promo_code
from daimon.core.promo_credit import ActiveTimedCredit, PromoRedeemed, PromoRedeemRefused
from daimon.core.stores import channel_budgets as budget_store
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenant_ledger
from daimon.testing.factories import make_channel_budget, make_tenant
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

_TEAM = "T_REDEEM"
_USER = "U_ADMIN"
_NOW = datetime(2026, 5, 14, 12, tzinfo=UTC)
_SINCE = datetime(2026, 5, 1, tzinfo=UTC)
_END = datetime(2026, 5, 20, tzinfo=UTC)
_MODULE = "daimon.adapters.slack.billing_panel.redeem"


def _state(**overrides: Any) -> BillingPanelState:
    base: dict[str, Any] = {
        "is_admin": True,
        "caller_user_id": _USER,
        "caller_spend": 0.0,
        "caller_turns": 0,
        "caller_cap": None,
        "guild_balance_usd": Decimal("0"),
        "guild_spend": 0.0,
        "guild_turns": 0,
        "guild_distinct_members": 0,
        "member_rows": (),
        "over_cap_count": 0,
    }
    return BillingPanelState(**(base | overrides))


def _action_ids(blocks: list[dict[str, Any]]) -> list[str]:
    return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]


def _texts(blocks: list[dict[str, Any]]) -> str:
    """Every section's and context block's text."""
    texts = [str(b.get("text", {}).get("text", "")) for b in blocks]
    texts += [str(e.get("text", "")) for b in blocks for e in b.get("elements", [])]
    return "\n".join(texts)


def _submission(code: str) -> dict[str, Any]:
    return {
        "view": {
            "id": "V_FORM",
            "private_metadata": json.dumps({"root_view_id": "V_ROOT", "channel_id": "C1"}),
            "state": {"values": {"billing_redeem_code": {"code": {"value": code}}}},
        }
    }


@contextmanager
def _slack(fake: Any, *, admin: bool) -> Iterator[None]:
    with (
        patch(f"{_MODULE}.resolve_web_client", new_callable=AsyncMock, return_value=fake.client),
        patch(f"{_MODULE}.resolve_is_admin", new_callable=AsyncMock, return_value=admin),
    ):
        yield


def _bodies(fake: Any, method: str) -> list[dict[str, Any]]:
    calls: list[Any] = (
        fake.mock.requests.get(("POST", URL(f"https://slack.com/api/{method}"))) or []
    )
    bodies: list[dict[str, Any]] = [call.kwargs["json"] for call in calls]
    return bodies


def test_the_redeem_button_shows_while_a_code_the_viewer_may_redeem_is_redeemable() -> None:
    """Both views carry the redeem button while the snapshot found a code the viewer may redeem."""
    admin = build_billing_container(_state(has_redeemable_promo_code=True), now=_NOW, since=_SINCE)
    no_code = build_billing_container(_state(), now=_NOW, since=_SINCE)
    member = build_billing_container(
        _state(is_admin=False, has_redeemable_promo_code=True), now=_NOW, since=_SINCE
    )
    member_no_code = build_billing_container(_state(is_admin=False), now=_NOW, since=_SINCE)
    assert "billing_redeem_open" in _action_ids(admin), "admins should get the redeem button"
    assert _action_ids(no_code) == ["billing_topup", "billing_lookup"], (
        "no redeemable code should hide the button"
    )
    assert _action_ids(member) == ["billing_redeem_open"], "a member gets it for a channel code"
    assert "billing_redeem_open" not in _action_ids(member_no_code), "and not without one"


def test_timed_credit_shows_under_the_total_in_both_views() -> None:
    """Live timed credit shows in both views and is absent without any."""
    credit = (ActiveTimedCredit(remaining_usd=Decimal("7.5"), ends_at=_END),)
    for is_admin in (True, False):
        blocks = build_billing_container(
            _state(is_admin=is_admin, timed_credit=credit), now=_NOW, since=_SINCE
        )
        assert "Includes $7.50 that expires. It's used first." in _texts(blocks), (
            "each view should say how much of the credit expires"
        )
    assert "Includes" not in _texts(build_billing_container(_state(), now=_NOW, since=_SINCE)), (
        "no timed credit should mean no line"
    )


def test_evaluate_redeem_submission() -> None:
    """A blank code is rejected in the form; a real one proceeds with both view ids."""
    empty = evaluate_redeem_submission(_submission("   "))
    assert not empty.proceed and empty.response_payload["response_action"] == "errors", (
        "a blank code should be rejected in the form"
    )
    ok = evaluate_redeem_submission(_submission("abc-def"))
    assert ok.proceed and (ok.code, ok.view_id, ok.root_view_id) == (
        "abc-def",
        "V_FORM",
        "V_ROOT",
    ), "a code should proceed with the form and panel view ids"
    assert ok.response_payload["response_action"] == "update", (
        "a valid submission should update the form in place"
    )


def test_redeem_result_text() -> None:
    """Each redeem outcome renders its own reply."""
    redeemed = PromoRedeemed(
        promo_code_id=uuid.uuid4(),
        kind="credit",
        amount_usd=Decimal("10"),
        credit_starts_at=None,
        credit_ends_at=None,
        granted=True,
        balance_usd=Decimal("12.5"),
    )
    assert redeem_result_text(redeemed) == "🎟️ Added *$10.00* of credit.\n\nBalance: *$12.50*", (
        "a credit reply should name the amount and balance"
    )
    timed = dataclasses.replace(
        redeemed, kind="timed", credit_ends_at=datetime(2026, 5, 20, tzinfo=UTC)
    )
    assert redeem_result_text(timed).startswith(
        "🎟️ Added *$10.00* of credit.\n\nUsed before credit with no expiry. "
    )
    assert redeem_result_text(PromoRedeemRefused("revoked")) == "That code is no longer active.", (
        "a refusal should explain itself"
    )


async def test_open_pushes_the_form_for_anyone(fake_slack_web_client: Any) -> None:
    """The redeem button pushes the form; what the code may do is decided on submit."""
    payload = {
        "team": {"id": _TEAM},
        "user": {"id": "U_MEMBER"},
        "trigger_id": "TR",
        "view": {"id": "V", "private_metadata": json.dumps({"channel_id": "C1"})},
    }
    with _slack(fake_slack_web_client, admin=False):
        await handle_redeem_open(MagicMock(), payload)
    [body] = _bodies(fake_slack_web_client, "views.push")
    assert body["view"]["callback_id"] == "billing_redeem", "a member should get the form"
    assert json.loads(body["view"]["private_metadata"]) == {
        "root_view_id": "V",
        "channel_id": "C1",
    }, "the form should remember the panel and the channel it came from"


async def test_submission_redeems_and_refreshes_the_panel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A submitted code credits the workspace and refreshes the panel; a repeat is refused."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    await make_channel_budget(db_session, tenant=tenant, platform="slack", channel_id="C1")
    terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False)
    await promo_store.insert_promo_code(
        db_session, code_hash=hash_promo_code(normalize_promo_code("WELCOME-2026")), terms=terms
    )
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory

    with _slack(fake_slack_web_client, admin=True):
        for code in ("welcome-2026", "WELCOME-2026"):
            await run_redeem_submission(
                runtime,
                fake_slack_web_client.client,
                team_id=_TEAM,
                user_id=_USER,
                decision=evaluate_redeem_submission(_submission(code)),
            )

    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("10"), (
        "the code should be credited only once"
    )
    result, root, retry = _bodies(fake_slack_web_client, "views.update")
    assert result["view_id"] == "V_FORM" and "Added *$10.00*" in _texts(result["view"]["blocks"]), (
        "the form should confirm the credit"
    )
    assert root["view_id"] == "V_ROOT" and "*$10.00* total credit left" in _texts(
        root["view"]["blocks"]
    ), "the panel should show the new balance"
    assert "*This channel*\n$0.00 of $5.00 used this month" in _texts(root["view"]["blocks"]), (
        "the refreshed panel should keep the channel's budget line"
    )
    assert json.loads(root["view"]["private_metadata"]) == {"channel_id": "C1"}, (
        "and keep the channel for the next refresh"
    )
    assert retry["view"]["callback_id"] == "billing_redeem", "a repeat should reopen the form"
    assert "already redeemed" in _texts(retry["view"]["blocks"]), (
        "a repeat should be refused as already redeemed"
    )


async def test_submission_keeps_the_success_reply_when_the_panel_refresh_fails(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A failed panel refresh after a redemption does not overwrite the success message."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False)
    await promo_store.insert_promo_code(
        db_session, code_hash=hash_promo_code(normalize_promo_code("WELCOME-2026")), terms=terms
    )
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    failing_refresh = AsyncMock(
        side_effect=OperationalError("SELECT 1", {}, ConnectionError("connection lost"))
    )

    with (
        _slack(fake_slack_web_client, admin=True),
        patch(f"{_MODULE}.load_billing_snapshot", failing_refresh),
    ):
        await run_redeem_submission(
            runtime,
            fake_slack_web_client.client,
            team_id=_TEAM,
            user_id=_USER,
            decision=evaluate_redeem_submission(_submission("WELCOME-2026")),
        )

    failing_refresh.assert_awaited_once()
    [result] = _bodies(fake_slack_web_client, "views.update")
    assert result["view_id"] == "V_FORM" and "Added *$10.00*" in _texts(result["view"]["blocks"]), (
        "the form should keep confirming the credit"
    )
    assert await tenant_ledger.get_balance(db_session, tenant_id=tenant.id) == Decimal("10"), (
        "the credit should stay in the workspace ledger"
    )


async def test_a_member_redeems_a_channel_code_into_their_channel_only(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A member's channel code raises their channel's budget; a server code and a DM are refused."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id=_TEAM)
    await make_channel_budget(db_session, tenant=tenant, platform="slack", channel_id="C1")
    await make_channel_budget(db_session, tenant=tenant, platform="slack", channel_id="C2")
    for code, cap in (("TEAM-ROOM-2026", Decimal("20")), ("WELCOME-2026", None)):
        terms = build_promo_code_terms(amount_usd=Decimal("10"), timed=False, channel_cap_usd=cap)
        await promo_store.insert_promo_code(
            db_session, code_hash=hash_promo_code(normalize_promo_code(code)), terms=terms
        )
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    audit = AsyncMock()

    def submission(code: str, channel_id: str) -> Any:
        payload = _submission(code)
        payload["view"]["private_metadata"] = json.dumps(
            {"root_view_id": "", "channel_id": channel_id}
        )
        return evaluate_redeem_submission(payload)

    with (
        _slack(fake_slack_web_client, admin=False),
        patch.object(redeem, "record_panel_write", audit),
    ):
        for code, channel_id in (
            ("team-room-2026", "C1"),
            ("WELCOME-2026", "C1"),
            ("TEAM-ROOM-2026", "D1"),
        ):
            await run_redeem_submission(
                runtime,
                fake_slack_web_client.client,
                team_id=_TEAM,
                user_id="U_MEMBER",
                decision=submission(code, channel_id),
            )

    added, server_code, in_dm = _bodies(fake_slack_web_client, "views.update")
    assert "Added *$10.00* to <#C1>'s budget" in _texts(added["view"]["blocks"])
    assert "*$15.00*" in _texts(added["view"]["blocks"]), "the new limit is named"
    assert "Only an admin can redeem that code" in _texts(server_code["view"]["blocks"])
    assert "not a DM" in _texts(in_dm["view"]["blocks"]), "a DM has no channel to credit"
    db_session.expire_all()
    limits = {
        channel_id: (
            await budget_store.get_channel_budget(
                db_session, tenant_id=tenant.id, platform="slack", channel_id=channel_id
            )
        )
        for channel_id in ("C1", "C2")
    }
    assert limits["C1"] is not None and limits["C1"].limit_usd == Decimal("15")
    assert limits["C2"] is not None and limits["C2"].limit_usd == Decimal("5"), "only C1"
    assert [c.kwargs["outcome"] for c in audit.await_args_list] == ["allowed", "denied", "denied"]
    assert audit.await_args_list[0].kwargs["target_channel_id"] == "C1", "audited to the channel"
