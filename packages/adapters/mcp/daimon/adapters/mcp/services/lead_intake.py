"""Bounded Gmail/HubSpot/Discord transports for the native intake coordinator."""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, cast

import httpx
from daimon.core.lead_intake import (
    IntakeConfig,
    LeadDealCandidate,
    LeadProgress,
    LeadSource,
    get_relevant_deals,
    select_matching_opportunity,
)
from daimon.core.lead_intake_service import EffectRequest, ReviewRequired, VerifiedSource
from daimon.core.lead_source import decode_email, has_registration_request


class HttpIntakePorts:
    """Credentials are coordinator-owned, never mounted in an agent vault.

    No automatic HTTP retries: every create has a committed durable intent and
    uncertain responses return to the coordinator's recovery path.
    """

    def __init__(
        self, client: httpx.AsyncClient, *, gmail_token: str, hubspot_token: str, discord_token: str
    ) -> None:
        self.client = client
        self.tokens = {
            "gmail": gmail_token,
            "hubspot": hubspot_token,
            "discord": f"Bot {discord_token}",
        }
        self.hosts = {
            "gmail": "https://gmail.googleapis.com/gmail/v1/users/me/",
            "hubspot": "https://api.hubapi.com/",
            "discord": "https://discord.com/api/v10/",
        }

    async def request(
        self,
        provider: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        missing: bool = False,
    ) -> dict[str, Any]:
        response = await self.client.request(
            method,
            self.hosts[provider] + path,
            headers={
                "Authorization": ("" if provider == "discord" else "Bearer ")
                + self.tokens[provider]
            },
            json=payload,
            params=params,
        )
        if missing and response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json() if response.content else {}

    async def source(
        self, config: IntakeConfig, source: LeadSource, actor_id: str | None
    ) -> VerifiedSource:
        if source.kind == "gmail":
            profile = await self.request("gmail", "GET", "profile")
            if (
                str(profile["emailAddress"]).strip().casefold()
                != (source.mailbox or "").strip().casefold()
            ):
                raise ValueError("mailbox_mismatch")
            message = await self.request(
                "gmail", "GET", f"messages/{source.message_id}", params={"format": "raw"}
            )
            if message["id"] != source.message_id:
                raise ValueError("message_mismatch")
            if int(message["internalDate"]) < int(config.reconciliation_start.timestamp() * 1000):
                raise ValueError("source_before_activation")
            if str(profile["emailAddress"]).strip().casefold() != config.gmail_mailbox.casefold():
                raise ValueError("configured_mailbox_mismatch")
            decoded = decode_email(str(message["raw"]))
            # Retain a reconstructed MIME without any attachment payloads.
            raw = base64.urlsafe_b64encode(decoded.text.encode()).decode()
            return VerifiedSource(
                source=source, text=decoded.text, raw=raw, recipients=tuple(decoded.recipients)
            )
        channel = await self.request("discord", "GET", f"channels/{source.channel_id}")
        if (
            str(channel.get("guild_id")) != config.guild_id
            or str(channel["id"]) != source.channel_id
        ):
            raise ValueError("source_guild_mismatch")
        message = await self.request(
            "discord", "GET", f"channels/{source.channel_id}/messages/{source.message_id}"
        )
        if (
            str(message["id"]) != source.message_id
            or str(message["channel_id"]) != source.channel_id
            or str(message["author"]["id"]) != actor_id
            or message["author"].get("bot", False)
            or not has_registration_request(str(message["content"]))
        ):
            raise ValueError("explicit_author_registration_required")
        return VerifiedSource(source=source, text=str(message["content"]))

    async def scan(
        self, config: IntakeConfig, start: datetime, end: datetime, cursor: str | None
    ) -> tuple[list[str], str | None]:
        profile = await self.request("gmail", "GET", "profile")
        if str(profile["emailAddress"]).casefold() != config.gmail_mailbox.casefold():
            raise ValueError("configured_mailbox_mismatch")
        params = {
            "q": f"in:anywhere after:{int(start.timestamp()) - 1} before:{int(end.timestamp())}",
            "includeSpamTrash": "true",
            "maxResults": "100",
        }
        if cursor:
            params["pageToken"] = cursor
        page = await self.request("gmail", "GET", "messages", params=params)
        return [str(item["id"]) for item in page.get("messages", [])], page.get("nextPageToken")

    async def _crm_check(self, config: IntakeConfig) -> None:
        portal = await self.request("hubspot", "GET", "account-info/v3/details")
        if str(portal["portalId"]) != config.portal_id:
            raise ReviewRequired("portal_mismatch")
        for kind, name in (
            ("companies", "daimon_company_identity"),
            ("deals", "daimon_intake_identity"),
        ):
            prop = await self.request("hubspot", "GET", f"crm/v3/properties/{kind}/{name}")
            if prop.get("hasUniqueValue") is not True:
                raise ReviewRequired("unique_schema_required")

    async def _objects(self, kind: str, properties: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        cursor = ""
        seen: set[str] = set()
        while True:
            page = await self.request(
                "hubspot",
                "GET",
                f"crm/v3/objects/{kind}",
                params={
                    "limit": "100",
                    "properties": properties,
                    **({"after": cursor} if cursor else {}),
                },
            )
            result.extend(cast("list[dict[str, Any]]", page["results"]))
            cursor = str(page.get("paging", {}).get("next", {}).get("after", ""))
            if not cursor:
                return result
            if cursor in seen or len(result) > 100_000:
                raise ValueError("crm_pagination_limit")
            seen.add(cursor)

    async def _associations(self, kind: str, identity: str, target: str) -> set[str]:
        found: set[str] = set()
        after = ""
        seen: set[str] = set()
        while True:
            page = await self.request(
                "hubspot",
                "GET",
                f"crm/v4/objects/{kind}/{identity}/associations/{target}",
                params={"limit": "500", **({"after": after} if after else {})},
            )
            found.update(str(row["toObjectId"]) for row in page.get("results", []))
            after = str(page.get("paging", {}).get("next", {}).get("after", ""))
            if not after:
                return found
            if after in seen:
                raise ValueError("association_cursor_repeated")
            seen.add(after)

    async def _messages(self, channel: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        before = ""
        for _ in range(100):
            page = cast(
                "list[dict[str, Any]]",
                await self.request(
                    "discord",
                    "GET",
                    f"channels/{channel}/messages",
                    params={"limit": "100", **({"before": before} if before else {})},
                ),
            )
            result.extend(page)
            if len(page) < 100:
                return result
            before = str(page[-1]["id"])
        raise ReviewRequired("discord_recovery_scan_incomplete")

    async def recover(self, config: IntakeConfig, effect: EffectRequest) -> str | None:
        step, p = effect.step, effect.payload
        if step == "thread":
            channel = await self.request("discord", "GET", f"channels/{p['starter']}", missing=True)
            if channel is not None:
                if str(channel.get("parent_id")) != config.channel_id:
                    raise ReviewRequired("thread_parent_mismatch")
                return str(channel["id"])
            return None
        if step == "starter" or step.startswith("dossier:"):
            channel_id = config.channel_id if step == "starter" else p["thread"]
            bot = await self.request("discord", "GET", "users/@me")
            matches = [
                m
                for m in await self._messages(channel_id)
                if str(m["author"]["id"]) == str(bot["id"])
                and str(m.get("nonce", "")) == effect.key[:24]
            ]
            if len(matches) > 1:
                raise ReviewRequired("duplicate_discord_effect")
            return str(matches[0]["id"]) if matches else None
        if step == "archive":
            message = await self.request(
                "gmail", "GET", f"messages/{p['message']}", params={"format": "minimal"}
            )
            return p["message"] if "INBOX" not in message.get("labelIds", []) else None
        await self._crm_check(config)
        if step.startswith("association:"):
            kind = step.split(":")[1]
            return (
                p["target"]
                if p["target"] in await self._associations("deals", p["deal"], kind)
                else None
            )
        kind = {"company": "companies", "contact": "contacts", "deal": "deals"}[step]
        prop = {
            "company": "daimon_company_identity",
            "contact": "email",
            "deal": "daimon_intake_identity",
        }[step]
        value = p["email"] if step == "contact" else effect.key
        from urllib.parse import quote

        record = await self.request(
            "hubspot",
            "GET",
            f"crm/v3/objects/{kind}/{quote(value, safe='')}",
            params={"idProperty": prop},
            missing=True,
        )
        if record:
            if record.get("archived"):
                raise ReviewRequired("archived_crm_record")
            return str(record["id"])
        if p.get("selected"):
            record = await self.request(
                "hubspot",
                "GET",
                f"crm/v3/objects/{kind}/{p['selected']}",
                params={"properties": "hs_is_closed"},
            )
            if (
                record.get("archived")
                or str(record.get("properties", {}).get("hs_is_closed")).lower() == "true"
            ):
                raise ReviewRequired("selected_record_closed")
            return str(record["id"])
        if step == "company":
            objects = await self._objects(kind, "name,domain,hs_additional_domains")
            matches = [
                r
                for r in objects
                if p["domain"]
                in {
                    str(r["properties"].get("domain") or "").casefold().rstrip("."),
                    *str(r["properties"].get("hs_additional_domains") or "").casefold().split(";"),
                }
            ]
            names = [
                r
                for r in objects
                if str(r["properties"].get("name", "")).casefold() == p["name"].casefold()
            ]
            if len(matches) > 1 or (not matches and names):
                raise ReviewRequired("company_ambiguous")
            return str(matches[0]["id"]) if matches else None
        if step == "deal":
            objects = await self._objects(kind, "dealname,description,hs_is_closed")
            company = await self._associations("companies", p["company"], "deals")
            contact = await self._associations("contacts", p["contact"], "deals")
            candidates = get_relevant_deals(
                [
                    LeadDealCandidate(
                        id=str(r["id"]),
                        name=str(r["properties"].get("dealname") or ""),
                        description=str(r["properties"].get("description") or ""),
                        is_closed=str(r["properties"].get("hs_is_closed")).lower() == "true",
                        company_ids=[p["company"]] if str(r["id"]) in company else [],
                        contact_ids=[p["contact"]] if str(r["id"]) in contact else [],
                    )
                    for r in objects
                ],
                company_id=p["company"],
                contact_id=p["contact"],
                company_name=p["company_name"],
                company_domain=p["domain"],
                opportunity_description=p["description"],
            )
            match = select_matching_opportunity(
                candidates,
                p["description"],
                LeadProgress(separate_opportunity_confirmed=p["separate"] == "True"),
            )
            if match.requires_review:
                raise ReviewRequired("opportunity_ambiguous")
            return match.deal_id
        return None

    async def write(self, config: IntakeConfig, effect: EffectRequest) -> str:
        step, p = effect.step, effect.payload
        if step == "starter" or step.startswith("dossier:"):
            channel = config.channel_id if step == "starter" else p["thread"]
            # Discord's allowlist controls actual pings even for malicious source text.
            payload = dict(
                content=p["content"],
                nonce=effect.key[:24],
                enforce_nonce=True,
                allowed_mentions={
                    "parse": [],
                    "users": [] if step == "starter" else list(config.notifier_user_ids),
                },
            )
            response = await self.request(
                "discord", "POST", f"channels/{channel}/messages", payload
            )
            return str(response["id"])
        if step == "thread":
            response = await self.request(
                "discord",
                "POST",
                f"channels/{config.channel_id}/messages/{p['starter']}/threads",
                {"name": p["title"][:100], "auto_archive_duration": 10080},
            )
            return str(response["id"])
        if step == "archive":
            await self.request(
                "gmail", "POST", f"messages/{p['message']}/modify", {"removeLabelIds": ["INBOX"]}
            )
            return p["message"]
        await self._crm_check(config)
        if step.startswith("association:"):
            kind = step.split(":")[1]
            await self.request(
                "hubspot",
                "PUT",
                f"crm/v4/objects/deals/{p['deal']}/associations/default/{kind}/{p['target']}",
            )
            if p["target"] not in await self._associations("deals", p["deal"], kind):
                raise ValueError("association_readback_missing")
            return p["target"]
        kind = {"company": "companies", "contact": "contacts", "deal": "deals"}[step]
        if step == "company":
            properties = {
                "name": p["name"],
                "domain": p["domain"],
                "daimon_company_identity": effect.key,
            }
        elif step == "contact":
            properties = {"email": p["email"]}
        else:
            properties = {
                **config.deal_fields,
                "dealname": p["name"],
                "description": p["description"],
                "pipeline": config.pipeline,
                "dealstage": config.stage,
                "daimon_intake_identity": effect.key,
                "discord_lead_thread": p["thread_url"],
            }
        response = await self.request(
            "hubspot", "POST", f"crm/v3/objects/{kind}", {"properties": properties}
        )
        # Read back the committed remote identity before declaring success.
        remote_id = str(response["id"])
        await self.request("hubspot", "GET", f"crm/v3/objects/{kind}/{remote_id}")
        return remote_id
