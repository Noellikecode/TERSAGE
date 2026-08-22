"""The autonomous action flow.

What the slow loop does once it has decided a building needs a person in it.
Five writes, and the distinction between the first four and the fifth is the
whole governance model:

**Autonomous.** A work order, a calendar hold, a crew notification, and a
pre-incident plan artifact. These commit the department's own time and write
into the department's own systems. An agent may do them.

**Approval-gated.** A referral to the building department accuses a property
owner of unpermitted construction. It commits *another agency's* time and has
consequences for a citizen, so it is staged, prefilled, and waits for a captain
to tap once. The case number that comes back is written onto the profile.

Every write carries a derived idempotency key and records its compensating
action before it executes. Run the flow twice and the receiving systems dedupe;
nothing is filed twice, no crew is invited twice, and no second case number
exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from firstdue.domain.conflicts import Conflict, ConflictStatus
from firstdue.domain.enums import (
    ApprovalThreshold,
    Department,
    Operation,
    ReferralStatus,
    WriteActionStatus,
)
from firstdue.domain.idempotency import request_hash
from firstdue.domain.preplan import PreIncidentPlan, build_plan
from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType
from firstdue.domain.runs import CompensationRecord
from firstdue.domain.work import (
    ApprovalRequest,
    ApprovalStatus,
    QueueEntryStatus,
    ReferralRecord,
    SurveyQueueEntry,
    WriteAction,
)
from firstdue.errors import NotFoundError, StaleVersionError, ValidationError
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditEvent, AuditEventKind, AuditSink
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.office import (
    CalendarClient,
    CalendarEvent,
    MailClient,
    MailMessage,
    ObjectStore,
)
from firstdue.ports.repositories import (
    ApprovalRepository,
    CompensationRepository,
    ConflictRepository,
    ProfileRepository,
    QueueRepository,
    ReferralRepository,
    WriteActionRepository,
)
from firstdue.ports.writes import ExternalWriteTarget

logger = get_logger(__name__)

AGENT_ID: Final[str] = "structure-watch"
REFERRAL_AGENT_ID: Final[str] = "referral-clerk"

WORK_ORDER_TARGET: Final[str] = "inspection-work-orders"
PLAN_TARGET: Final[str] = "preincident-plan-store"
REFERRAL_TARGET: Final[str] = "building-referral-intake"

#: How far ahead a survey is scheduled, and how long a company is held for it.
SURVEY_LEAD = timedelta(days=3)
SURVEY_DURATION = timedelta(hours=2)


class DispatchResult(BaseModel):
    """Everything one dispatch produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    address_id: str
    entry_id: str
    work_order_ref: str | None = None
    calendar_event_ref: str | None = None
    notification_ref: str | None = None
    plan_object_id: str | None = None
    plan_uri: str | None = None
    referral_id: str | None = None
    approval_id: str | None = None
    #: True when nothing new was written because it had all been done already.
    replayed: bool = False
    compensations_recorded: tuple[str, ...] = ()

    @property
    def external_refs(self) -> tuple[str, ...]:
        """Every reference an external system handed back, in citation order.

        This is what lands on the agent run record as its write actions, so a
        replay can point at the work order and the case number the run created
        rather than asserting that it created some.
        """
        return tuple(
            ref
            for ref in (
                self.work_order_ref,
                self.calendar_event_ref,
                self.notification_ref,
                self.plan_object_id,
                self.referral_id,
            )
            if ref
        )


class ApprovalResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    referral_id: str
    approval_id: str
    case_number: str
    #: True when the referral was already filed; the same case number stands.
    replayed: bool = False


