"""Agent identity and grants.

**No agent holds standing access to citizen records.** Between incidents the
fleet can read permits and hazmat metadata and nothing about any person.

Two grant types, deliberately different shapes:

* :class:`StandingGrant` -- the slow loop. Lives indefinitely, limited to
  ``PUBLIC`` and ``TIER_II_CONFIDENTIAL`` metadata. It is structurally incapable
  of carrying PHI access.
* :class:`IncidentGrant` -- the incident loop. Minted at dispatch, scoped to one
  address, dies at incident close.

Read scopes and write scopes are separate throughout: an agent authorized to
read the permit system is not thereby authorized to file with it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.enums import Classification, Department, Scope
from firstdue.errors import GrantExpiredError, NotAuthorizedError, ValidationError

#: Scopes that grant read access to derived person-level information.
PERSON_SCOPES: frozenset[Scope] = frozenset({Scope.READ_EMS_DERIVED})

#: Everything the slow loop is ever allowed to see.
STANDING_ALLOWED_CLASSIFICATIONS: frozenset[Classification] = frozenset(
    {Classification.PUBLIC, Classification.TIER_II_CONFIDENTIAL}
)

WRITE_SCOPES: frozenset[Scope] = frozenset(
    {
        Scope.WRITE_PROFILE,
        Scope.WRITE_REFERRAL,
        Scope.WRITE_WORK_ORDER,
        Scope.WRITE_PREINCIDENT_PLAN,
        Scope.WRITE_RMS,
        Scope.REQUEST_UTILITY_SHUTOFF,
        Scope.REQUEST_ROAD_CLOSURE,
        Scope.NOTIFY_AGENCY,
    }
)

#: Everything that is not a write scope. Kept as a derived set rather than a
#: second hand-maintained list, so a new scope cannot end up in neither.
READ_SCOPES: frozenset[Scope] = frozenset(Scope) - WRITE_SCOPES


def is_write_scope(scope: Scope) -> bool:
    """Whether a scope authorizes changing something outside this process.

    The read/write split is the one authorization boundary that is never
    inferred: an agent authorized to *read* the permit system is not thereby
    authorized to *file* with it, and no amount of read scope adds up to a
    write. :meth:`_GrantBase.assert_scope` checks the exact scope asked for and
    nothing adjacent to it.
    """
    return scope in WRITE_SCOPES


class _GrantBase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_id: str = Field(min_length=1, max_length=120)
    agent_id: str = Field(min_length=1, max_length=120)
    holder_department: Department
    scopes: frozenset[Scope] = Field(min_length=1)
    issued_at: datetime

    def authorizes(self, scope: Scope) -> bool:
        """Whether this grant carries exactly this scope.

        Exactly. There is no widening rule anywhere: holding
        ``write:referral`` does not imply ``read:profile``, and holding every
        read scope in the system does not imply any write scope.
        """
        return scope in self.scopes

    @property
    def read_scopes(self) -> frozenset[Scope]:
        return frozenset(self.scopes) & READ_SCOPES

    @property
    def write_scopes(self) -> frozenset[Scope]:
        return frozenset(self.scopes) & WRITE_SCOPES

    @property
    def can_write(self) -> bool:
        return bool(self.write_scopes)

    def is_expired(self, now: datetime) -> bool:
        return False

    def assert_scope(self, scope: Scope, *, now: datetime) -> None:
        """Raise unless this grant is live and carries ``scope``."""
        if self.is_expired(now):
            raise GrantExpiredError(
                "grant has expired", details={"grant_id": self.grant_id, "scope": str(scope)}
            )
        if not self.authorizes(scope):
            raise NotAuthorizedError(
                "grant does not carry the required scope",
                details={"grant_id": self.grant_id, "scope": str(scope)},
            )


class StandingGrant(_GrantBase):
    """The slow loop's permanent, deliberately narrow authority."""

    allowed_classifications: frozenset[Classification] = frozenset(STANDING_ALLOWED_CLASSIFICATIONS)

    @model_validator(mode="after")
    def _cannot_reach_people(self) -> Self:
        forbidden = self.allowed_classifications - STANDING_ALLOWED_CLASSIFICATIONS
        if forbidden:
            raise ValidationError(
                "a standing grant may only carry PUBLIC and TIER_II_CONFIDENTIAL access",
                details={"forbidden": sorted(str(c) for c in forbidden)},
            )
        overlap = self.scopes & PERSON_SCOPES
        if overlap:
            raise ValidationError(
                "a standing grant may never carry person-level read scopes",
                details={"scopes": sorted(str(s) for s in overlap)},
            )
        return self


class IncidentGrant(_GrantBase):
    """Authority that exists only while an incident is open.

    Bound to one incident, one address, one jurisdiction, and one responding
    agency, with a hard expiry. Every one of those bindings is checked
    independently, because each corresponds to a different way authority leaks:
    a grant that outlived its incident, a grant used against the building next
    door, a grant used outside the jurisdiction that issued it, and a grant used
    by an agency that was never dispatched.
    """

    incident_id: str = Field(min_length=1, max_length=120)
    address_id: str = Field(min_length=1, max_length=120)
    alarm_level: int = Field(ge=1, le=5)
    jurisdiction_id: str = Field(min_length=1, max_length=120)
    responding_agency_id: str = Field(min_length=1, max_length=120)
    #: Present when responding outside the home jurisdiction. Drives
    #: WITHHOLD_JURISDICTION decisions at the gateway.
    mutual_aid_agreement_id: str | None = Field(default=None, max_length=120)

    expires_at: datetime
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValidationError(
                "incident grant must expire after it was issued",
                details={"grant_id": self.grant_id},
            )
        return self

    def is_expired(self, now: datetime) -> bool:
        """Expired by TTL or by revocation, whichever came first.

        ``now`` is passed in rather than read, so a test proves expiry without
        waiting and a replay reproduces exactly which grants were live.
        """
        if self.revoked_at is not None and now >= self.revoked_at:
            return True
        return now >= self.expires_at

    def ttl_seconds(self, now: datetime) -> float:
        """Seconds of authority remaining. Zero once expired or revoked."""
        end = min(self.expires_at, self.revoked_at) if self.revoked_at else self.expires_at
        return max(0.0, (end - now).total_seconds())

    def covers_address(self, address_id: str) -> bool:
        """One grant, one building. A dispatch does not open the block."""
        return self.address_id == address_id

    def covers_incident(self, incident_id: str) -> bool:
        return self.incident_id == incident_id

    def covers_agency(self, responding_agency_id: str) -> bool:
        """Only the agency that was dispatched may act on this grant."""
        return self.responding_agency_id == responding_agency_id

    def covers_jurisdiction(self, jurisdiction_id: str) -> bool:
        """In-jurisdiction, or covered by a named mutual-aid agreement."""
        if self.jurisdiction_id == jurisdiction_id:
            return True
        return self.mutual_aid_agreement_id is not None

    def revoke(self, *, at: datetime) -> IncidentGrant:
        """Revoke at incident close. Idempotent: the earliest revocation stands."""
        if self.revoked_at is not None:
            return self
        return self.model_copy(update={"revoked_at": at})

    @property
    def is_out_of_jurisdiction(self) -> bool:
        return self.mutual_aid_agreement_id is not None
