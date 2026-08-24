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

**Deciding who to call.** :meth:`ResourceAgent.request` makes one named
request and asks nothing about whether it was the right one; that has always
been the caller's problem, and the caller solved it with a fixed rule table.
Given an incident log to read -- and therefore the head agent's briefing and
this incident's own notification history -- :meth:`ResourceAgent.notify` runs
:mod:`firstdue.agents.graphs.notifier` instead: it decides which partners this
record actually calls for, drafts what each of them is told in their own terms,
drops the ones already informed, and repeats to the ones who never answered.

That graph changes *who is asked and in what words*. It does not change what
asking means. Every entry in the plan it returns goes back through
:meth:`request` one at a time, with ``approval_id=None``, so a decision that a
gas shutoff is warranted produces a staged request with a chief's name on it
and no gas main moves. There is no path from a graph node to
:class:`~firstdue.ports.writes.ExternalWriteTarget`, and that is a property of
the code rather than of the graph being careful.

With no log wired -- the default, and what the whole test suite and ``make
demo`` run -- none of it happens and this agent behaves exactly as it always
has.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.agents.graphs.base import (
    DEFAULT_MAX_STEPS,
    GraphCassette,
    GraphStop,
    ReasoningPlanner,
    graph_budget,
    park,
    run_graph,
)
from firstdue.agents.graphs.notifier import (
    IncidentReader,
    NotificationPlan,
    NotifierGraphState,
    PartnerNotification,
    deterministic_plan,
)
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
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.domain.work import ApprovalRequest, ApprovalStatus, WriteAction
from firstdue.errors import NotAuthorizedError
from firstdue.extraction.recorded import request_digest
from firstdue.gateway.engine import AccessRequest, PolicyEngine
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditSink
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.model import ModelClient
from firstdue.ports.repositories import (
    ApprovalRepository,
    IncidentLogRepository,
    WriteActionRepository,
)
from firstdue.ports.writes import ExternalWriteTarget
from firstdue.services.memory_bank import MemoryBank

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
    # The three partners this agent's own descriptor names -- mutual aid, the
    # utility, and county OEM -- finally have a way to be *told* something.
    # Until these existed the only utility kind was a shutoff, so "the crew
    # cannot de-energise this roof from the panel" could reach the utility only
    # as a request to cut the service, which put an informational message
    # behind a chief's approval and taught everyone to treat the gate as
    # paperwork. Telling a utility what is on the roof commits nobody to
    # anything, which is the whole test for this half of the file.
    ResourceRequestKind(
        kind_id="utility-conditions",
        receiving_department=Department.UTILITY,
        scope=Scope.NOTIFY_AGENCY,
        intent_template="Notify the utility of electrical conditions at {address_id}.",
        compensating_action="Retract the utility conditions notification.",
    ),
    ResourceRequestKind(
        kind_id="mutual-aid",
        receiving_department=Department.FIRE,
        scope=Scope.NOTIFY_AGENCY,
        intent_template="Notify mutual-aid companies of conditions at {address_id}.",
        compensating_action="Retract the mutual-aid notification.",
    ),
    ResourceRequestKind(
        kind_id="county-oem",
        receiving_department=Department.COUNTY_OEM,
        scope=Scope.NOTIFY_AGENCY,
        intent_template="Notify county OEM of conditions at {address_id}.",
        compensating_action="Retract the county OEM notification.",
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


class NotificationRun(BaseModel):
    """One notification pass: what it decided, and what the gateway did with it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan: NotificationPlan
    #: One per draft, in the order the plan ordered them. A gated kind appears
    #: here with ``awaiting_human`` set and no ``external_ref``.
    outcomes: tuple[ResourceOutcome, ...] = ()

    # ---- the graph. All empty on a pass that did not run one, and a pass that
    # did not run one is the default; see :attr:`ResourceAgent.reasons`.
    graph_stop: str = Field(default="", max_length=40)
    graph_steps: int = Field(default=0, ge=0)
    #: Threads opened in the memory bank when a ceiling stopped the pass.
    open_question_ids: tuple[str, ...] = ()

    @property
    def sent(self) -> tuple[str, ...]:
        """The kinds that actually reached a partner."""
        return tuple(
            outcome.kind_id for outcome in self.outcomes if outcome.external_ref is not None
        )

    @property
    def awaiting_chief(self) -> tuple[str, ...]:
        """The kinds staged on somebody's approval card and sent to nobody."""
        return tuple(outcome.kind_id for outcome in self.outcomes if outcome.awaiting_human)


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
        log: IncidentLogRepository | None = None,
        memory: MemoryBank | None = None,
        model: ModelClient | None = None,
        planner: ReasoningPlanner | None = None,
        traces: GraphCassette | None = None,
        use_langgraph: bool = True,
        max_graph_steps: int = DEFAULT_MAX_STEPS,
        agent_version: str = "1.0.0",
    ) -> None:
        self._policy = policy
        self._approvals = approvals
        self._write_actions = write_actions
        self._target = target
        self._audit = audit
        self._clock = clock
        self._ids = ids
        # Optional, exactly as they are on ``HazardWatcher``, and for the same
        # reason: with none of them wired this agent makes the requests it is
        # asked for and decides nothing, byte for byte as it always did. The
        # graph is something a deployment opts into by giving the agent the
        # incident log to read, not a change of behaviour it inherits.
        self._log = log
        self._memory = memory
        self._model = model
        self._planner = planner
        self._traces = traces
        self._use_langgraph = use_langgraph
        self._max_graph_steps = max_graph_steps
        self._agent_version = agent_version

    @property
    def reasons(self) -> bool:
        """Whether this instance decides who to notify, or is told.

        The incident log is the predicate and the only one, because it is where
        both of the graph's inputs live: the head agent's briefing is appended
        to it, and so is every notification this incident has already sent. An
        agent without it can still draft and still stage, but it would be
        reasoning from the snapshot alone -- which is what the deterministic
        rule table already does, in less time.
        """
        return self._log is not None

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

    # ------------------------------------------------------ deciding who to call

    async def plan_notifications(
        self,
        *,
        incident_id: str,
        snapshot: ProfileSnapshot,
        correlation_id: str = "",
        deadline: datetime | None = None,
    ) -> NotificationRun:
        """Decide who this record calls for and what each of them is told.

        Writes nothing, sends nothing, stages nothing. It returns a plan, and
        the plan is inert -- which is what makes it safe to run this on a five
        second budget in the middle of a working fire and then look at what it
        decided before anything acts on it.

        Three things end in the deterministic rule table, and all three are
        ordinary: no incident log wired, so there is no briefing and no history
        to reason from; the wall clock or the step bound reached, checked in the
        graph's own router; a graph that stopped anywhere but ``CLOSED``. A
        partner notified late by the fallback beats a partner not notified
        because a graph was still thinking, so exhaustion is a stop and never a
        raise.
        """
        now = self._clock.now()
        log = self._log
        if log is None:
            return NotificationRun(plan=deterministic_plan(snapshot))

        budget = graph_budget(
            AGENT_ID, deadline=deadline, started=now, max_steps=self._max_graph_steps
        )
        notification = PartnerNotification(
            budget=budget,
            reader=IncidentReader(log, incident_id=incident_id),
            planner=self._planner,
            model=self._model,
        )
        digest = request_digest("partner-notification", incident_id, snapshot.snapshot_id)
        run = await run_graph(
            notification.spec(),
            NotifierGraphState(
                district_id=snapshot.district_id,
                correlation_id=correlation_id,
                incident_id=incident_id,
                snapshot=snapshot,
                now=now,
            ),
            agent_id=AGENT_ID,
            agent_version=self._agent_version,
            budget=budget,
            request_digest=digest,
            use_langgraph=self._use_langgraph,
            recorded=self._traces.load(digest) if self._traces is not None else None,
        )
        if self._traces is not None:
            self._traces.store(run.trace)

        steps = len(run.trace.records)
        if run.trace.stop is not GraphStop.CLOSED:
            # Whatever the graph had got to is discarded rather than shipped
            # half-decided: a pass that parked before ``suppress`` ran does not
            # know who was already told, and a partial plan is harder to reason
            # about afterwards than the table everyone already knows.
            fallback = deterministic_plan(snapshot)
            return NotificationRun(
                plan=fallback.model_copy(update={"stop": run.trace.stop, "steps": steps}),
                graph_stop=str(run.trace.stop),
                graph_steps=steps,
                open_question_ids=await self._park_notification(run.state, snapshot=snapshot),
            )

        state = run.state
        return NotificationRun(
            plan=NotificationPlan(
                drafts=state.drafts,
                suppressed=state.suppressed,
                stop=run.trace.stop,
                steps=steps,
                deterministic=False,
                unresolved_pointers=state.unresolved_pointers,
            ),
            graph_stop=str(run.trace.stop),
            graph_steps=steps,
        )

    async def notify(
        self,
        *,
        grant: IncidentGrant,
        incident_id: str,
        snapshot: ProfileSnapshot,
        correlation_id: str = "",
        deadline: datetime | None = None,
    ) -> NotificationRun:
        """Make every call the plan asks for, one at a time, through the gateway.

        ``approval_id`` is not a parameter here and is never passed on, and that
        omission is the entire gate. :meth:`request` hands the gateway a request
        with no approval on it, so a shutoff or a closure comes back
        ``REQUIRE_APPROVAL`` and is staged for a chief; only
        :meth:`IncidentSession.approve` -- a human tapping a card -- ever calls
        :meth:`request` with an approval id, and only that call reaches
        :meth:`_execute`. The agent may decide a shutoff is warranted, draft the
        request, and stage it. It cannot close a gas main, and no argument about
        how confident the graph was changes that, because the code path does not
        exist.
        """
        run = await self.plan_notifications(
            incident_id=incident_id,
            snapshot=snapshot,
            correlation_id=correlation_id,
            deadline=deadline,
        )
        outcomes = [
            await self.request(
                draft.kind_id,
                grant=grant,
                incident_id=incident_id,
                address_id=snapshot.address_id,
                detail=draft.detail,
            )
            for draft in run.plan.drafts
        ]
        logger.info(
            "partner_notifications_made",
            extra={
                "incident_id": incident_id,
                "planned": len(run.plan.drafts),
                "suppressed": len(run.plan.suppressed),
                "graph_stop": run.graph_stop or "none",
            },
        )
        return run.model_copy(update={"outcomes": tuple(outcomes)})

    async def _park_notification(
        self, state: NotifierGraphState, *, snapshot: ProfileSnapshot
    ) -> tuple[str, ...]:
        """Open one thread for a pass that ran out before it finished deciding.

        Fixed question text, because the bank derives the thread id from it and
        a reworded sentence would open a second thread beside the one already
        being carried. It names no partner and no condition: an incident that
        exhausted a five-second budget is a fact about this system, not about
        the building.
        """
        question_id = await park(
            self._memory,
            agent_id=AGENT_ID,
            agent_version=self._agent_version,
            question="Which partner notifications did the incident pass not decide?",
            classification=Classification.PUBLIC,
            state=state,
            address_id=snapshot.address_id,
        )
        return (question_id,) if question_id is not None else ()

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
