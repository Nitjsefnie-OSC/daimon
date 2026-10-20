"""Durable lead intake contracts and conservative source-evidence rules."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from email.utils import getaddresses
from enum import StrEnum
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

from daimon.core.access_policy import TenantAccessPolicy, is_invoker_allowed
from daimon.core.membership import Membership
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=100_000)]


class LeadSourceKind(StrEnum):
    GMAIL = "gmail"
    MANUAL = "manual"


class LeadClassification(StrEnum):
    CONSULTING = "consulting"
    PROJECT = "project"
    OTHER = "other"


class LeadIntakeStatus(StrEnum):
    ACCEPTED = "accepted"
    PENDING = "pending"
    REVIEW = "review"
    COMPLETED = "completed"
    REJECTED = "rejected"


class LeadStep(StrEnum):
    COMPANY = "company"
    CONTACT = "contact"
    DEAL = "deal"
    ASSOCIATIONS = "associations"
    DISCORD_MESSAGE = "discord_message"
    DISCORD_THREAD = "discord_thread"
    DISCORD_DOSSIER = "discord_dossier"
    ARCHIVE = "archive"


class LeadDisposition(StrEnum):
    DISCOVERED = "discovered"
    REJECTED = "rejected"
    ACCEPTED = "accepted"
    PENDING = "pending"
    COMPLETED = "completed"


class LeadSource(BaseModel):
    """Immutable Gmail message or explicitly registered Discord message."""

    model_config = ConfigDict(frozen=True)
    kind: LeadSourceKind
    message_id: NonEmptyStr
    mailbox: str | None = None
    channel_id: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> LeadSource:
        if self.kind == LeadSourceKind.GMAIL:
            if not self.mailbox or self.channel_id:
                raise ValueError("Gmail source requires actual mailbox and no channel_id")
        elif not self.channel_id or self.mailbox:
            raise ValueError("Manual source requires channel_id and no mailbox")
        return self

    @property
    def key(self) -> str:
        if self.kind == LeadSourceKind.GMAIL:
            return f"gmail:{(self.mailbox or '').strip().lower()}:{self.message_id}"
        return f"discord:{self.channel_id}:{self.message_id}"


class LeadEvidence(BaseModel):
    """Original source and quoted evidence for the proposed enquirer."""

    model_config = ConfigDict(frozen=True)
    original_text: NonEmptyStr
    raw_source: str | None = None
    enquirer_email: str | None = None
    enquirer_name: str | None = None
    company_name: str | None = None
    company_domain: str | None = None
    enquirer_evidence: str | None = None
    company_evidence: str | None = None
    attribution_value: str | None = None
    attribution_evidence: str | None = None
    identity_verified: bool = False
    verification_notes: str | None = None


class LeadSubmission(BaseModel):
    """Verified source, classification and prepared qualified-lead delivery."""

    model_config = ConfigDict(frozen=True)
    source: LeadSource
    evidence: LeadEvidence
    classification: LeadClassification
    opportunity_description: NonEmptyStr
    thread_title: NonEmptyStr
    thread_content: NonEmptyStr


class LeadProgress(BaseModel):
    company_id: str | None = None
    contact_id: str | None = None
    deal_id: str | None = None
    discord_message_id: str | None = None
    discord_thread_id: str | None = None
    discord_dossier_chunks_posted: int = 0
    opportunity_id: UUID | None = None
    completed_steps: list[LeadStep] = Field(default_factory=lambda: list[LeadStep]())
    in_flight_step: LeadStep | None = None
    review_reason: str | None = None
    selected_company_id: str | None = None
    selected_contact_id: str | None = None
    selected_deal_id: str | None = None
    separate_opportunity_confirmed: bool = False
    resolved_by: str | None = None
    resolution_evidence: str | None = None


class LeadIntake(BaseModel):
    id: UUID
    submission: LeadSubmission
    status: LeadIntakeStatus
    progress: LeadProgress
    attempts: int
    last_error: str | None
    routine_id: UUID | None
    execution_id: UUID | None
    created_at: datetime
    updated_at: datetime


class LeadOpportunity(BaseModel):
    id: UUID
    company_key: str
    description: str
    company_id: str | None = None
    deal_id: str | None = None
    created_at: datetime


class LeadMailboxScan(BaseModel):
    id: UUID
    mailbox: str
    window_start: datetime
    window_end: datetime
    query: str
    routine_id: UUID | None
    execution_id: UUID | None
    discovered_count: int
    started_at: datetime
    completed_at: datetime | None = None
    last_error: str | None = None


class LeadMailboxMessage(BaseModel):
    mailbox: str
    message_id: str
    disposition: LeadDisposition
    intake_id: UUID | None
    reason: str | None
    discovered_at: datetime
    updated_at: datetime


# A consumer address is not evidence of a distinct organization to create.
_PERSONAL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "yahoo.com",
        "icloud.com",
        "proton.me",
        "protonmail.com",
        "aol.com",
        "live.com",
    }
)
_EMAIL_PATTERN = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
_FROM_PATTERN = re.compile(r"^\s*>?\s*From:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def verify_lead_evidence(evidence: LeadEvidence) -> LeadEvidence:
    """Require literal source quotes and the original sender for email identity."""
    reason: str | None = None
    email = (evidence.enquirer_email or "").strip().lower()
    domain = (evidence.company_domain or "").strip().lower().rstrip(".")
    quote = evidence.enquirer_evidence or ""
    company_quote = evidence.company_evidence or ""
    source = evidence.original_text
    if not email or not _EMAIL_PATTERN.fullmatch(email):
        reason = "An original-enquirer email address is required"
    elif (
        not quote
        or quote not in source
        or email not in {address.lower() for address in _EMAIL_PATTERN.findall(quote)}
    ):
        reason = "Enquirer email needs a verbatim quote from the verified source"
    else:
        # Forwarded blocks conventionally appear after the outer sender. When
        # multiple distinct original senders appear, do not choose one silently.
        sender_blocks = _FROM_PATTERN.findall(source)
        senders = [
            address.lower()
            for _, address in getaddresses(sender_blocks[1:] or sender_blocks)
            if address
        ]
        if senders and set(senders) != {email}:
            reason = "Forwarded sender identity is ambiguous or differs from enquirer"
    if reason is None:
        if not domain or domain in _PERSONAL_DOMAINS or "." not in domain:
            reason = "A verified organizational domain is required"
        elif not company_quote or company_quote not in source:
            reason = "Company identity needs a verbatim source quote"
        elif domain not in company_quote.lower():
            reason = "Company domain is absent from the company evidence"
        elif (
            not evidence.company_name or evidence.company_name.lower() not in company_quote.lower()
        ):
            reason = "Company name is absent from the company evidence"
        elif email.rsplit("@", 1)[-1] != domain:
            reason = "Enquirer and company domains differ; human identity review required"
    return evidence.model_copy(
        update={"identity_verified": reason is None, "verification_notes": reason}
    )


def get_company_key(evidence: LeadEvidence) -> str:
    """Canonical key shared by company creation claims and identity lookup."""
    if not evidence.identity_verified or not evidence.company_domain:
        raise ValueError("Company processing requires verified identity")
    return evidence.company_domain.strip().lower().rstrip(".")


DEAL_SOURCE_CHANNEL = "Cold in-mail"
DEAL_TYPE_NEW_BUSINESS = "newbusiness"


def get_discord_message_url(guild_id: str, channel_id: str, message_id: str) -> str:
    """Permalink to the Discord post that carries the lead's intake thread."""
    if not (guild_id and channel_id and message_id):
        raise ValueError("Discord lead thread link requires guild, channel and message")
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def get_intake_disposition(status: LeadIntakeStatus) -> LeadDisposition:
    if status == LeadIntakeStatus.REVIEW:
        return LeadDisposition.PENDING
    return LeadDisposition(status.value)