class ActionFlow:
    """Dispatches a survey and stages the referral that needs a human."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        conflicts: ConflictRepository,
        queue: QueueRepository,
        referrals: ReferralRepository,
        approvals: ApprovalRepository,
        write_actions: WriteActionRepository,
        compensations: CompensationRepository,
        write_targets: dict[str, ExternalWriteTarget],
        calendar: CalendarClient,
        mailer: MailClient,
        plan_store: ObjectStore,
        clock: Clock,
        ids: IdGenerator,
        audit: AuditSink | None = None,
        agent_version: str = "1.0.0",
    ) -> None:
        self._profiles = profiles
        self._conflicts = conflicts
        self._queue = queue
        self._referrals = referrals
        self._approvals = approvals
        self._write_actions = write_actions
        self._compensations = compensations
        self._targets = write_targets
        self._calendar = calendar
        self._mailer = mailer
        self._plan_store = plan_store
        self._clock = clock
        self._ids = ids
        self._audit = audit
        self._agent_version = agent_version

    # ------------------------------------------------------------- dispatch

    async def dispatch(
        self,
        entry: SurveyQueueEntry,
        *,
        company: str,
        crew_email: str,
        correlation_id: str,
    ) -> DispatchResult:
        """Cut the work order, hold the calendar, notify the crew, write the plan."""
        profile = await self._profiles.get(entry.address_id)
        if profile is None:
            raise NotFoundError("profile not found", details={"address_id": entry.address_id})

        now = self._clock.now()
        scheduled = now + SURVEY_LEAD
        compensations: list[str] = []

        work_order_ref, replayed = await self._work_order(
            entry, company=company, profile=profile, now=now, compensations=compensations
        )
        calendar_ref = await self._calendar_hold(
            entry, company=company, crew_email=crew_email, profile=profile, starts_at=scheduled
        )
        notification_ref = await self._notify(
            entry, company=company, crew_email=crew_email, profile=profile, starts_at=scheduled
        )
        plan, stored = await self._write_plan(profile, now=now, compensations=compensations)

        await self._queue.save(
            entry.model_copy(
                update={
                    "status": QueueEntryStatus.DISPATCHED,
                    "assigned_company": company,
                    "dispatched_at": now,
                    "calendar_event_ref": calendar_ref,
                }
            )
        )

        referral_id, approval_id = await self._stage_referral(
            profile, entry=entry, now=now, correlation_id=correlation_id
        )

        logger.info(
            "survey_dispatched",
            extra={
                "address_id": entry.address_id,
                "company": company,
                "replayed": replayed,
                "referral_staged": referral_id is not None,
            },
        )
        return DispatchResult(
            address_id=entry.address_id,
            entry_id=entry.entry_id,
            work_order_ref=work_order_ref,
            calendar_event_ref=calendar_ref,
            notification_ref=notification_ref,
            plan_object_id=stored.object_id,
            plan_uri=stored.uri,
            referral_id=referral_id,
            approval_id=approval_id,
            replayed=replayed,
            compensations_recorded=tuple(compensations),
        )

    async def _work_order(
        self,
        entry: SurveyQueueEntry,
        *,
        company: str,
        profile: BuildingProfile,
        now: datetime,
        compensations: list[str],
    ) -> tuple[str | None, bool]:
        target = self._targets.get(WORK_ORDER_TARGET)
        if target is None:
            return None, False

        key = self._ids.idempotency_key("work-order", entry.entry_id)
        body: dict[str, Any] = {
            "address_id": entry.address_id,
            "district_id": entry.district_id,
            "company": company,
            "rank": entry.rank,
            "reasons": [r.detail for r in entry.reasons],
        }
        action = WriteAction(
            action_id=f"act_wo_{entry.entry_id}",
            agent_id=AGENT_ID,
            agent_version=self._agent_version,
            target=WORK_ORDER_TARGET,
            receiving_department=Department.FIRE,
            operation=Operation.WRITE,
            idempotency_key=key,
            payload_hash=request_hash(body),
            intent=f"Open a survey work order for {entry.address_id} assigned to {company}.",
            compensating_action="Cancel the work order.",
            status=WriteActionStatus.DRAFTED,
            address_id=entry.address_id,
            created_at=now,
        )
        await self._write_actions.record(action)
        receipt = await target.execute(action, body=body)
        await self._write_actions.save_receipt(receipt)
        await self._write_actions.record(
            action.model_copy(
                update={
                    "status": WriteActionStatus.EXECUTED,
                    "executed_at": now,
                    "external_ref": receipt.external_ref,
                }
            )
        )
        compensations.append(
            await self._record_compensation(
                action_id=action.action_id,
                target=WORK_ORDER_TARGET,
                compensating_action=action.compensating_action,
                reason="Survey work order was opened autonomously.",
                now=now,
            )
        )
        await self._audit_event(
            AuditEventKind.WRITE_REPLAYED if receipt.replayed else AuditEventKind.WRITE_EXECUTED,
            actor=AGENT_ID,
            target=WORK_ORDER_TARGET,
            address_id=entry.address_id,
            detail={
                "action_id": action.action_id,
                "external_ref": receipt.external_ref,
                "payload_hash": action.payload_hash,
                "replayed": str(receipt.replayed),
            },
        )
        return receipt.external_ref, receipt.replayed

    async def _calendar_hold(
        self,
        entry: SurveyQueueEntry,
        *,
        company: str,
        crew_email: str,
        profile: BuildingProfile,
        starts_at: datetime,
    ) -> str | None:
        key = self._ids.idempotency_key("calendar", entry.entry_id)
        event = CalendarEvent(
            event_id=f"cal_{entry.entry_id}",
            calendar_id=f"{company.lower()}@sffd.example",
            summary=f"Company survey - {entry.address_id}",
            description=(
                "Scheduled by the survey ranker.\n"
                + "\n".join(f"- {reason.detail}" for reason in entry.reasons)
            ),
            starts_at=starts_at,
            ends_at=starts_at + SURVEY_DURATION,
            attendees=(crew_email,),
        )
        created = await self._calendar.create_event(event, idempotency_key=key)
        return created.external_ref

    async def _notify(
        self,
        entry: SurveyQueueEntry,
        *,
        company: str,
        crew_email: str,
        profile: BuildingProfile,
        starts_at: datetime,
    ) -> str | None:
        key = self._ids.idempotency_key("crew-mail", entry.entry_id)
        open_conflicts = [c for c in profile.conflicts if c.status is ConflictStatus.OPEN]
        lines = [
            f"Company survey scheduled for {starts_at.date().isoformat()} at "
            f"{entry.address_id}.",
            "",
            "Why this structure surfaced:",
            *(f"  - {reason.detail}" for reason in entry.reasons),
        ]
        if open_conflicts:
            lines += [
                "",
                "Open disagreements to settle on site:",
                *(f"  - {c.summary}" for c in open_conflicts),
            ]
        lines += [
            "",
            "This notification records what is on file and what disagrees. "
            "It contains no tactical direction.",
        ]
        message = MailMessage(
            message_id=f"msg_{entry.entry_id}",
            to=(crew_email,),
            subject=f"Survey assignment {company}: {entry.address_id}",
            body="\n".join(lines),
        )
        sent = await self._mailer.send(message, idempotency_key=key)
        return sent.external_ref

    async def _write_plan(
        self, profile: BuildingProfile, *, now: datetime, compensations: list[str]
    ) -> tuple[PreIncidentPlan, Any]:
        # Stamped with the profile's last change, not with the wall clock. The
        # artifact describes one exact profile version, so it must be a pure
        # function of that version -- otherwise rewriting the same plan produces
        # different bytes and the store rejects it as a key collision.
        generated_at = profile.timeline[-1].occurred_at if profile.timeline else now
        plan = build_plan(profile, generated_at=generated_at)
        content = plan.to_bytes()
        # Keyed by profile version: the plan describes one exact version, and
        # re-writing the same version stores one artifact.
        key = self._ids.idempotency_key("preplan", profile.address_id, str(profile.profile_version))
        object_id = f"preplans/{profile.address_id}/v{profile.profile_version}.json"
        stored = await self._plan_store.put(
            object_id=object_id,
            content=content,
            content_type="application/json",
            idempotency_key=key,
        )
        compensations.append(
            await self._record_compensation(
                action_id=f"act_plan_{profile.address_id}_{profile.profile_version}",
                target=PLAN_TARGET,
                compensating_action="Delete the pre-incident plan artifact.",
                reason="Pre-incident plan written autonomously.",
                now=now,
            )
        )
        return plan, stored

    # ------------------------------------------------------------- referral

    async def _stage_referral(
        self,
        profile: BuildingProfile,
        *,
        entry: SurveyQueueEntry,
        now: datetime,
        correlation_id: str,
    ) -> tuple[str | None, str | None]:
        """Stage a referral for the worst open conflict, if there is one.

        Staged, never filed. Filing accuses a property owner of unpermitted
        construction, and that is a captain's decision.
        """
        open_conflicts = [c for c in profile.conflicts if c.status is ConflictStatus.OPEN]
        if not open_conflicts:
            return None, None
        conflict = max(open_conflicts, key=lambda c: (c.severity, c.conflict_id))

        referral_id = f"ref_{conflict.conflict_id.removeprefix('conflict_')}"
        existing = await self._referrals.get(referral_id)
        if existing is not None:
            return existing.referral_id, existing.action_id

        referral = ReferralRecord(
            referral_id=referral_id,
            address_id=profile.address_id,
            conflict_id=conflict.conflict_id,
            receiving_department=Department.BUILDING,
            supporting_fact_ids=conflict.fact_ids,
            narrative=self._referral_narrative(profile, conflict),
            status=ReferralStatus.AWAITING_APPROVAL,
            idempotency_key=self._ids.idempotency_key("referral", conflict.conflict_id),
            drafted_at=now,
        )
        await self._referrals.add(referral)

        approval = ApprovalRequest(
            approval_id=f"apr_{referral_id}",
            action_id=f"act_ref_{referral_id}",
            address_id=profile.address_id,
            threshold=ApprovalThreshold.SUPERVISOR,
            receiving_department=Department.BUILDING,
            prefilled_summary=(
                f"File a referral with the building department for "
                f"{profile.address_id}: {conflict.summary}"
            ),
            rule_id=conflict.rule_id,
            status=ApprovalStatus.STAGED,
            staged_at=now,
        )
        await self._approvals.stage(approval)

        logger.info(
            "referral_staged",
            extra={
                "address_id": profile.address_id,
                "conflict_id": conflict.conflict_id,
                "approval_id": approval.approval_id,
            },
        )
        return referral.referral_id, approval.approval_id

    @staticmethod
    def _referral_narrative(profile: BuildingProfile, conflict: Conflict) -> str:
        """Deterministic referral text.

        A model may polish this later; it may not author the facts. Every
        sentence here is a restatement of a stored record, and the fact ids are
        printed so the building department can pull the same documents.
        """
        return (
            f"The San Francisco Fire Department requests review of "
            f"{profile.address_id}.\n\n"
            f"Deterministic finding ({conflict.rule_id}, severity "
            f"{conflict.severity} of 5): {conflict.summary}\n\n"
            f"Supporting records: {', '.join(conflict.fact_ids)}.\n\n"
            "Both source records are retained and neither has been amended. "
            "This referral states a disagreement between filed and measured "
            "records; it makes no determination of code compliance."
        )

    async def approve_referral(
        self, referral_id: str, *, approved_by: str, correlation_id: str
    ) -> ApprovalResult:
        """File the referral a human just approved, and record the case number.

        Idempotent on the referral's own key: the receiving system dedupes, so
        approving twice returns the first case number rather than opening a
        second case against the same property.
        """
        referral = await self._referrals.get(referral_id)
        if referral is None:
            raise NotFoundError("referral not found", details={"referral_id": referral_id})

        approval = await self._approvals.get(f"apr_{referral_id}")
        if approval is None:
            raise NotFoundError("approval request not found", details={"referral_id": referral_id})

        if referral.status is ReferralStatus.FILED and referral.case_number:
            return ApprovalResult(
                referral_id=referral_id,
                approval_id=approval.approval_id,
                case_number=referral.case_number,
                replayed=True,
            )

        target = self._targets.get(REFERRAL_TARGET)
        if target is None:
            raise ValidationError(
                "the referral intake target is not configured",
                details={"target": REFERRAL_TARGET},
            )

        now = self._clock.now()
        body: dict[str, Any] = {
            "address_id": referral.address_id,
            "conflict_id": referral.conflict_id,
            "supporting_fact_ids": list(referral.supporting_fact_ids),
            "narrative": referral.narrative,
        }
        action = WriteAction(
            action_id=approval.action_id,
            agent_id=REFERRAL_AGENT_ID,
            agent_version=self._agent_version,
            target=REFERRAL_TARGET,
            receiving_department=Department.BUILDING,
            operation=Operation.WRITE,
            idempotency_key=referral.idempotency_key,
            payload_hash=request_hash(body),
            intent=f"File a building-department referral for {referral.address_id}.",
            compensating_action="Withdraw the referral.",
            status=WriteActionStatus.APPROVED,
            approval_id=approval.approval_id,
            address_id=referral.address_id,
            created_at=now,
        )
        await self._write_actions.record(action)
        receipt = await target.execute(action, body=body)
        await self._write_actions.save_receipt(receipt)

        await self._approvals.save(
            approval.model_copy(
                update={
                    "status": ApprovalStatus.GRANTED,
                    "decided_at": now,
                    "decided_by": approved_by,
                }
            )
        )
        filed = referral.model_copy(
            update={
                "status": ReferralStatus.FILED,
                "approved_by": approved_by,
                "filed_at": now,
                "case_number": receipt.external_ref,
                "action_id": action.action_id,
            }
        )
        await self._referrals.save(filed)
        await self._record_compensation(
            action_id=action.action_id,
            target=REFERRAL_TARGET,
            compensating_action=action.compensating_action,
            reason=f"Referral filed against {referral.address_id} after human approval.",
            now=now,
        )
        await self._audit_event(
            AuditEventKind.APPROVAL_GRANTED,
            actor=approved_by,
            target=REFERRAL_TARGET,
            address_id=referral.address_id,
            detail={
                "approval_id": approval.approval_id,
                "referral_id": referral_id,
                "threshold": str(approval.threshold),
                "rule_id": approval.rule_id,
            },
        )
        await self._audit_event(
            AuditEventKind.WRITE_REPLAYED if receipt.replayed else AuditEventKind.WRITE_EXECUTED,
            actor=REFERRAL_AGENT_ID,
            target=REFERRAL_TARGET,
            address_id=referral.address_id,
            detail={
                "action_id": action.action_id,
                # The case number the building department issued. Not a secret,
                # and the only handle either side can use to find the case.
                "external_ref": receipt.external_ref,
                "approval_id": approval.approval_id,
                "replayed": str(receipt.replayed),
            },
        )
        await self._write_back_case_number(filed, now=now)

        logger.info(
            "referral_filed",
            extra={
                "referral_id": referral_id,
                "case_number": receipt.external_ref,
                "replayed": receipt.replayed,
            },
        )
        return ApprovalResult(
            referral_id=referral_id,
            approval_id=approval.approval_id,
            case_number=receipt.external_ref,
            replayed=receipt.replayed,
        )

    async def _write_back_case_number(self, referral: ReferralRecord, *, now: datetime) -> None:
        """Record the returned case number on the profile timeline.

        The number the building department issued is the only evidence the
        referral exists on their side, so it lands on the profile rather than
        only in a receipt.
        """
        profile = await self._profiles.get(referral.address_id)
        if profile is None:
            return
        if any(r.referral_id == referral.referral_id for r in profile.open_referrals):
            return

        updated = profile.model_copy(
            update={"open_referrals": (*profile.open_referrals, referral)}
        ).append_event(
            ProfileEvent(
                event_id=f"pevt_ref_{referral.referral_id}",
                sequence=profile.next_sequence,
                occurred_at=now,
                type=ProfileEventType.REFERRAL_FILED,
                actor=REFERRAL_AGENT_ID,
                actor_version=self._agent_version,
                summary=(
                    f"Referral filed with the building department; case " f"{referral.case_number}"
                ),
                fact_ids=referral.supporting_fact_ids,
                conflict_id=referral.conflict_id,
            )
        )
        try:
            await self._profiles.save(updated, expected_version=profile.profile_version)
        except StaleVersionError:
            logger.info("case_number_write_contended", extra={"referral_id": referral.referral_id})

    # ------------------------------------------------------------ internals

    async def _audit_event(
        self,
        kind: AuditEventKind,
        *,
        actor: str,
        target: str,
        detail: dict[str, str],
        address_id: str | None = None,
        incident_id: str | None = None,
    ) -> None:
        """Record one immutable audit event.

        Detail is redacted by the sink on the way in, so an event that never
        held record contents cannot leak them later.
        """
        if self._audit is None:
            return
        await self._audit.record_event(
            AuditEvent(
                audit_id=self._ids.new_id("audit"),
                kind=kind,
                occurred_at=self._clock.now(),
                actor=actor,
                actor_version=self._agent_version,
                target=target,
                address_id=address_id,
                incident_id=incident_id,
                correlation_id=self._ids.new_id("corr"),
                detail=detail,
            )
        )

    async def _record_compensation(
        self,
        *,
        action_id: str,
        target: str,
        compensating_action: str,
        reason: str,
        now: datetime,
    ) -> str:
        """Record the undo before anything needs undoing.

        Created when the write executes, not when a failure happens: a system
        that only records the undo after something goes wrong has no undo for
        the failure that took the process with it.
        """
        compensation = CompensationRecord(
            compensation_id=f"comp_{action_id}",
            action_id=action_id,
            target=target,
            compensating_action=compensating_action,
            idempotency_key=self._ids.idempotency_key("compensate", action_id),
            reason=reason,
            recorded_at=now,
        )
        stored = await self._compensations.record(compensation)
        return stored.compensation_id
