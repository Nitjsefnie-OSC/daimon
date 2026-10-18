"""Private, strictly validated Teams QA target configuration."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Persona(StrictModel):
    upn: str
    role: Literal["admin", "member"]
    vault_item: str
    expected_oid: str | None = None


class Journey(StrictModel):
    persona: str
    command: str
    expected: Literal["allow", "refuse"]
    refusal_phrases: list[str] = Field(default_factory=list)
    reply_in: Literal["channel", "chat"] = "channel"


class TeamsQaConfig(StrictModel):
    tenant_id: str
    client_id: str
    redirect_host: Literal["localhost"] = "localhost"
    team_id: str
    channel_id: str
    team_display_name: str
    channel_display_name: str
    bot_app_id: str
    bot_display_name: str
    token_dir: Path = Field(strict=False)
    personas: dict[str, Persona]
    journeys: dict[str, Journey] = Field(default_factory=dict)
    company: str = "teams-qa"

    @model_validator(mode="after")
    def private_cache(self) -> Self:
        """Require private cache paths and safe persona filenames."""
        repo = Path(__file__).resolve().parents[3]
        target = self.token_dir.expanduser().resolve()
        if target == repo or repo in target.parents:
            raise ValueError("token_dir must be outside the repository")
        if not self.personas:
            raise ValueError("at least one persona is required")
        if not all(re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in self.personas):
            raise ValueError("invalid persona name")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.company):
            raise ValueError("invalid company name")
        for journey in self.journeys.values():
            if journey.persona not in self.personas:
                raise ValueError("journey persona is not configured")
        self.token_dir = target
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> TeamsQaConfig:
        """Load a private JSON config and secure its token directory."""
        selected = path or Path(os.environ["DAIMON_TEAMS_QA_CONFIG"])
        config = cls.model_validate(json.loads(selected.read_text()))
        config.token_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        config.token_dir.chmod(0o700)
        return config