class LeadDealCandidate(BaseModel):
    id: NonEmptyStr
    name: str
    description: str = ""
    company_ids: list[str] = Field(default_factory=list)
    contact_ids: list[str] = Field(default_factory=list)
    is_closed: bool = False


class LeadOpportunityMatch(BaseModel):
    deal_id: str | None = None
    requires_review: bool = False
    reason: str | None = None


def _normalize_project(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold()))


def get_relevant_deals(
    candidates: list[LeadDealCandidate],
    *,
    company_id: str,
    contact_id: str,
    company_name: str,
    company_domain: str,
    opportunity_description: str,
) -> list[LeadDealCandidate]:
    """Include associated open deals and plausible missing-association records."""
    relevant: list[LeadDealCandidate] = []
    company = _normalize_project(company_name)
    domain = company_domain.casefold().strip()
    description = _normalize_project(opportunity_description)
    for candidate in candidates:
        if candidate.is_closed:
            continue
        associated = company_id in candidate.company_ids or contact_id in candidate.contact_ids
        text = f"{candidate.name} {candidate.description}"
        normalized = _normalize_project(text)
        plausible_orphan = not candidate.company_ids and (
            (bool(company) and f" {company} " in f" {normalized} ")
            or (bool(domain) and domain in text.casefold())
            or (bool(description) and description in normalized)
        )
        if associated or plausible_orphan:
            relevant.append(candidate)
    return relevant


