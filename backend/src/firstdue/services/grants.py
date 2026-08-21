"""Minting and revoking authority.

No agent holds standing access to citizen records. Between incidents the fleet
runs on a :class:`~firstdue.domain.identity.StandingGrant` that is structurally
incapable of carrying PHI. At dispatch an
:class:`~firstdue.domain.identity.IncidentGrant` is minted, bound to one
incident, one address, one jurisdiction, and one responding agency, with a TTL.
At incident close it is revoked.

Both ends are audited, because "which agents could see what, and when" is the
question an investigator asks first and the one a system without grant events
cannot answer.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from firstdue.domain.enums import Classification, Department, Scope
from firstdue.domain.identity import (
    STANDING_ALLOWED_CLASSIFICATIONS,
    IncidentGrant,
    StandingGrant,
)
from firstdue.errors import NotFoundError
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditEvent, AuditEventKind, AuditSink
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.repositories import GrantRepository

logger = get_logger(__name__)

#: How long an incident grant lives if nothing revokes it. Long enough for a
#: working fire, short enough that a forgotten revocation is not open access.
DEFAULT_INCIDENT_TTL: Final[timedelta] = timedelta(hours=12)

#: What the slow loop is allowed to do, forever. Buildings, never people.
STANDING_SCOPES: Final[frozenset[Scope]] = frozenset(
    {
        Scope.READ_PUBLIC_RECORDS,
        Scope.READ_TIER_II_METADATA,
        Scope.READ_PROFILE,
        Scope.READ_GEOMETRY,
        Scope.WRITE_PROFILE,
    }
)

#: What an incident grant may carry. Person-level reads appear here and nowhere
#: else in the system.
INCIDENT_SCOPES: Final[frozenset[Scope]] = frozenset(
    {
        Scope.READ_PROFILE,
        Scope.READ_GEOMETRY,
        Scope.READ_PUBLIC_RECORDS,
        Scope.READ_TIER_II_METADATA,
        Scope.READ_EMS_DERIVED,
        Scope.WRITE_RMS,
        # The incident loop writes back what it observes: an IC resolution
        # during the 360 and a registered thermal reading both become facts on
        # the building profile, and the queue an officer returns to after the
        # incident is different because of them. The loop always did this; the
        # grant never said so, because until the fleet ran through the runtime
        # nothing checked. Routing sensor-fusion through it produced a DENIED
        # run, which is the scope declaration having been wrong all along.
        Scope.WRITE_PROFILE,
        Scope.NOTIFY_AGENCY,
        # Carried, not exercised freely: the gateway returns REQUIRE_APPROVAL
        # for these, so the scope is what makes the request *stageable* rather
        # than what makes it permitted. Without it a chief could never approve
        # a shutoff, because the agent would have no authority to ask.
        Scope.REQUEST_UTILITY_SHUTOFF,
        Scope.REQUEST_ROAD_CLOSURE,
    }
)


class GrantService:
    """Issues and revokes authority, and records both."""

    def __init__(
        self,
        *,
        grants: GrantRepository,
        audit: AuditSink,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._grants = grants
        self._audit = audit
        self._clock = clock
        self._ids = ids

    async def standing_grant(
        self,
        agent_id: str,
        *,
        department: Department = Department.FIRE,
        scopes: frozenset[Scope] | None = None,
        correlation_id: str | None = None,
    ) -> StandingGrant:
        """Issue (or return) the slow loop's permanent, narrow authority.

        The model refuses to construct one carrying person-level scope, so the
        "no standing access to citizen records" rule is enforced by the type
        rather than by this function remembering to check.
        """
        existing = await self._grants.get_standing_grant(agent_id)
        if existing is not None:
            return existing

        grant = StandingGrant(
            grant_id=self._ids.new_id("grant"),
            agent_id=agent_id,
            holder_department=department,
            scopes=frozenset(scopes or STANDING_SCOPES),
            issued_at=self._clock.now(),
            allowed_classifications=frozenset(STANDING_ALLOWED_CLASSIFICATIONS),
        )
        stored = await self._grants.store_standing_grant(grant)
        await self._record(
            AuditEventKind.GRANT_MINTED,
            actor=agent_id,
            target="standing-grant",
            correlation_id=correlation_id or self._ids.new_id("corr"),
            detail={
                "grant_id": stored.grant_id,
                "kind": "standing",
                "scope_count": str(len(stored.scopes)),
                "classifications": ",".join(sorted(str(c) for c in stored.allowed_classifications)),
            },
        )
        return stored

    async def mint_incident_grant(
        self,
        *,
        agent_id: str,
        incident_id: str,
        address_id: str,
        jurisdiction_id: str,
        responding_agency_id: str,
        alarm_level: int,
        department: Department = Department.FIRE,
        scopes: frozenset[Scope] | None = None,
        mutual_aid_agreement_id: str | None = None,
        ttl: timedelta = DEFAULT_INCIDENT_TTL,
        correlation_id: str | None = None,
    ) -> IncidentGrant:
        """Mint authority for one incident at one address.

        Every binding is set here and checked by the gateway on every access.
        The TTL exists because an incident that nobody closes must not leave a
        grant live indefinitely.
        """
        now = self._clock.now()
        grant = IncidentGrant(
            grant_id=self._ids.new_id("grant"),
            agent_id=agent_id,
            holder_department=department,
            scopes=frozenset(scopes or INCIDENT_SCOPES),
            issued_at=now,
            incident_id=incident_id,
            address_id=address_id,
            alarm_level=alarm_level,
            jurisdiction_id=jurisdiction_id,
            responding_agency_id=responding_agency_id,
            mutual_aid_agreement_id=mutual_aid_agreement_id,
            expires_at=now + ttl,
        )
        stored = await self._grants.store_incident_grant(grant)
        await self._record(
            AuditEventKind.GRANT_MINTED,
            actor=agent_id,
            target=f"incident:{incident_id}",
            correlation_id=correlation_id or self._ids.new_id("corr"),
            incident_id=incident_id,
            address_id=address_id,
            detail={
                "grant_id": stored.grant_id,
                "kind": "incident",
                "alarm_level": str(alarm_level),
                "jurisdiction_id": jurisdiction_id,
                "responding_agency_id": responding_agency_id,
                "mutual_aid_agreement_id": mutual_aid_agreement_id or "none",
                "ttl_seconds": str(int(ttl.total_seconds())),
                "person_level_scope": str(Scope.READ_EMS_DERIVED in stored.scopes),
            },
        )
        logger.info(
            "incident_grant_minted",
            extra={
                "incident_id": incident_id,
                "address_id": address_id,
                "ttl_seconds": int(ttl.total_seconds()),
            },
        )
        return stored

    async def revoke(
        self, grant_id: str, *, reason: str, correlation_id: str | None = None
    ) -> IncidentGrant:
        """Revoke at incident close. Idempotent: the earliest revocation stands."""
        grant = await self._grants.get_incident_grant(grant_id)
        if grant is None:
            raise NotFoundError("grant not found", details={"grant_id": grant_id})

        now = self._clock.now()
        revoked = await self._grants.revoke_incident_grant(grant_id, at=now)
        await self._record(
            AuditEventKind.GRANT_REVOKED,
            actor=grant.agent_id,
            target=f"incident:{grant.incident_id}",
            correlation_id=correlation_id or self._ids.new_id("corr"),
            incident_id=grant.incident_id,
            address_id=grant.address_id,
            detail={
                "grant_id": grant_id,
                "reason": reason,
                "revoked_at": revoked.revoked_at.isoformat() if revoked.revoked_at else "",
            },
        )
        logger.info(
            "incident_grant_revoked",
            extra={"incident_id": grant.incident_id, "grant_id": grant_id},
        )
        return revoked

    async def revoke_for_incident(
        self, incident_id: str, grant_id: str, *, correlation_id: str | None = None
    ) -> IncidentGrant:
        """The close-time path: authority ends when the incident does."""
        return await self.revoke(grant_id, reason="incident closed", correlation_id=correlation_id)

    async def _record(
        self,
        kind: AuditEventKind,
        *,
        actor: str,
        target: str,
        correlation_id: str,
        detail: dict[str, str],
        incident_id: str | None = None,
        address_id: str | None = None,
    ) -> None:
        await self._audit.record_event(
            AuditEvent(
                audit_id=self._ids.new_id("audit"),
                kind=kind,
                occurred_at=self._clock.now(),
                actor=actor,
                target=target,
                incident_id=incident_id,
                address_id=address_id,
                correlation_id=correlation_id,
                detail=detail,
            )
        )


def standing_grant_can_reach(classification: Classification) -> bool:
    """Whether the slow loop is allowed to see this classification at all."""
    return classification in STANDING_ALLOWED_CLASSIFICATIONS
