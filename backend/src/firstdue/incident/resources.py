"""The Resource Agent: telling versus committing.

Two categories, and the line between them is the governance model:

**Notification is autonomous.** Telling the water department that a hydrant is
in use, telling public works that a street is blocked, telling the exposure
building next door, telling the building department what was found -- these
inform another agency. An agent may do them, because the agency remains free to
do nothing.

**Commitment is approval-gated.** Cutting gas or electric, closing a road,
committing a hazmat team, requesting collapse rescue -- these spend another
agency's resources and have consequences for people who did not ask. Each is
staged, prefilled, and waits for one human tap.

**The line is enforced by gateway policy, not by this class.** Every request
here calls :meth:`PolicyEngine.decide`, and the engine returns ``REQUIRE_APPROVAL``
based on the scope. If this file had the list wrong, the gateway would still
refuse -- which is the point of the check living there. The categorisation below
is a convenience for building the request, not the control.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import (
    ApprovalThreshold,
    Classification,
    Department,
    Operation,
    PolicyAction,
    Scope,
    WriteActionStatus,
)
from firstdue.domain.idempotency import request_hash
from firstdue.domain.identity import IncidentGrant
from firstdue.domain.work import ApprovalRequest, ApprovalStatus, WriteAction
from firstdue.errors import NotAuthorizedError
from firstdue.gateway.engine import AccessRequest, PolicyEngine
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditSink
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.repositories import ApprovalRepository, WriteActionRepository
from firstdue.ports.writes import ExternalWriteTarget

logger = get_logger(__name__)

AGENT_ID: Final[str] = "agency-notifier"
NOTIFICATION_TARGET: Final[str] = "agency-notifications"


class ResourceRequestKind(BaseModel):
    """One kind of request the agent can make, and what it needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind_id: str = Field(min_length=1, max_length=60)
    receiving_department: Department
    scope: Scope
    intent_template: str = Field(min_length=1, max_length=200)
    compensating_action: str = Field(min_length=1, max_length=200)

    @property
    def is_notification(self) -> bool:
        """Informing an agency, as opposed to spending its resources."""
        return self.scope is Scope.NOTIFY_AGENCY


#: Autonomous. Each informs an agency that remains free to act or not.
NOTIFICATIONS: Final[tuple[ResourceRequestKind, ...]] = (
    ResourceRequestKind(
        kind_id="water-supply",
        receiving_department=Department.WATER,
        scope=Scope.NOTIFY_AGENCY,
        intent_template="Notify the water department of hydrant use at {address_id}.",
        compensating_action="Retract the hydrant-use notification.",
    ),
    ResourceRequestKind(
        kind_id="public-works",
        receiving_department=Department.PUBLIC_WORKS,
        scope=Scope.NOTIFY_AGENCY,
        intent_template="Notify public works of street obstruction at {address_id}.",
        compensating_action="Retract the street-obstruction notification.",
    ),
    ResourceRequestKind(
        kind_id="exposure",
        receiving_department=Department.FIRE,
        scope=Scope.NOTIFY_AGENCY,
        intent_template="Notify the exposure address adjacent to {address_id}.",
        compensating_action="Retract the exposure notification.",
    ),
    ResourceRequestKind(
        kind_id="building-department",
        receiving_department=Department.BUILDING,
        scope=Scope.NOTIFY_AGENCY,
        intent_template="Notify the building department of conditions at {address_id}.",
        compensating_action="Retract the building-department notification.",
    ),
)

#: Approval-gated. Each spends somebody else's resources.
COMMITMENTS: Final[tuple[ResourceRequestKind, ...]] = (
    ResourceRequestKind(
        kind_id="gas-shutoff",
        receiving_department=Department.UTILITY,
        scope=Scope.REQUEST_UTILITY_SHUTOFF,
        intent_template="Request gas shutoff at {address_id}.",
        compensating_action="Withdraw the shutoff request.",
    ),
    ResourceRequestKind(
        kind_id="electric-shutoff",
        receiving_department=Department.UTILITY,
        scope=Scope.REQUEST_UTILITY_SHUTOFF,
        intent_template="Request electric shutoff at {address_id}.",
        compensating_action="Withdraw the shutoff request.",
    ),
    ResourceRequestKind(
        kind_id="road-closure",
        receiving_department=Department.POLICE,
        scope=Scope.REQUEST_ROAD_CLOSURE,
        intent_template="Request police road closure around {address_id}.",
        compensating_action="Withdraw the road-closure request.",
    ),
    ResourceRequestKind(
        kind_id="hazmat-team",
        receiving_department=Department.COUNTY_OEM,
        scope=Scope.REQUEST_ROAD_CLOSURE,
        intent_template="Request a county hazmat team to {address_id}.",
        compensating_action="Stand down the hazmat request.",
    ),
    ResourceRequestKind(
        kind_id="collapse-rescue",
        receiving_department=Department.COUNTY_OEM,
        scope=Scope.REQUEST_ROAD_CLOSURE,
        intent_template="Request collapse-rescue resources to {address_id}.",
        compensating_action="Stand down the collapse-rescue request.",
    ),
)

ALL_KINDS: Final[dict[str, ResourceRequestKind]] = {
    kind.kind_id: kind for kind in (*NOTIFICATIONS, *COMMITMENTS)
}