def select_matching_opportunity(
    candidates: list[LeadDealCandidate],
    opportunity_description: str,
    progress: LeadProgress,
) -> LeadOpportunityMatch:
    """Reuse only a uniquely exact project match or an explicit human selection."""
    if progress.selected_deal_id:
        if any(candidate.id == progress.selected_deal_id for candidate in candidates):
            return LeadOpportunityMatch(deal_id=progress.selected_deal_id)
        return LeadOpportunityMatch(
            requires_review=True,
            reason="Selected opportunity is not a relevant open deal",
        )
    if progress.separate_opportunity_confirmed:
        return LeadOpportunityMatch()
    if not candidates:
        return LeadOpportunityMatch()
    description = _normalize_project(opportunity_description)
    exact = [
        candidate
        for candidate in candidates
        if description
        and description
        in {
            _normalize_project(candidate.description),
            _normalize_project(candidate.name),
        }
    ]
    if len(candidates) == 1 and len(exact) == 1:
        return LeadOpportunityMatch(deal_id=exact[0].id)
    return LeadOpportunityMatch(
        requires_review=True,
        reason="Existing opportunities require selection or confirmation of a separate project",
    )


class LeadIntakeHealth(BaseModel):
    status_counts: dict[LeadIntakeStatus, int]
    oldest_pending_at: datetime | None
    failure_count: int
    unclassified_count: int
    last_successful_scan_at: datetime | None


def get_opportunity_identity(
    company_key: str, description: str, intake_id: UUID, progress: LeadProgress
) -> UUID:
    """Stable remote identity even if a worker survives losing its DB claim."""
    if progress.selected_deal_id:
        return uuid5(NAMESPACE_URL, f"daimon:hubspot:deal:{progress.selected_deal_id}")
    if progress.separate_opportunity_confirmed:
        return intake_id
    return uuid5(
        NAMESPACE_URL,
        f"daimon:hubspot:opportunity:{company_key}:{_normalize_project(description)}",
    )


