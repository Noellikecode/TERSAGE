"""The Incident Record Agent: nothing reaches the commander that is not recorded.

**Persist before transmit.** Every brief emission is written to the append-only
incident log, with its content hash, *before* the SSE frame carrying it can be
acknowledged or displayed. :meth:`BriefEmission.require_persisted` is the gate,
and it raises otherwise -- so the ordering is enforced by the type rather than by
the transport remembering to await something first.

That ordering is what makes the log answer the question it exists for. If frames
could be displayed before they were recorded, then after a bad outcome the log
would be a record of what we *meant* to show, and the difference between that and
what a commander actually saw is the whole investigation.

**The records system is not on the critical path.** RMS writes are buffered.
When the records system is unreachable the entries queue, the incident proceeds,
and a recovery flush drains them afterwards. An incident blocked by a logging
failure is a worse failure than the logging one.

**Sealed on close.** The log is sealed at incident close, and a sealed log
accepts nothing further.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.briefs import BriefEmission
from firstdue.domain.enums import BenchmarkType, LogEntryType, Operation, WriteActionStatus
from firstdue.domain.incidents import Benchmark, Incident
from firstdue.domain.logentries import AppendOnlyLog, IncidentLogEntry
from firstdue.domain.policy import PolicyDecision
from firstdue.domain.work import WriteAction
from firstdue.errors import SourceUnavailableError
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditEvent, AuditEventKind, AuditSink
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.repositories import IncidentLogRepository, WriteActionRepository
from firstdue.ports.writes import ExternalWriteTarget

logger = get_logger(__name__)

AGENT_ID: Final[str] = "incident-recorder"
RMS_TARGET: Final[str] = "department-rms"

#: NERIS is the national incident reporting standard replacing NFIRS. What this
#: produces is a *draft*: the fields the system observed, for a human to
#: complete and file. Nothing here files a report.
NERIS_DRAFT_VERSION: Final[str] = "neris-draft/1"


class FlushResult(BaseModel):
    """What a recovery flush managed to drain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempted: int = Field(default=0, ge=0)
    flushed: int = Field(default=0, ge=0)
    still_buffered: int = Field(default=0, ge=0)
    unavailable_reason: str | None = Field(default=None, max_length=200)

    @property
    def complete(self) -> bool:
        return self.still_buffered == 0 and self.unavailable_reason is None


