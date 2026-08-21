"""Gateway policy decisions.

Every read and write in FIRST DUE routes through a deterministic, allow-listed
engine. **No model participates in an authorization decision** -- ``decided_by``
is a constant on this record so that claim is checkable in the audit log rather
than asserted in a README.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.enums import ApprovalThreshold, Classification, Operation, PolicyAction
from firstdue.errors import ValidationError

DECIDER: Literal["deterministic-policy-engine"] = "deterministic-policy-engine"


class PolicyDecision(BaseModel):
    """One allow / derive / withhold / require-approval / deny outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(min_length=1, max_length=120)
    incident_id: str | None = Field(default=None, max_length=120)
    agent_id: str = Field(min_length=1, max_length=120)
    agent_version: str | None = Field(default=None, max_length=40)
    grant_id: str | None = Field(default=None, max_length=120)

    target: str = Field(min_length=1, max_length=200)
    operation: Operation
    classification: Classification
    action: PolicyAction

    #: The rule that produced this outcome. Cited in the UI reasoning trace.
    rule_id: str = Field(min_length=1, max_length=120)
    #: Operator-facing explanation. Never contains record contents.
    justification: str = Field(min_length=1, max_length=500)
    policy_version: str = Field(min_length=1, max_length=40)
    decided_at: datetime

    #: Constant. A model can explain a decision; it can never make one.
    decided_by: Literal["deterministic-policy-engine"] = DECIDER

    #: DERIVE only -- the named, audited function that produced the scoped fact.
    derivation_function: str | None = Field(default=None, max_length=120)
    #: WITHHOLD_JURISDICTION only.
    mutual_aid_agreement_id: str | None = Field(default=None, max_length=120)
    #: REQUIRE_APPROVAL only.
    approval_threshold: ApprovalThreshold | None = None
    approval_id: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _check_action_requirements(self) -> Self:
        if self.action is PolicyAction.DERIVE and not self.derivation_function:
            raise ValidationError(
                "a DERIVE decision must name the derivation function that ran",
                details={"decision_id": self.decision_id},
            )
        if self.action is PolicyAction.WITHHOLD_JURISDICTION and not self.mutual_aid_agreement_id:
            raise ValidationError(
                "a WITHHOLD_JURISDICTION decision must cite the aid agreement applied",
                details={"decision_id": self.decision_id},
            )
        if self.action is PolicyAction.REQUIRE_APPROVAL and self.approval_threshold in (
            None,
            ApprovalThreshold.NONE,
        ):
            raise ValidationError(
                "a REQUIRE_APPROVAL decision must carry a non-NONE threshold",
                details={"decision_id": self.decision_id},
            )
        if self.action is not PolicyAction.DERIVE and self.derivation_function:
            raise ValidationError(
                "only a DERIVE decision may name a derivation function",
                details={"decision_id": self.decision_id},
            )
        return self

    @property
    def released_raw_record(self) -> bool:
        """True only for ALLOW. DERIVE never releases the underlying record."""
        return self.action is PolicyAction.ALLOW
