"""Validated response contract for the website recruitment backend."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, JsonValue, model_validator

NonEmptyStr = Annotated[str, Field(min_length=1)]


class MatchStatus(StrEnum):
    LINKED = "linked"
    EXACT = "exact"
    POSSIBLE = "possible"
    NONE = "none"


class MatchReason(StrEnum):
    EMAIL = "email"
    PROFILE_URL = "profile_url"
    NAME = "name"
    THREAD_LINK = "thread_link"


class CandidateMatch(BaseModel):
    candidate_id: UUID
    notion_page_id: UUID
    notion_page_url: HttpUrl
    name: str
    email: str | None = None
    profile_url: str | None = None
    status: str | None = None
    position: str | None = None
    location: str | None = None
    last_edited_at: datetime | None = None
    match_reasons: list[MatchReason] = Field(min_length=1)


class MatchResponse(BaseModel):
    status: MatchStatus
    candidates: list[CandidateMatch] = Field(max_length=10)
    # The bench's own select options, per constrained field. The backend drops
    # any canonical value that no longer exists on the Notion property, so an
    # absent field here means "no value is safe to send", not "unconstrained".
    field_options: dict[str, list[str]] = Field(default_factory=dict[str, list[str]])

    @model_validator(mode="after")
    def validate_candidate_count(self) -> Self:
        count = len(self.candidates)
        if (
            (self.status == MatchStatus.NONE and count != 0)
            or (self.status in (MatchStatus.EXACT, MatchStatus.LINKED) and count != 1)
            or (self.status == MatchStatus.POSSIBLE and count == 0)
        ):
            raise ValueError("Candidate count does not match status")
        return self


class RegistrationOutcome(StrEnum):
    CREATED = "created"
    LINKED = "linked"
    ALREADY_REGISTERED = "already_registered"


class RegistrationResponse(BaseModel):
    candidate_id: UUID
    notion_page_url: HttpUrl
    outcome: RegistrationOutcome


class FieldOutcome(StrEnum):
    APPLIED = "applied"
    UNCHANGED = "unchanged"
    IGNORED_STALE = "ignored_stale"
    CONFLICT = "conflict"


class FieldReason(StrEnum):
    OLDER_SOURCE = "older_source"
    REPLAYED_REVISION = "replayed_revision"
    MANUAL_EDIT_NEWER = "manual_edit_newer"
    ORDERING_UNKNOWN = "ordering_unknown"


class FieldResult(BaseModel):
    field: NonEmptyStr
    result: FieldOutcome
    reason: FieldReason | None = None
    current_value: JsonValue = None

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if self.result == FieldOutcome.CONFLICT and self.reason not in (
            FieldReason.MANUAL_EDIT_NEWER,
            FieldReason.ORDERING_UNKNOWN,
        ):
            raise ValueError("Conflict must identify its reason")
        if self.result == FieldOutcome.IGNORED_STALE and self.reason not in (
            FieldReason.OLDER_SOURCE,
            FieldReason.REPLAYED_REVISION,
        ):
            raise ValueError("Stale result must identify its reason")
        return self


class UpdateResponse(BaseModel):
    candidate_id: UUID
    notion_page_url: HttpUrl
    results: list[FieldResult] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def validate_unique_fields(self) -> Self:
        if len({result.field for result in self.results}) != len(self.results):
            raise ValueError("Duplicate field results")
        return self
