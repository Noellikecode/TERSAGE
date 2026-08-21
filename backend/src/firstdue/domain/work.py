"""Work records: surveys, the survey queue, referrals, approvals, write actions.

Everything in this module that leaves the building -- a referral filed with the
building department, a work order, a notification, an RMS write -- carries an
**idempotency key**. Duplicate delivery of a Pub/Sub message cannot file the
same referral twice, and the type system will not let you try.

Every write action also names its **compensating action**, because a system that
can file a referral must be able to withdraw one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from firstdue.domain.enums import (
    ApprovalThreshold,
    Department,
    Operation,
    ReferralStatus,
    SurveyOutcome,
    WriteActionStatus,
)
from firstdue.domain.keys import CanonicalKey
from firstdue.errors import MissingIdempotencyKeyError, ValidationError

#: Long enough that a caller cannot accidentally collide two distinct writes.
IdempotencyKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=8, max_length=200),
]


class WriteAction(BaseModel):
    """A single externally-visible write, with its idempotency key and rollback."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str = Field(min_length=1, max_length=120)
    agent_id: str = Field(min_length=1, max_length=120)
    agent_version: str = Field(min_length=1, max_length=40)

    target: str = Field(min_length=1, max_length=120, description="external system id")
    receiving_department: Department
    operation: Operation

    #: Required. There is no constructor path that omits it.
    idempotency_key: IdempotencyKey
    #: Hash of the request body, so a replayed key with a different body is a 409.
    payload_hash: str = Field(min_length=8, max_length=128)

    #: What this will do, in one line, shown on the approval card.
    intent: str = Field(min_length=1, max_length=300)
    #: The compensating action that undoes it, named before it is executed.
    compensating_action: str = Field(min_length=1, max_length=200)

    status: WriteActionStatus = WriteActionStatus.DRAFTED
    approval_id: str | None = Field(default=None, max_length=120)
    incident_id: str | None = Field(default=None, max_length=120)
    address_id: str | None = Field(default=None, max_length=120)

    created_at: datetime
    executed_at: datetime | None = None
    external_ref: str | None = Field(default=None, max_length=200)
    failure_reason: str | None = Field(default=None, max_length=400)

    @model_validator(mode="after")
    def _check_state(self) -> Self:
        if not self.idempotency_key.strip():
            raise MissingIdempotencyKeyError(
                "every external write requires an idempotency key",
                details={"target": self.target},
            )
        if self.status is WriteActionStatus.EXECUTED and self.executed_at is None:
            raise ValidationError(
                "an executed write action must record when it executed",
                details={"action_id": self.action_id},
            )
        if self.status is WriteActionStatus.APPROVED and self.approval_id is None:
            raise ValidationError(
                "an approved write action must name its approval record",
                details={"action_id": self.action_id},
            )
        return self


class WriteReceipt(BaseModel):
    """What an external system returned. ``replayed`` distinguishes a dedupe hit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_id: str = Field(min_length=1, max_length=120)
    action_id: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=120)
    external_ref: str = Field(min_length=1, max_length=200)
    accepted_at: datetime
    #: True when the target recognised the idempotency key and did nothing new.
    replayed: bool = False


class ApprovalStatus(StrEnum):
    STAGED = "STAGED"
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"


class ApprovalRequest(BaseModel):
    """A staged, pre-filled action awaiting one human tap.

    Telling an agency something is autonomous. Committing their resources or
    cutting a utility requires this record.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str = Field(min_length=1, max_length=120)
    action_id: str = Field(min_length=1, max_length=120)
    incident_id: str | None = Field(default=None, max_length=120)
    address_id: str | None = Field(default=None, max_length=120)

    threshold: ApprovalThreshold
    receiving_department: Department
    #: Exactly what will happen if granted, shown verbatim on the card.
    prefilled_summary: str = Field(min_length=1, max_length=500)
    #: The gateway rule that required approval.
    rule_id: str = Field(min_length=1, max_length=120)

    status: ApprovalStatus = ApprovalStatus.STAGED
    staged_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _check_decision(self) -> Self:
        decided = self.status in {ApprovalStatus.GRANTED, ApprovalStatus.DENIED}
        if decided and (self.decided_at is None or not self.decided_by):
            raise ValidationError(
                "a decided approval must record who decided it and when",
                details={"approval_id": self.approval_id},
            )
        if self.threshold is ApprovalThreshold.NONE:
            raise ValidationError(
                "an approval request cannot have threshold NONE",
                details={"approval_id": self.approval_id},
            )
        return self


