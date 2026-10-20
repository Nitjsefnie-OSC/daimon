"""Versioned, offline watch migration manifest. Never exports or accepts secrets."""

from datetime import datetime
from typing import Literal

from daimon.core.continuity.continuation import ContinuationRequest
from pydantic import BaseModel, ConfigDict, Field, JsonValue

UNMAPPED_NOTICE = "this watch is paused until page data moves to Daimon OS"


class ImportedWatch(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    legacy_id: str = Field(min_length=1, max_length=128)
    os_binding: ContinuationRequest
    source: Literal["page_event", "poll"]
    instructions: str = Field(min_length=1, max_length=2000)
    page_id: str | None = None
    tool: str | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    projection: str = Field(default=".", max_length=2000)
    rule: Literal["change", "added", "updated"] = "change"
    interval: int = Field(default=600, ge=60, le=86400)
    baseline: dict[str, JsonValue] = Field(default_factory=dict)
    expires_at: datetime
    max_turns_per_day: int = Field(default=24, ge=1, le=120)


class WatchExport(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    version: Literal[1]
    watches: list[ImportedWatch] = Field(max_length=25)