class IntakeConfig(BaseModel):
    """Operator-owned tenant configuration. Secrets are stored separately."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    agent_id: UUID
    mode: str = Field(default="capture", pattern="^(capture|active|crm_paused)$")
    revision: int = Field(default=1, ge=1)
    guild_id: str
    channel_id: str
    source_connection_id: str
    gmail_mailbox: str = ""
    crm_connection_id: str
    portal_id: str
    routine_id: UUID | None = None
    recipients: tuple[str, ...] = ()
    notifier_user_ids: tuple[str, ...] = ()
    resolver_user_ids: tuple[str, ...] = ()
    pipeline: str = "default"
    stage: str = "appointmentscheduled"
    deal_fields: dict[str, str] = Field(default_factory=dict)
    retention_days: int = Field(default=90, ge=1, le=90)
    raw_retention_days: float = Field(default=7, gt=0, le=7)
    reconciliation_start: AwareDatetime

    @model_validator(mode="after")
    def raw_ttl_is_shorter(self) -> IntakeConfig:
        if self.raw_retention_days >= self.retention_days:
            raise ValueError("Raw source retention must be shorter than lead retention")
        return self


class LeadDraft(BaseModel):
    """Model proposal; original text and verification flags are fetched server-side."""

    model_config = ConfigDict(extra="forbid")
    source: LeadSource
    classification: LeadClassification
    opportunity_description: NonEmptyStr
    thread_title: str = Field(min_length=1, max_length=100)
    thread_content: NonEmptyStr
    enquirer_email: str | None = None
    enquirer_name: str | None = None
    company_name: str | None = None
    company_domain: str | None = None
    enquirer_evidence: str | None = None
    company_evidence: str | None = None


class LeadEffectState(BaseModel):
    step: str
    stable_key: str
    state: str
    remote_id: str | None
    created_at: datetime


class LeadDestination(BaseModel):
    """The sales channel a record was captured for; its readers stay that channel's."""

    model_config = ConfigDict(frozen=True)
    guild_id: str
    channel_id: str


class IntakeRecord(BaseModel):
    id: UUID
    tenant_id: UUID
    source_digest: str
    status: str
    # Filled by the store from plaintext columns, never from the payload.
    destination: LeadDestination | None = None
    submission: LeadSubmission | None
    progress: LeadProgress = Field(default_factory=LeadProgress)
    effects: list[LeadEffectState] = Field(default_factory=lambda: list[LeadEffectState]())
    actor_id: str | None = None
    agent_id: UUID | None = None
    routine_id: UUID | None = None
    execution_id: UUID | None = None
    config_revision: int
    error_code: str | None = None
    created_at: datetime
    expires_at: datetime
    raw_expires_at: datetime


class IntakeResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: str | None = Field(default=None, pattern=r"^\d+$", max_length=20)
    contact_id: str | None = Field(default=None, pattern=r"^\d+$", max_length=20)
    deal_id: str | None = Field(default=None, pattern=r"^\d+$", max_length=20)
    separate_opportunity_confirmed: bool = False
    discord_message_id: str | None = Field(default=None, pattern=r"^\d+$", max_length=20)
    discord_post_absence_confirmed: bool = False
    evidence: str = Field(min_length=20, max_length=2000)


def may_resolve(
    *,
    user_id: str,
    role_ids: tuple[str, ...],
    core_role_ids: tuple[str, ...],
    resolver_ids: tuple[str, ...],
    routine: bool,
    member: bool,
) -> bool:
    """Resolution is a human business grant, independent of server admin status."""
    return (
        member
        and not routine
        and (user_id in resolver_ids or bool(set(role_ids) & set(core_role_ids)))
    )


class IntakeBusy(Exception):
    """Another worker holds the source or company claim; resume later."""


