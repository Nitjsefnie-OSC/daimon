"""Declared source context at the universal provider-turn boundary."""

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from daimon.core.turn.admission import Admission

from daimon.core.config import RecruitmentSettings
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.turn.errors import AdmissionDenied


@dataclass(frozen=True)
class TurnPrivacy:
    """Every driver caller declares its platform and Discord source metadata.

    Unknown Discord placement fails closed for configured tenants. This is also
    checked before session preparation; the driver repeats it for direct callers.

    Declaration contract, not an independent credential: trusted service callers
    must supply the actual platform, tenant, settings and resolved placement.
    Never construct this from model/tool input. The driver cannot detect a caller
    lying about its platform, settings or parent. Prepared turns derive the tenant
    and origin from admission; direct/headless callers own that obligation.
    """

    platform: str
    recruitment: RecruitmentSettings | None = None
    tenant_id: UUID | None = None
    channel_id: str = ""
    parent_id: str | None = None

    @classmethod
    def from_admission(
        cls,
        recruitment: RecruitmentSettings,
        admission: "Admission",
        *,
        platform: str,
        tenant_id: UUID,
        fallback_channel_id: str = "",
    ) -> "TurnPrivacy":
        """Prefer the admitted tenant and source over repeated caller arguments."""
        return cls(
            platform,
            recruitment,
            admission.grant.tenant_id if admission.grant is not None else tenant_id,
            admission.origin_thread_id or admission.origin_channel_id or fallback_channel_id,
            admission.origin_channel_id,
        )

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
