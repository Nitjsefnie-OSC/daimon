"""Synthetic staffing facts and backend contract; never uses the network."""

from datetime import UTC, datetime
from uuid import UUID

import httpx
from daimon.core.recruitment import (
    Candidate,
    RecruitmentConfig,
    SourceMessage,
    enrollment_source,
    may_decide,
)
from daimon.core.recruitment_api import RecruitmentAPI, SyntheticRecruitmentTransport
from pydantic import SecretStr


def test_enrollment_and_live_recruiter_boundary() -> None:
    config = RecruitmentConfig(guild_id="1", recruiter_role_ids=("7",))
    assert enrollment_source("  !LABS-Staffing: Synthetic", "2", config)
    assert not enrollment_source("!labs-staffing:   ", "2", config)
    assert not enrollment_source("Staffing request", "2", config)
    assert not may_decide(config, "9", ("7",), can_access=False)
    assert may_decide(config, "9", ("7",), can_access=True)
    assert not may_decide(config, "9", (), can_access=True)


async def test_fake_matches_and_idempotent_registration() -> None:
    fake = SyntheticRecruitmentTransport()
    async with httpx.AsyncClient(transport=fake) as http:
        api = RecruitmentAPI(http, "https://synthetic.invalid", SecretStr("test"))
        payload = {
            "discord_guild_id": "1",
            "discord_thread_id": "2",
            "candidate": {"name": "Synthetic"},
        }
        assert (await api.match(payload)).status == "none"
        registration = {**payload, "action": "create"}
        first = await api.register(registration, "stable")
        assert await api.register(registration, "stable") == first
        assert len(fake.effects) == 1
        assert (await api.match(payload)).status == "linked"
        assert UUID(str(first.candidate_id))


def test_source_revision_uses_real_timestamp() -> None:
    source = SourceMessage(
        id="1", content="Synthetic", observed_at=datetime.now(UTC), url="https://example.test/1"
    )
    assert source.observed_at.tzinfo is not None
    assert Candidate(name="Synthetic").name == "Synthetic"