def authorize_intake_actor(
    config: IntakeConfig,
    policy: TenantAccessPolicy,
    *,
    user_id: str,
    facts: Membership,
    agent_id: UUID | None,
    routine_id: UUID | None,
    execution_active: bool,
    resolve: bool = False,
) -> None:
    """Require live admission plus the named routine or human business grant."""
    if agent_id != config.agent_id or facts.is_member is not True:
        raise PermissionError("intake_actor_not_admitted")
    if not is_invoker_allowed(
        policy,
        external_user_id=user_id,
        is_admin=facts.is_admin,
        role_ids=facts.role_ids,
        verified_membership=facts.is_member,
    ):
        raise PermissionError("invoker_not_allowed")
    if routine_id is not None and (
        routine_id != config.routine_id or not execution_active or resolve
    ):
        raise PermissionError("routine_not_authorized")
    if resolve and not may_resolve(
        user_id=user_id,
        role_ids=facts.role_ids,
        core_role_ids=policy.invoker_role_ids,
        resolver_ids=config.resolver_user_ids,
        routine=routine_id is not None,
        member=facts.is_member is True,
    ):
        raise PermissionError("sales_resolver_or_core_role_required")


DISCORD_MESSAGE_LIMIT = 2000
_DISCORD_EPOCH_MS = 1420070400000
# Recovery starts before durable acceptance to absorb clock skew with Discord.
_RECOVERY_LEAD = timedelta(seconds=60)
_FENCE = "```"
# Room for a reopened fence line before a chunk and a closing fence after it.
_MAX_REOPENED_FENCE = 40
_FENCE_RESERVE = 2 * _MAX_REOPENED_FENCE + len("\n") + len("\n" + _FENCE)


def get_recovery_cursor(accepted_at: datetime) -> str:
    """Snowflake just before acceptance: every post for the intake comes after it."""
    milliseconds = int((accepted_at - _RECOVERY_LEAD).timestamp() * 1000)
    return str(max(0, (milliseconds - _DISCORD_EPOCH_MS) << 22))


def get_starter_content(thread_title: str) -> str:
    """The whole visible channel post; Discord trims surrounding whitespace itself."""
    return thread_title.strip()


def split_dossier(content: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split on paragraph, then line boundaries; a cut code block is closed and reopened."""
    lines = [
        piece
        for line in content.strip().split("\n")
        for piece in _split_long_line(line, limit - _FENCE_RESERVE)
    ]
    # open_fences[i] is the fence line still open before lines[i].
    open_fences: list[str | None] = [None]
    for line in lines:
        is_fence = line.lstrip().startswith(_FENCE)
        opened = open_fences[-1]
        if is_fence:
            opened = None if opened else line.strip()[:_MAX_REOPENED_FENCE]
        open_fences.append(opened)

    def render(start: int, end: int) -> str:
        reopened = open_fences[start]
        still_open = open_fences[end]
        body = "\n".join(lines[start:end])
        return (
            (f"{reopened}\n" if reopened else "") + body + (f"\n{_FENCE}" if still_open else "")
        ).strip()

    chunks: list[str] = []
    start = 0
    while start < len(lines):
        end = start + 1
        paragraph_end: int | None = None
        while end < len(lines) and len(render(start, end + 1).encode("utf-16-le")) // 2 <= limit:
            end += 1
            if not lines[end - 1].strip():
                paragraph_end = end
        if (
            end < len(lines)
            and paragraph_end is not None
            and len(render(start, paragraph_end)) >= limit // 2
        ):
            end = paragraph_end
        chunk = render(start, end)
        if chunk and chunk != _FENCE and not _is_empty_fence(chunk):
            chunks.append(chunk)
        start = end
    return chunks


def count_posted_dossier_chunks(posted: list[str], chunks: list[str]) -> int:
    """Length of the dossier prefix already visible, in order, among posted messages."""
    count = 0
    for message, chunk in zip(posted, chunks, strict=False):
        if message != chunk:
            break
        count += 1
    return count


def _split_long_line(line: str, width: int) -> list[str]:
    if len(line.encode("utf-16-le")) // 2 <= width:
        return [line]
    pieces: list[str] = []
    current: list[str] = []
    units = 0
    for char in line:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > width:
            pieces.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += size
    if current:
        pieces.append("".join(current))
    return pieces


def _is_empty_fence(chunk: str) -> bool:
    lines = chunk.split("\n")
    return len(lines) == 2 and all(line.strip().startswith(_FENCE) for line in lines)
