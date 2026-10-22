"""Validated recruitment extraction and source attribution; no I/O."""

from enum import StrEnum
from typing import Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictStr,
    model_validator,
)


class RecruitmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceMessage(RecruitmentModel):
    id: StrictStr = Field(min_length=1)
    content: StrictStr
    deleted: bool = False
    observed_at: AwareDatetime
    url: StrictStr = Field(min_length=1)


class Candidate(RecruitmentModel):
    name: StrictStr = Field(min_length=1)
    email: StrictStr | None = None
    phone: StrictStr | None = None
    profile_url: StrictStr | None = None
    location: StrictStr | None = None
    timezone: StrictStr | None = None
    address: StrictStr | None = None
    position: StrictStr | None = None
    status: StrictStr | None = None
    bluedot: StrictStr | None = None
    skills: list[StrictStr] | None = None
    active: StrictBool | None = None


class ChangeOperation(StrEnum):
    SET = "set"
    CLEAR = "clear"


class CandidateField(StrEnum):
    NAME = "name"
    EMAIL = "email"
    PHONE = "phone"
    PROFILE_URL = "profile_url"
    LOCATION = "location"
    TIMEZONE = "timezone"
    ADDRESS = "address"
    POSITION = "position"
    STATUS = "status"
    BLUEDOT = "bluedot"
    SKILLS = "skills"
    ACTIVE = "active"


class ProposedChange(RecruitmentModel):
    field: CandidateField
    operation: ChangeOperation
    value: StrictStr | StrictBool | list[StrictStr] | None
    source_message_id: StrictStr = Field(min_length=1)
    evidence: StrictStr = Field(min_length=1)

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if self.operation == ChangeOperation.CLEAR:
            if self.value is not None or self.field == CandidateField.NAME:
                raise ValueError("Clear requires null and cannot clear a candidate name")
        elif self.field == CandidateField.ACTIVE:
            if not isinstance(self.value, bool):
                raise ValueError("Active requires a boolean")
        elif self.field == CandidateField.SKILLS:
            if not isinstance(self.value, list):
                raise ValueError("Skills requires a list")
        elif not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("Text fields require a nonempty string")
        return self


class Extraction(RecruitmentModel):
    candidate: Candidate | None = None
    summary: StrictStr
    flags: list[StrictStr] = Field(default_factory=list)
    other_candidates: list[StrictStr] = Field(default_factory=list)
    changes: list[ProposedChange] = Field(default_factory=list[ProposedChange])


def validated_changes(
    extraction: Extraction, messages: list[SourceMessage], options: dict[str, list[str]]
) -> list[dict[str, JsonValue]]:
    """Bind proposals to real source revisions, rejecting unsupported evidence/options."""
    if extraction.other_candidates:
        return []
    by_id = {message.id: message for message in messages}
    if len(by_id) != len(messages):
        raise ValueError("Duplicate source message IDs")
    if len({change.field for change in extraction.changes}) != len(extraction.changes):
        raise ValueError("Duplicate candidate fields in extraction")
    changes: list[dict[str, JsonValue]] = []
    for proposal in extraction.changes:
        source = by_id.get(proposal.source_message_id)
        if (
            source is None
            or not proposal.evidence.strip()
            or proposal.evidence not in source.content
        ):
            raise ValueError("Candidate change lacks source evidence")
        if (
            proposal.field
            in {CandidateField.STATUS, CandidateField.LOCATION, CandidateField.TIMEZONE}
            and proposal.operation == ChangeOperation.SET
            and proposal.value not in options.get(proposal.field, [])
        ):
            raise ValueError(f"Unsupported recruitment option: {proposal.field}")
        value: JsonValue = (
            [item for item in proposal.value]
            if isinstance(proposal.value, list)
            else proposal.value
        )
        changes.append(
            {
                "field": proposal.field.value,
                "operation": proposal.operation.value,
                "value": value,
                "source_message_id": source.id,
                "source_message_url": source.url,
                "source_observed_at": source.observed_at.isoformat(),
            }
        )
    return changes


class RecruitmentConfig(RecruitmentModel):
    guild_id: str
    enabled: bool = False
    revision: int = 1
    recruiter_user_ids: tuple[str, ...] = ()
    recruiter_role_ids: tuple[str, ...] = ()
    channel_ids: tuple[str, ...] = ()
    raw_retention_days: int = Field(default=7, ge=1, le=365)
    retention_days: int = Field(default=30, ge=1, le=3650)


def enrollment_source(title: str, parent_id: str, config: RecruitmentConfig) -> bool:
    prefix = "!labs-staffing:"
    name = title.lstrip()
    return (
        (not config.channel_ids or parent_id in config.channel_ids)
        and name.casefold().startswith(prefix)
        and bool(name[len(prefix) :].strip())
    )


def may_decide(
    config: RecruitmentConfig, user_id: str, role_ids: tuple[str, ...], *, can_access: bool
) -> bool:
    return can_access and (
        user_id in config.recruiter_user_ids or bool(set(role_ids) & set(config.recruiter_role_ids))
    )


class ThreadState(RecruitmentModel):
    thread_id: str
    parent_id: str
    phase: str = "discovered"
    revision: str = ""
    config_revision: int = 0
    backend: str = ""
    candidate: Candidate | None = None
    candidate_id: str | None = None
    notion_url: str | None = None
    summary: str = ""
    flags: list[str] = Field(default_factory=list)
    matches: dict[str, JsonValue] = Field(default_factory=dict)
    changes: list[dict[str, JsonValue]] = Field(
        default_factory=lambda: list[dict[str, JsonValue]]()
    )
    cursor: str = "0"
    scan_before: str | None = None
    scan_complete: bool = False
    scan_seen: list[str] = Field(default_factory=lambda: list[str]())
    card_id: str | None = None
    card_intent: str = ""
    card_revision: str = ""
    error: str | None = None
    outbox_id: str | None = None


class Outbox(RecruitmentModel):
    id: str
    thread_id: str
    revision: str
    kind: str
    key: str
    payload: dict[str, JsonValue]
    actor_id: str
    interaction_id: str
    confirmed_at: AwareDatetime
    config_revision: int
    backend: str
    state: str = "pending"
    attempts: int = 0
    # Legacy attempted requests are conservatively uncertain on load.
    possibly_applied: bool = True

    @property
    def may_discard(self) -> bool:
        return self.attempts == 0 or (
            not self.possibly_applied
            and self.error in {"validation_error", "payload_too_large", "candidate_match_conflict"}
        )

    next_attempt_at: AwareDatetime | None = None
    error: str | None = None
