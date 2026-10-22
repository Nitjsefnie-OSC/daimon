"""Recruiter-reviewed recruitment transactions, independent of Discord types."""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from anthropic import APIError
from daimon.core.recruitment import (
    Candidate,
    Extraction,
    Outbox,
    RecruitmentConfig,
    SourceMessage,
    ThreadState,
    enrollment_source,
    validated_changes,
)
from daimon.core.recruitment_api import RecruitmentAPI, RecruitmentAPIError
from daimon.core.recruitment_api_models import MatchResponse
from daimon.core.stores.recruitment import RecruitmentStore
from pydantic import JsonValue

type Extract = Callable[
    [list[SourceMessage], Candidate | None, dict[str, list[str]]], Awaitable[Extraction]
]
type Authorize = Callable[[str, str], Awaitable[None]]


class RecruitmentService:
    def __init__(
        self,
        store: RecruitmentStore,
        api: RecruitmentAPI,
        extract: Extract,
        authorize: Authorize,
        *,
        backend: str,
    ) -> None:
        self.store, self.api, self.extract, self.authorize, self.backend = (
            store,
            api,
            extract,
            authorize,
            backend,
        )

    async def config(self) -> RecruitmentConfig:
        config = await self.store.configuration()
        if config is None or not config.enabled:
            raise PermissionError("recruitment_disabled")
        return config

    async def enroll(self, thread_id: str, parent_id: str, title: str) -> None:
        config = await self.config()
        async with self.store.claim(thread_id):
            if await self.store.enrolled(thread_id):
                return
            if not enrollment_source(title, parent_id, config):
                return
            if await self.store.has_provider_session(thread_id):
                raise ValueError("use_a_fresh_candidate_thread")
            await self.store.save(ThreadState(thread_id=thread_id, parent_id=parent_id), config)

    async def ingest(
        self, thread_id: str, messages: list[SourceMessage], *, refresh: bool = False
    ) -> None:
        config = await self.config()
        async with self.store.claim(thread_id):
            state = await self.store.get(thread_id)
            if not state or state.phase in {"skipped", "expired"}:
                return
            for message in messages:
                await self.store.ingest(thread_id, message)
            fresh = await self.store.messages(thread_id, pending_only=True)
            if refresh and not state.candidate_id:
                fresh = messages
            elif refresh and state.changes:
                source_ids = {str(c["source_message_id"]) for c in state.changes}
                fresh = list(
                    {m.id: m for m in fresh + [m for m in messages if m.id in source_ids]}.values()
                )
            if not fresh or await self.store.pending_enrichment(thread_id):
                return
            state.revision = uuid4().hex
            state.config_revision, state.backend = config.revision, self.backend
            state.card_revision = ""
            # An uncertain request must be reconciled with its original key/body.
            if state.outbox_id:
                item = await self.store.outbox(state.outbox_id)
                if item and item.state not in {"completed", "discarded"}:
                    if item.attempts:
                        state.error = "new_facts_waiting_for_uncertain_request"
                        await self.store.save(state, config, activity=True)
                        return
                    item.state = "discarded"
                    await self.store.save_outbox(item)
                state.outbox_id = None
            state.error = "refresh_required"
            await self.store.save(state, config, activity=True)
            history = [m for m in await self.store.messages(thread_id) if not m.deleted]
            if len(history) > 200 or sum(len(m.content) for m in history) > 60000:
                state.phase, state.error = "blocked", "history_too_large"
                await self.store.save(state, config, activity=True)
                return
            options: dict[str, list[str]] = {}
            if state.matches:
                options = MatchResponse.model_validate(state.matches).field_options
            try:
                if not state.candidate_id:
                    extraction = await self.extract(history, None, options)
                    state.summary, state.flags = extraction.summary, extraction.flags
                    state.candidate = extraction.candidate
                    if extraction.other_candidates:
                        state.candidate = None
                        state.flags.append("Use a separate thread for another candidate.")
                else:
                    by_field = {str(c["field"]): c for c in state.changes}
                    for message in sorted(fresh, key=lambda m: (m.observed_at, int(m.id))):
                        # Replacing an edited source also removes proposals that
                        # the corrected text no longer supports.
                        by_field = {
                            k: v
                            for k, v in by_field.items()
                            if v["source_message_id"] != message.id
                        }
                        if message.deleted:
                            continue
                        context = [m for m in history if m.id != message.id][-29:]
                        extraction = await self.extract(
                            context + [message], state.candidate, options
                        )
                        state.summary, state.flags = extraction.summary, extraction.flags
                        if extraction.other_candidates:
                            state.flags.append("Other candidates were excluded from these updates.")
                            continue
                        by_field.update(
                            {
                                str(c["field"]): c
                                for c in validated_changes(extraction, [message], options)
                            }
                        )
                    state.changes = list(by_field.values())
                if state.candidate and not state.candidate_id:
                    identity = {
                        k: v
                        for k, v in state.candidate.model_dump(
                            mode="json", exclude_none=True
                        ).items()
                        if k in {"name", "email", "profile_url"}
                    }
                    match = await self.api.match(
                        {
                            "discord_guild_id": config.guild_id,
                            "discord_thread_id": thread_id,
                            "candidate": identity,
                        }
                    )
                    state.matches = match.model_dump(mode="json")
                state.phase, state.error = "review", None
            except (ValueError, RecruitmentAPIError, APIError, TimeoutError):
                state.phase, state.error = "blocked", "review_unavailable"
            await self.store.save(
                state, config, activity=True, processed=tuple(fresh) if state.error is None else ()
            )

    async def confirm(
        self, thread_id: str, revision: str, selection: str, actor: str, interaction: str
    ) -> None:
        await self.authorize(actor, thread_id)
        config = await self.config()
        async with self.store.claim(thread_id):
            state = await self.store.get(thread_id)
            if (
                not state
                or state.phase != "review"
                or state.revision != revision
                or state.outbox_id
                or not state.scan_complete
                or state.config_revision != config.revision
                or state.backend != self.backend
                or await self.store.pending_enrichment(thread_id)
            ):
                raise ValueError("stale_summary")
            if selection == "skip":
                state.phase = "skipped"
                await self.store.save(state, config)
                return
            if not state.candidate or state.error:
                raise ValueError("clarify_candidate_details")
            now = datetime.now(UTC)
            payload: dict[str, JsonValue]
            if selection == "update":
                if not state.candidate_id or not state.changes:
                    raise ValueError("no_confirmable_update")
                kind = "update"
                payload = {
                    "discord_guild_id": config.guild_id,
                    "discord_thread_id": thread_id,
                    "changes": [change for change in state.changes],
                }
            else:
                if state.candidate_id:
                    raise ValueError("already_linked")
                match = MatchResponse.model_validate(state.matches)
                if selection == "create" and match.status in {"exact", "linked"}:
                    raise ValueError("link_existing_candidate")
                if selection != "create" and selection not in {
                    str(c.candidate_id) for c in match.candidates
                }:
                    raise ValueError("invalid_candidate_selection")
                kind = "register"
                payload = {
                    "discord_guild_id": config.guild_id,
                    "discord_channel_id": state.parent_id,
                    "discord_thread_id": thread_id,
                    "discord_thread_url": f"https://discord.com/channels/{config.guild_id}/{thread_id}",
                    "source": "staffing",
                    "confirmation": {
                        "discord_user_id": actor,
                        "discord_interaction_id": interaction,
                        "confirmed_at": now.isoformat(),
                    },
                    "action": "create" if selection == "create" else "link",
                }
                if selection == "create":
                    payload["candidate"] = state.candidate.model_dump(
                        mode="json", exclude_none=True
                    )
                else:
                    payload["candidate_id"] = selection
                    selected = next(c for c in match.candidates if str(c.candidate_id) == selection)
                    state.candidate = Candidate(
                        name=selected.name, email=selected.email, profile_url=selected.profile_url
                    )
            key = hashlib.sha256(
                f"{self.store.tenant_id}:{self.backend}:{thread_id}:{revision}:{kind}".encode()
            ).hexdigest()
            item = Outbox(
                id=uuid4().hex,
                thread_id=thread_id,
                revision=revision,
                kind=kind,
                key=key,
                payload=payload,
                actor_id=actor,
                interaction_id=interaction,
                confirmed_at=now,
                config_revision=config.revision,
                backend=self.backend,
            )
            state.outbox_id, state.phase = item.id, "pending"
            await self.store.save(state, config, outbox=item)

    async def deliver(self, thread_id: str, *, force: bool = False) -> None:
        config = await self.config()
        async with self.store.claim(thread_id):
            state = await self.store.get(thread_id)
            if not state or not state.outbox_id:
                return
            if await self.store.pending_enrichment(thread_id):
                return
            item = await self.store.outbox(state.outbox_id)
            if not item or item.state in {"completed", "discarded", "expired"}:
                return
            if (
                item.state == "blocked"
                and not force
                or not force
                and item.next_attempt_at
                and item.next_attempt_at > datetime.now(UTC)
            ):
                return
            try:
                await self.authorize(item.actor_id, thread_id)
                if (
                    config != await self.config()
                    or item.config_revision != config.revision
                    or item.backend != self.backend
                ):
                    raise PermissionError("configuration_changed")
            except PermissionError:
                state.error, state.phase = "access_or_configuration_revoked", "blocked"
                await self.store.save(state, config)
                return
            if item.attempts == 0 and item.revision != state.revision:
                raise ValueError("stale_approval")
            # Mark uncertain before I/O; response loss/restart must use this same key.
            was_uncertain = item.attempts > 0 and item.possibly_applied
            item.possibly_applied = True
            item.state, item.attempts = "uncertain", item.attempts + 1
            await self.store.save_outbox(item)
            try:
                if item.kind == "register":
                    response = await self.api.register(item.payload, item.key)
                    state.candidate_id, state.notion_url = (
                        str(response.candidate_id),
                        str(response.notion_page_url),
                    )
                else:
                    if state.candidate_id is None:
                        raise ValueError("missing_candidate")
                    response_update = await self.api.update(
                        state.candidate_id, item.payload, item.key
                    )
                    if state.candidate:
                        candidate = state.candidate.model_dump(mode="json")
                        applied = {
                            r.field
                            for r in response_update.results
                            if r.result in {"applied", "unchanged"}
                        }
                        changes = item.payload.get("changes")
                        if isinstance(changes, list):
                            for change in changes:
                                if isinstance(change, dict) and change.get("field") in applied:
                                    candidate[str(change["field"])] = change.get("value")
                        state.candidate = Candidate.model_validate(candidate)
                    state.flags = [
                        f"{r.field}: {r.result}"
                        for r in response_update.results
                        if r.result in {"conflict", "ignored_stale"}
                    ]
                item.state, item.error, item.possibly_applied = "completed", None, False
                state.phase, state.error, state.outbox_id = "active", None, None
                if state.revision != item.revision:
                    state.phase, state.error = "blocked", "refresh_required_after_uncertain_request"
                state.changes = []
            except RecruitmentAPIError as error:
                item.error = error.code
                if error.code in {
                    "validation_error",
                    "payload_too_large",
                    "candidate_match_conflict",
                }:
                    item.possibly_applied = was_uncertain
                item.state = "pending" if error.retryable and item.attempts < 5 else "blocked"
                item.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=max(error.retry_after or 0, min(300, 5 * 2**item.attempts))
                )
                state.error, state.phase = error.code, "blocked"
            await self.store.save(state, config, outbox=item, update_outbox=True)

    async def discard(self, thread_id: str, actor: str) -> None:
        await self.authorize(actor, thread_id)
        config = await self.config()
        async with self.store.claim(thread_id):
            state = await self.store.get(thread_id)
            item = await self.store.outbox(state.outbox_id) if state and state.outbox_id else None
            if not state or not item or not item.may_discard:
                raise ValueError("uncertain_requests_cannot_be_discarded")
            item.state = "discarded"
            state.outbox_id, state.phase, state.error = None, "blocked", "refresh_required"
            await self.store.save(state, config, outbox=item, update_outbox=True)

    async def refresh(self, thread_id: str, actor: str) -> None:
        await self.authorize(actor, thread_id)
        config = await self.config()
        async with self.store.claim(thread_id):
            state = await self.store.get(thread_id)
            if not state or state.phase == "expired" or state.outbox_id:
                raise ValueError("resolve_outstanding_request_first")
            state.phase, state.error = "discovered", "refresh_required"
            state.revision = uuid4().hex
            await self.store.save(state, config)
        await self.ingest(thread_id, await self.store.messages(thread_id), refresh=True)

    async def delete_message(self, thread_id: str, message_id: str) -> None:
        from daimon.core.stores.recruitment import invalidate_source

        for id, channel, deleted in await self.store.message_manifest(thread_id):
            if id == message_id and not deleted:
                await invalidate_source(
                    self.store.sessions, self.store.tenant_id, channel, id, deleted=True
                )
                await self.ingest(thread_id, [])
                return