class ResourceOutcome(BaseModel):
    """What happened to one resource request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind_id: str
    action: PolicyAction
    rule_id: str
    decision_id: str
    #: Set when the request was sent. Absent when it is waiting for a human.
    external_ref: str | None = None
    #: Set when it is staged and waiting.
    approval_id: str | None = None
    replayed: bool = False

    @property
    def sent(self) -> bool:
        return self.external_ref is not None

    @property
    def awaiting_human(self) -> bool:
        return self.action is PolicyAction.REQUIRE_APPROVAL


class ResourceAgent:
    """Notifies agencies, and stages the requests that commit them."""

    def __init__(
        self,
        *,
        policy: PolicyEngine,
        approvals: ApprovalRepository,
        write_actions: WriteActionRepository,
        target: ExternalWriteTarget,
        audit: AuditSink,
        clock: Clock,
        ids: IdGenerator,
        agent_version: str = "1.0.0",
    ) -> None:
        self._policy = policy
        self._approvals = approvals
        self._write_actions = write_actions
        self._target = target
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._agent_version = agent_version

    async def request(
        self,
        kind_id: str,
        *,
        grant: IncidentGrant,
        incident_id: str,
        address_id: str,
        detail: str = "",
        approval_id: str | None = None,
    ) -> ResourceOutcome:
        """Make one resource request, whatever kind it is.

        The gateway decides. This method does not branch on whether the request
        is a notification or a commitment -- it asks, and acts on the answer.
        """
        kind = ALL_KINDS.get(kind_id)
        if kind is None:
            raise NotAuthorizedError("unknown resource request", details={"kind_id": kind_id})

        decision = self._policy.decide(
            AccessRequest(
                agent_id=AGENT_ID,
                agent_version=self._agent_version,
                grant=grant,
                target=NOTIFICATION_TARGET,
                operation=Operation.NOTIFY,
                # A notification carries no record contents -- it says an
                # incident is in progress at an address. The classification of
                # the request is the classification of that, which is public.
                classification=Classification.PUBLIC,
                scope=kind.scope,
                now=self._clock.now(),
                incident_id=incident_id,
                address_id=address_id,
                responding_agency_id=grant.responding_agency_id,
                approval_id=approval_id,
            )
        )
        await self._audit.record_decision(decision)

        if decision.action is PolicyAction.REQUIRE_APPROVAL:
            staged = await self._stage(kind, decision, incident_id, address_id, detail)
            return ResourceOutcome(
                kind_id=kind_id,
                action=decision.action,
                rule_id=decision.rule_id,
                decision_id=decision.decision_id,
                approval_id=staged.approval_id,
            )

        if decision.action is not PolicyAction.ALLOW:
            logger.warning(
                "resource_request_refused",
                extra={"kind_id": kind_id, "rule_id": decision.rule_id},
            )
            return ResourceOutcome(
                kind_id=kind_id,
                action=decision.action,
                rule_id=decision.rule_id,
                decision_id=decision.decision_id,
            )

        receipt = await self._execute(kind, incident_id, address_id, detail, approval_id)
        return ResourceOutcome(
            kind_id=kind_id,
            action=decision.action,
            rule_id=decision.rule_id,
            decision_id=decision.decision_id,
            external_ref=receipt[0],
            replayed=receipt[1],
        )

    async def _stage(
        self,
        kind: ResourceRequestKind,
        decision: Any,
        incident_id: str,
        address_id: str,
        detail: str,
    ) -> ApprovalRequest:
        approval_id = f"apr_{incident_id}_{kind.kind_id}"
        existing = await self._approvals.get(approval_id)
        if existing is not None:
            return existing

        approval = ApprovalRequest(
            approval_id=approval_id,
            action_id=f"act_{incident_id}_{kind.kind_id}",
            incident_id=incident_id,
            address_id=address_id,
            threshold=decision.approval_threshold or ApprovalThreshold.CHIEF,
            receiving_department=kind.receiving_department,
            # Exactly what happens if granted, shown verbatim on the card.
            prefilled_summary=(
                kind.intent_template.format(address_id=address_id)
                + (f" {detail}" if detail else "")
            )[:500],
            rule_id=decision.rule_id,
            status=ApprovalStatus.STAGED,
            staged_at=self._clock.now(),
        )
        stored = await self._approvals.stage(approval)
        logger.info(
            "resource_request_staged",
            extra={"incident_id": incident_id, "kind_id": kind.kind_id},
        )
        return stored

    async def _execute(
        self,
        kind: ResourceRequestKind,
        incident_id: str,
        address_id: str,
        detail: str,
        approval_id: str | None,
    ) -> tuple[str, bool]:
        body: dict[str, Any] = {
            "kind_id": kind.kind_id,
            "incident_id": incident_id,
            "address_id": address_id,
            "detail": detail,
        }
        action = WriteAction(
            action_id=f"act_{incident_id}_{kind.kind_id}",
            agent_id=AGENT_ID,
            agent_version=self._agent_version,
            target=NOTIFICATION_TARGET,
            receiving_department=kind.receiving_department,
            operation=Operation.NOTIFY,
            idempotency_key=self._ids.idempotency_key("resource", incident_id, kind.kind_id),
            payload_hash=request_hash(body),
            intent=kind.intent_template.format(address_id=address_id),
            compensating_action=kind.compensating_action,
            status=(WriteActionStatus.APPROVED if approval_id else WriteActionStatus.DRAFTED),
            approval_id=approval_id,
            incident_id=incident_id,
            address_id=address_id,
            created_at=self._clock.now(),
        )
        await self._write_actions.record(action)
        receipt = await self._target.execute(action, body=body)
        await self._write_actions.save_receipt(receipt)
        return receipt.external_ref, receipt.replayed


def notification_kinds() -> tuple[str, ...]:
    return tuple(kind.kind_id for kind in NOTIFICATIONS)


def commitment_kinds() -> tuple[str, ...]:
    return tuple(kind.kind_id for kind in COMMITMENTS)


def kinds_for(scope: Scope) -> Sequence[ResourceRequestKind]:
    return [kind for kind in ALL_KINDS.values() if kind.scope is scope]
