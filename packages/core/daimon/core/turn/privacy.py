"""Explicit source authority at the universal provider-turn boundary."""

from dataclasses import dataclass
from uuid import UUID

from daimon.core.config import RecruitmentSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.turn.errors import AdmissionDenied


@dataclass(frozen=True)
class TurnPrivacy:
    """Every driver caller declares its platform and Discord source metadata.

    Unknown Discord placement fails closed for configured tenants. This is also
    checked before session preparation; the driver repeats it for direct callers.
    """

    platform: str
    recruitment: RecruitmentSettings | None = None
    tenant_id: UUID | None = None
    channel_id: str = ""
    parent_id: str | None = None

    def check(self) -> None:
        if self.platform != "discord":
            return
        if self.recruitment is None:
            raise AdmissionDenied(reason="writers_none")
        if not self.recruitment.tenants:
            return
        if self.tenant_id is None:
            raise AdmissionDenied(reason="writers_none")
        for guild_id in self.recruitment.tenants:
            if derive_tenant_uuid(
                platform="discord", workspace_id=guild_id
            ) == self.tenant_id and self.recruitment.is_recruitment_channel(
                guild_id, self.channel_id, self.parent_id
            ):
                raise AdmissionDenied(reason="writers_none")