class NerisDraft(BaseModel):
    """A draft incident report, assembled from what was actually recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft_version: str = NERIS_DRAFT_VERSION
    incident_id: str
    cad_ref: str
    address_id: str
    alarm_level: int
    dispatched_at: datetime
    closed_at: datetime | None = None
    #: Timestamps of things that happened. Clerical, never tactical.
    benchmarks: tuple[dict[str, str], ...] = ()
    brief_versions: int = Field(default=0, ge=0)
    ic_resolutions: int = Field(default=0, ge=0)
    notifications: int = Field(default=0, ge=0)
    approvals: int = Field(default=0, ge=0)
    policy_decisions: int = Field(default=0, ge=0)
    observed_facts: int = Field(default=0, ge=0)
    log_entries: int = Field(default=0, ge=0)
    log_sealed_at: datetime | None = None
    #: Stated on the artifact: a draft for a human to complete, not a filing.
    disclaimer: str = (
        "Draft assembled from the incident log. Not a filed report; a human "
        "completes and files it."
    )


class IncidentRecorder:
    """Writes the incident's immutable record, and flushes it to RMS later."""

    def __init__(
        self,
        *,
        incident_log: IncidentLogRepository,
        write_actions: WriteActionRepository,
        audit: AuditSink,
        clock: Clock,
        ids: IdGenerator,
        rms: ExternalWriteTarget | None = None,
        agent_version: str = "1.0.0",
    ) -> None:
        self._log = incident_log
        self._write_actions = write_actions
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._rms = rms
        self._agent_version = agent_version

    # ------------------------------------------------- persist before transmit

    async def record_emission(self, emission: BriefEmission) -> BriefEmission:
        """Write a brief emission to the log and return it marked persisted.

        The returned emission is the only thing a transport may send, because
        an unpersisted one raises at :meth:`BriefEmission.require_persisted`.
        The order here is the whole contract: append first, mark second, return
        third.
        """
        sealed = emission if emission.content_hash else emission.sealed()
        sequence = await self._log.next_sequence(emission.incident_id)
        entry = IncidentLogEntry(
            entry_id=self._ids.new_id("entry"),
            incident_id=emission.incident_id,
            sequence=sequence,
            entry_type=LogEntryType.BRIEF_EMITTED,
            occurred_at=self._clock.now(),
            profile_snapshot_id=sealed.profile_snapshot_id,
            agent_versions=dict(sealed.agent_versions),
            content={
                "emission_id": sealed.emission_id,
                "version": sealed.version,
                "stage": str(sealed.stage),
                "content_hash": sealed.content_hash,
                "unknown_count": len(sealed.unknowns),
                "unavailable": list(sealed.unavailable),
                "narrative_available": sealed.narrative_available,
                "model_invoked": sealed.model_invoked,
            },
        )
        stored = await self._log.append(entry)
        logger.info(
            "brief_persisted",
            extra={
                "incident_id": emission.incident_id,
                "version": sealed.version,
                "sequence": stored.sequence,
            },
        )
        return sealed.mark_persisted(at=stored.occurred_at)

    # ----------------------------------------------------- everything else

    async def record_benchmark(self, benchmark: Benchmark) -> IncidentLogEntry:
        """A timestamp of something that happened. Clerical, never tactical."""
        return await self._append(
            benchmark.incident_id,
            LogEntryType.BENCHMARK,
            content={
                "benchmark_id": benchmark.benchmark_id,
                "type": str(benchmark.type),
                "occurred_at": benchmark.occurred_at.isoformat(),
                "recorded_by": benchmark.recorded_by,
            },
        )

    async def record_resolution(
        self, incident_id: str, *, conflict_id: str, resolved_by: str, note: str, fact_id: str
    ) -> IncidentLogEntry:
        """An IC settled a disagreement on scene."""
        return await self._append(
            incident_id,
            LogEntryType.IC_RESOLUTION,
            content={
                "conflict_id": conflict_id,
                "resolved_by": resolved_by,
                "resolving_fact_id": fact_id,
                "note": note[:500],
            },
        )

    async def record_notification(
        self, incident_id: str, *, target: str, external_ref: str, autonomous: bool
    ) -> IncidentLogEntry:
        return await self._append(
            incident_id,
            LogEntryType.NOTIFICATION_SENT,
            content={
                "target": target,
                "external_ref": external_ref,
                # Telling an agency is autonomous; committing them is not.
                "autonomous": autonomous,
            },
        )

    async def record_approval(
        self, incident_id: str, *, approval_id: str, decided_by: str, threshold: str
    ) -> IncidentLogEntry:
        return await self._append(
            incident_id,
            LogEntryType.APPROVAL_GRANTED,
            content={
                "approval_id": approval_id,
                "decided_by": decided_by,
                "threshold": threshold,
            },
        )

    async def record_decision(self, decision: PolicyDecision) -> IncidentLogEntry:
        """A gateway decision, in the incident's own record as well as the audit log."""
        await self._audit.record_decision(decision)
        return await self._append(
            decision.incident_id or "",
            LogEntryType.POLICY_DECISION,
            content={
                "decision_id": decision.decision_id,
                "action": str(decision.action),
                "rule_id": decision.rule_id,
                "policy_version": decision.policy_version,
                "target": decision.target,
                "decided_by": decision.decided_by,
            },
        )

    async def record_observed_fact(
        self, incident_id: str, *, fact_id: str, canonical_key: str, source: str
    ) -> IncidentLogEntry:
        return await self._append(
            incident_id,
            LogEntryType.FACT_OBSERVED,
            content={"fact_id": fact_id, "canonical_key": canonical_key, "source": source},
        )

    async def _append(
        self, incident_id: str, entry_type: LogEntryType, *, content: Mapping[str, Any]
    ) -> IncidentLogEntry:
        sequence = await self._log.next_sequence(incident_id)
        return await self._log.append(
            IncidentLogEntry(
                entry_id=self._ids.new_id("entry"),
                incident_id=incident_id,
                sequence=sequence,
                entry_type=entry_type,
                occurred_at=self._clock.now(),
                profile_snapshot_id=content.get("profile_snapshot_id", "") or "pending",
                agent_versions={AGENT_ID: self._agent_version},
                content=dict(content),
            )
        )

    # --------------------------------------------------------- buffered RMS

    async def flush_to_rms(self, *, incident_id: str | None = None) -> FlushResult:
        """Drain buffered entries into the records system.

        Called after the incident and on recovery. A records system that is
        still down leaves the entries buffered and says so -- it does not drop
        them, and it does not block anything.
        """
        pending = [
            entry
            for entry in await self._log.list_unflushed()
            if incident_id is None or entry.incident_id == incident_id
        ]
        if not pending:
            return FlushResult()
        if self._rms is None:
            return FlushResult(
                attempted=len(pending),
                still_buffered=len(pending),
                unavailable_reason="NO_RMS_TARGET",
            )

        flushed = 0
        for entry in pending:
            action = WriteAction(
                action_id=f"act_rms_{entry.incident_id}_{entry.sequence}",
                agent_id=AGENT_ID,
                agent_version=self._agent_version,
                target=RMS_TARGET,
                receiving_department=self._rms.receiving_department,
                operation=Operation.WRITE,
                # Derived from the entry's own identity, so a retried flush
                # cannot write the same log line twice.
                idempotency_key=self._ids.idempotency_key(
                    "rms", entry.incident_id, str(entry.sequence)
                ),
                payload_hash=entry.content_hash or entry.compute_content_hash(),
                intent=f"Write incident log entry {entry.sequence} to the records system.",
                compensating_action="Retract the records-system entry.",
                status=WriteActionStatus.DRAFTED,
                incident_id=entry.incident_id,
                created_at=self._clock.now(),
            )
            try:
                receipt = await self._rms.execute(
                    action,
                    body={
                        "incident_id": entry.incident_id,
                        "sequence": entry.sequence,
                        "entry_type": str(entry.entry_type),
                        "content_hash": entry.content_hash,
                    },
                )
            except SourceUnavailableError as exc:
                logger.warning(
                    "rms_flush_deferred",
                    extra={"incident_id": entry.incident_id, "error_code": str(exc.code)},
                )
                return FlushResult(
                    attempted=len(pending),
                    flushed=flushed,
                    still_buffered=len(pending) - flushed,
                    unavailable_reason=str(exc.code),
                )
            await self._write_actions.record(action)
            await self._write_actions.save_receipt(receipt)
            await self._log.mark_written_to_rms(
                entry.incident_id, entry.entry_id, at=self._clock.now()
            )
            flushed += 1

        await self._audit.record_event(
            AuditEvent(
                audit_id=self._ids.new_id("audit"),
                kind=AuditEventKind.RMS_FLUSHED,
                occurred_at=self._clock.now(),
                actor=AGENT_ID,
                actor_version=self._agent_version,
                target=RMS_TARGET,
                incident_id=incident_id,
                correlation_id=self._ids.new_id("corr"),
                detail={"flushed": str(flushed), "attempted": str(len(pending))},
            )
        )
        return FlushResult(attempted=len(pending), flushed=flushed, still_buffered=0)

    # ------------------------------------------------------------- closing

    async def seal(self, incident_id: str, *, at: datetime) -> AppendOnlyLog:
        """Seal the log. Nothing is appended after this."""
        sealed = await self._log.seal(incident_id, at=at)
        logger.info(
            "incident_log_sealed",
            extra={"incident_id": incident_id, "entries": len(sealed.entries)},
        )
        return sealed

    async def neris_draft(self, incident: Incident) -> NerisDraft:
        """Assemble the draft report from what was recorded.

        Counts rather than contents: the draft says how many brief versions,
        resolutions, notifications, and decisions there were, and the log itself
        holds each one. A human completes and files it.
        """
        log = await self._log.get_log(incident.incident_id)
        by_type: dict[LogEntryType, int] = {}
        for entry in log.entries:
            by_type[entry.entry_type] = by_type.get(entry.entry_type, 0) + 1

        return NerisDraft(
            incident_id=incident.incident_id,
            cad_ref=incident.cad_ref,
            address_id=incident.address_id,
            alarm_level=incident.alarm_level,
            dispatched_at=incident.dispatched_at,
            closed_at=incident.closed_at,
            benchmarks=tuple(
                {"type": str(b.type), "occurred_at": b.occurred_at.isoformat()}
                for b in incident.benchmarks
            ),
            brief_versions=by_type.get(LogEntryType.BRIEF_EMITTED, 0),
            ic_resolutions=by_type.get(LogEntryType.IC_RESOLUTION, 0),
            notifications=by_type.get(LogEntryType.NOTIFICATION_SENT, 0),
            approvals=by_type.get(LogEntryType.APPROVAL_GRANTED, 0),
            policy_decisions=by_type.get(LogEntryType.POLICY_DECISION, 0),
            observed_facts=by_type.get(LogEntryType.FACT_OBSERVED, 0),
            log_entries=len(log.entries),
            log_sealed_at=log.sealed_at,
        )


def benchmark(
    *,
    incident_id: str,
    benchmark_type: BenchmarkType,
    at: datetime,
    recorded_by: str,
    ids: IdGenerator,
) -> Benchmark:
    """Build a benchmark record. A timestamp, and who recorded it."""
    return Benchmark(
        benchmark_id=ids.new_id("bench"),
        incident_id=incident_id,
        type=benchmark_type,
        occurred_at=at,
        recorded_by=recorded_by,
    )


def entry_order(entries: Sequence[IncidentLogEntry]) -> list[int]:
    """The sequence numbers, in order. Used by replay and by the SSE resume."""
    return [entry.sequence for entry in entries]
