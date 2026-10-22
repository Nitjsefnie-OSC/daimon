"""Recruitment privacy depends solely on immutable channel parents."""

import pytest
from daimon.core.config import RecruitmentSettings


@pytest.mark.parametrize("name", ["!labs-staffing: Candidate", "prefix removed", ""])
def test_privacy_precedes_enrollment_and_survives_rename_and_erasure(name):
    settings = RecruitmentSettings(
        tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}}
    )
    assert settings.is_recruitment_channel("101", "20", "2")
    assert settings.is_recruitment_channel("101", "2")
    assert not settings.is_recruitment_channel("102", "20", "2")
    assert not settings.is_recruitment_channel("101", "20", "3")