class ReferralRecord(BaseModel):
    """An inter-agency referral to the building department.

    The narrative may be model-drafted; filing it is a ``REQUIRE_APPROVAL``
    write that a human captain grants. The returned case number is recorded on
    the profile.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    referral_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    conflict_id: str = Field(min_length=1, max_length=120)
    receiving_department: Department = Department.BUILDING

    #: Deterministic facts the referral rests on, cited by id.
    supporting_fact_ids: tuple[str, ...] = Field(min_length=1)
    #: Model-composed prose. Reviewed by a human before filing.
    narrative: str = Field(min_length=1, max_length=8000)

    status: ReferralStatus = ReferralStatus.DRAFTED
    idempotency_key: IdempotencyKey
    action_id: str | None = Field(default=None, max_length=120)

    drafted_at: datetime
    approved_by: str | None = Field(default=None, max_length=120)
    filed_at: datetime | None = None
    case_number: str | None = Field(default=None, max_length=120)
    withdrawn_at: datetime | None = None

    @model_validator(mode="after")
    def _check_filing(self) -> Self:
        if self.status is ReferralStatus.FILED:
            if self.filed_at is None or not self.case_number:
                raise ValidationError(
                    "a filed referral must record filed_at and the returned case number",
                    details={"referral_id": self.referral_id},
                )
            if not self.approved_by:
                raise ValidationError(
                    "a referral cannot be filed without a human approver",
                    details={"referral_id": self.referral_id},
                )
        if self.status is ReferralStatus.WITHDRAWN and self.withdrawn_at is None:
            raise ValidationError(
                "a withdrawn referral must record when it was withdrawn",
                details={"referral_id": self.referral_id},
            )
        return self


class SurveyRecord(BaseModel):
    """A human company survey. The only thing that can set ``human_verified``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    survey_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    queue_entry_id: str | None = Field(default=None, max_length=120)

    company: str = Field(min_length=1, max_length=60, description="e.g. E-05")
    surveyor: str = Field(min_length=1, max_length=120)

    started_at: datetime
    completed_at: datetime
    outcome: SurveyOutcome
    #: Attributes the crew actually looked at and confirmed.
    verified_keys: tuple[CanonicalKey, ...] = ()
    notes: str | None = Field(default=None, max_length=8000)

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.completed_at < self.started_at:
            raise ValidationError(
                "survey completed_at precedes started_at",
                details={"survey_id": self.survey_id},
            )
        if self.outcome is SurveyOutcome.COMPLETED and not self.verified_keys:
            raise ValidationError(
                "a completed survey must record at least one verified attribute",
                details={"survey_id": self.survey_id},
            )
        return self


class RankReason(BaseModel):
    """Why a structure surfaced in the queue. Cites the rule that fired."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str = Field(min_length=1, max_length=120)
    canonical_key: CanonicalKey | None = None
    detail: str = Field(min_length=1, max_length=300)
    weight: float = Field(ge=0.0, le=1.0)
    fact_ids: tuple[str, ...] = ()
    conflict_id: str | None = Field(default=None, max_length=120)


class QueueEntryStatus(StrEnum):
    RANKED = "RANKED"
    DISPATCHED = "DISPATCHED"
    SURVEYED = "SURVEYED"
    CANCELLED = "CANCELLED"


class SurveyQueueEntry(BaseModel):
    """One ranked structure in a district's survey queue."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    district_id: str = Field(min_length=1, max_length=120)

    rank: int = Field(ge=1)
    score: float = Field(ge=0.0, le=1.0)
    #: At least one reason -- a row with no reason is not allowed to exist.
    reasons: tuple[RankReason, ...] = Field(min_length=1)

    status: QueueEntryStatus = QueueEntryStatus.RANKED
    created_at: datetime
    ranked_by_version: str = Field(min_length=1, max_length=40)

    assigned_company: str | None = Field(default=None, max_length=60)
    dispatched_at: datetime | None = None
    calendar_event_ref: str | None = Field(default=None, max_length=200)
    survey_id: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _check_dispatch(self) -> Self:
        if self.status is QueueEntryStatus.DISPATCHED and (
            self.dispatched_at is None or not self.assigned_company
        ):
            raise ValidationError(
                "a dispatched queue entry must name the company and when it was dispatched",
                details={"entry_id": self.entry_id},
            )
        if self.status is QueueEntryStatus.SURVEYED and not self.survey_id:
            raise ValidationError(
                "a surveyed queue entry must reference its survey record",
                details={"entry_id": self.entry_id},
            )
        return self
