"""Native cumulative evidence for one case hold; never invent missing usage."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import JsonValue

from mux.conformance.budget import ActualSpend, BudgetRefused, MeasuredRequest, TokenUsage
from mux.contracts.ids import ModelRef, ResourceRef
from mux.drivers.gemini.live_cert import EvidenceAliases
from mux.drivers.gemini.normalize import OUTCOMES
from mux.drivers.gemini.round_cases import INPUT_MAX, MODEL, OUTPUT_MAX
from mux.drivers.gemini.transport import Object
from mux.drivers.gemini.usage import observation_from_interaction

COUNTERS = (
    "promptTokenCount",
    "cachedContentTokenCount",
    "candidatesTokenCount",
    "thoughtsTokenCount",
)


def counter_metadata(raw: Object) -> Object:
    """Retain each whitelisted raw category independently, including malformed ones.

    This is lower-bound evidence, not the shared exact-usage decoder. A malformed
    sibling must never throw before independently valid facts reach the book.
    """
    native = raw.get("usageMetadata")
    meter = native if native is not None else raw.get("usage")
    names = (
        "total_input_tokens",
        "total_cached_tokens",
        "total_output_tokens",
        "total_thought_tokens",
    )
    result: Object = {"source": "usageMetadata" if native is not None else "interactions.usage"}
    for name, interaction_name in zip(COUNTERS, names, strict=True):
        result[name] = (
            meter.get(name if native is not None else interaction_name)
            if isinstance(meter, dict)
            else (None if meter is None else "invalid-meter")
        )
    return result


def timestamp(value: JsonValue) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result.astimezone(UTC) if result.tzinfo is not None else None


@dataclass
class NativeEvidence:
    session: ResourceRef
    request_sha256: str
    request_model: str | None = None
    updated: datetime | None = None
    latest: Object | None = None
    maxima: dict[str, int] = field(default_factory=dict[str, int])
    tainted_counters: set[str] = field(default_factory=set[str])
    output_floor: int | None = None
    uncertain: bool = False
    final_get: bool = False
    final_complete: bool = False
    audit_retained: bool = False


class UsageBook:
    """Acceptance-owned IDs stay private; rejected snapshots cannot erase bounds."""

    def __init__(self, output: Path, aliases: EvidenceAliases) -> None:
        self.output, self.aliases = output, aliases
        self.native: dict[str, NativeEvidence] = {}
        self.private = output.parent / ".gemini-round-private" / output.name
        for directory in (self.private.parent, self.private):
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.is_symlink() or directory.stat().st_mode & 0o777 != 0o700:
                raise BudgetRefused("native audit directory must be private")

    def accept(
        self,
        raw: Object,
        session: ResourceRef,
        request_sha256: str,
        *,
        request_model: str | None = None,
    ) -> None:
        native_id = raw.get("id")
        if not isinstance(native_id, str) or not native_id or native_id in self.native:
            raise BudgetRefused("native acceptance identity is missing or reused")
        self.aliases.identity(native_id)  # Credential audit BEFORE aliasing/persistence.
        evidence = NativeEvidence(session, request_sha256, request_model)
        self.native[native_id] = evidence
        # An independently scoped native acceptance, not a neutral operation-key lookup.
        record = {
            "interaction_id": native_id,
            "session": session.model_dump(mode="json"),
            "model": MODEL,
            "request_sha256": request_sha256,
            "accepted_post_model": request_model,
            "usage_id_sha256": self.usage_id(native_id),
        }
        path = self.private / f"{self.usage_id(native_id)}.json"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(record, file)
            file.flush()
            os.fsync(file.fileno())
        evidence.audit_retained = True
        self.observe(native_id, raw)

    @staticmethod
    def usage_id(native_id: str) -> str:
        return hashlib.sha256(f"gemini:{native_id}:usage".encode()).hexdigest()

    def observe(self, native_id: str, raw: Object, *, final: bool = False) -> None:
        evidence = self.native[native_id]
        if final:
            if evidence.final_get:
                raise BudgetRefused("terminal usage fetch must occur exactly once")
            evidence.final_get = True
        native_model = raw.get("model")
        model_proven = native_model == MODEL or (
            native_model is None
            and evidence.request_model == MODEL
            and re.fullmatch(r"[a-f0-9]{64}", evidence.request_sha256) is not None
        )
        if raw.get("id") != native_id:
            evidence.uncertain = True
            return
        if not model_proven:
            evidence.uncertain = True  # Keep owned counts independently of model verification.
        metadata = counter_metadata(raw)
        counts: dict[str, int] = {}
        for name in COUNTERS:
            count = metadata.get(name)
            if type(count) is int and count >= 0:
                counts[name] = count
                evidence.maxima[name] = max(count, evidence.maxima.get(name, 0))
            elif count is not None:
                evidence.tainted_counters.add(name)
                evidence.uncertain = True
        candidate, thought = counts.get(COUNTERS[2]), counts.get(COUNTERS[3])
        if candidate is not None or thought is not None:
            output = (
                candidate + thought
                if candidate is not None and thought is not None
                else max(candidate or 0, thought or 0)
            )
            evidence.output_floor = max(output, evidence.output_floor or 0)
        stamp = timestamp(raw.get("updated"))
        if stamp is None or (evidence.updated is not None and stamp < evidence.updated):
            # Even a non-final contradictory/stale read remains durable unknown.
            evidence.uncertain = True
            return
        if evidence.latest is not None:
            prior = counter_metadata(evidence.latest)
            prior_counts = {name: value for name, value in prior.items() if type(value) is int}
            if (
                any(
                    name not in counts or counts[name] < value
                    for name, value in prior_counts.items()
                    if name in COUNTERS
                )
                or stamp == evidence.updated
                and metadata != prior
            ):
                evidence.uncertain = True
                return
        evidence.updated, evidence.latest = stamp, dict(raw)
        if final:
            evidence.final_complete = (
                not evidence.uncertain
                and evidence.audit_retained
                and raw.get("status") in OUTCOMES
                and len(counts) == len(COUNTERS)
                and counts[COUNTERS[1]] <= counts[COUNTERS[0]]
                and all(counts[name] >= value for name, value in evidence.maxima.items())
                and counts[COUNTERS[2]] + counts[COUNTERS[3]] >= (evidence.output_floor or 0)
            )

    def failed_final(self, native_id: str) -> None:
        evidence = self.native[native_id]
        if evidence.final_get:
            raise BudgetRefused("terminal usage fetch must occur exactly once")
        evidence.final_get, evidence.uncertain = True, True

    def amounts(self, *, attempts: int) -> tuple[ActualSpend, ActualSpend]:
        measured: list[MeasuredRequest] = []
        bounds: list[MeasuredRequest] = []
        complete = attempts == len(self.native)
        observed = datetime.now(UTC)
        for native_id, evidence in self.native.items():
            complete = complete and evidence.final_complete and not evidence.uncertain
            if evidence.final_complete and not evidence.uncertain and evidence.latest is not None:
                observation = observation_from_interaction(
                    evidence.latest,
                    evidence.session,
                    revision=1,
                    observed_at=observed,
                    root_turn=native_id,
                    model=ModelRef(provider="gemini", id=MODEL),
                )
                measured.append(
                    MeasuredRequest(
                        id=self.usage_id(native_id),
                        observed_at=observation.observed_at,
                        tokens=TokenUsage(
                            input_tokens=observation.input_tokens,
                            output_tokens=observation.output_tokens,
                            input_cached_tokens=observation.input_cached_tokens,
                            input_cache_write_tokens=0,  # No cache-creation route in this runner.
                        ),
                        pricing_basis="standard-global",
                    )
                )
            maximum = evidence.maxima
            prompt, cached = maximum.get(COUNTERS[0]), maximum.get(COUNTERS[1])
            bounds.append(
                MeasuredRequest(
                    id=self.usage_id(native_id),
                    observed_at=observed,
                    tokens=TokenUsage(
                        input_tokens=max(prompt or 0, cached or 0) if prompt is not None else None,
                        input_cached_tokens=cached,
                        output_tokens=evidence.output_floor,
                        # Maxima are category floors, not one coherent exact
                        # cache split. Use the cheapest compatible remainder;
                        # never price a newer prompt with an older cached zero.
                        input_cache_write_tokens=None,
                    ),
                    pricing_basis="standard-global",
                )
            )
        return (
            ActualSpend(
                requests=tuple(measured if complete else bounds),
                containers=(),
                usage_complete=complete,
            ),
            ActualSpend(requests=tuple(bounds), containers=(), usage_complete=False),
        )

    def exceeds_interaction_limit(self) -> bool:
        return any(
            max(evidence.maxima.get(COUNTERS[0], 0), evidence.maxima.get(COUNTERS[1], 0))
            > INPUT_MAX
            or (evidence.output_floor or 0) > OUTPUT_MAX
            for evidence in self.native.values()
        )

    def public_facts(self) -> list[Object]:
        result: list[Object] = []
        for native_id, evidence in self.native.items():
            raw = evidence.latest or {}
            status = raw.get("status")
            result.append(
                {
                    "interaction": self.aliases.identity(native_id),
                    "session": self.aliases.identity(evidence.session.id),
                    "model": MODEL,
                    "request_sha256": evidence.request_sha256,
                    "accepted_post_model": evidence.request_model,
                    "model_basis": "native_snapshot"
                    if raw.get("model") == MODEL
                    else (
                        "accepted_post_bytes"
                        if raw.get("model") is None and evidence.request_model == MODEL
                        else None
                    ),
                    "usage_id_sha256": self.usage_id(native_id),
                    "updated": evidence.updated.isoformat() if evidence.updated else None,
                    "interaction_status": status
                    if status in OUTCOMES or status in ("in_progress", "requires_action")
                    else None,
                    "max_seen": dict(evidence.maxima),
                    "tainted_counters": list[JsonValue](sorted(evidence.tainted_counters)),
                    "max_seen_output": evidence.output_floor,
                    "final_get": evidence.final_get,
                    "complete": evidence.final_complete,
                    "uncertain": evidence.uncertain,
                    "private_audit_retained": evidence.audit_retained,
                }
            )
        return result
