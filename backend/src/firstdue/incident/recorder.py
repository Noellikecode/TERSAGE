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

**The synthesis, and the line it does not cross.** Given a model to write with
or a bank to remember with, the closing draft stops being a table of counts and
becomes a piece of reasoning over the whole record -- led by the head agent's
briefing, and closing the slow loop's open questions where a crew standing in
the building actually settled one. See :mod:`firstdue.agents.graphs.recorder`.
This agent has fifteen seconds and runs after the incident closes, with nothing
waiting on it, which is what makes that affordable here and nowhere else in the
incident loop.

With neither collaborator wired -- the default, and what ``make demo`` and the
whole test suite run -- none of it happens and the draft is the one this agent
has always produced.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.agents.graphs.base import (
    DEFAULT_MAX_STEPS,
    GraphCassette,
    ReasoningPlanner,
    graph_budget,
    run_graph,
)
from firstdue.agents.graphs.recorder import (
    NERIS_DRAFT_MAX_CHARS,
    NerisGraphState,
    NerisSynthesis,
)
from firstdue.domain.briefs import BriefEmission
from firstdue.domain.enums import BenchmarkType, LogEntryType, Operation, Scope, WriteActionStatus
from firstdue.domain.incidents import Benchmark, Incident
from firstdue.domain.logentries import AppendOnlyLog, IncidentLogEntry
from firstdue.domain.policy import PolicyDecision
from firstdue.domain.work import WriteAction
from firstdue.errors import (
    AppendOnlyViolationError,
    SourceUnavailableError,
    StaleVersionError,
)
from firstdue.extraction.recorded import request_digest
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditEvent, AuditEventKind, AuditSink
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.model import ModelClient
from firstdue.ports.repositories import IncidentLogRepository, WriteActionRepository
from firstdue.ports.writes import ExternalWriteTarget
from firstdue.registry.descriptors import descriptor_for
from firstdue.services.memory_bank import MemoryBank

logger = get_logger(__name__)

AGENT_ID: Final[str] = "incident-recorder"
RMS_TARGET: Final[str] = "department-rms"

#: How many times an append re-reads the counter and takes the next sequence.
#:
#: The log's sequence is decided *outside* the transaction that commits it --
#: :meth:`IncidentLogRepository.next_sequence` is a read, and the entry carries
#: the number it read -- so two agents writing at the same instant both claim
#: the same one and the loser is refused. That is the right refusal for the
#: repository to make: nothing may take a sequence that is already taken.
#:
#: It is the wrong outcome *here*. Three agency notifications going out
#: together is the ordinary shape of an incident, and on a live run three of
#: them died with ``APPEND_ONLY_VIOLATION`` -- work that had happened, refused
#: a line in the record because two writes landed in the same millisecond. So
#: the loser re-reads the counter and takes the next number, which is what it
#: would have done had it arrived a moment later.
#:
#: The same applies to the transaction underneath it: the Firestore backend
#: commits the counter and the entry together, and a document three agents are
#: appending to at once is a document whose transaction can exhaust its own
#: attempts. That surfaced on the same live run as a composing run dying with
#: ``STALE_VERSION`` and staging no entry package.
#:
#: Bounded, and small. Each attempt is a fresh read of a counter that only
#: moves forward, so a writer that loses five times in a row is contending with
#: something this loop should not be quietly absorbing.
MAX_APPEND_ATTEMPTS: Final[int] = 5

#: NERIS is the national incident reporting standard replacing NFIRS. What this
#: produces is a *draft*: the fields the system observed, for a human to
#: complete and file. Nothing here files a report.
NERIS_DRAFT_VERSION: Final[str] = "neris-draft/1"

#: Printed on the artifact, and required to survive any polish. A report that
#: lost this sentence would read as a filing, which is the one thing it is not.
NERIS_DISCLAIMER: Final[str] = (
    "Draft assembled from the incident log. Not a filed report; a human completes and files it."
)


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
    #: Narratives read, and agents woken off them. Counted separately from the
    #: brief versions because an intake that was never read still produces an
    #: entry, and a draft that only counted successful reads would round that
    #: to zero.
    intake_reads: int = Field(default=0, ge=0)
    agent_handoffs: int = Field(default=0, ge=0)
    log_entries: int = Field(default=0, ge=0)
    log_sealed_at: datetime | None = None
    #: Stated on the artifact: a draft for a human to complete, not a filing.
    disclaimer: str = NERIS_DISCLAIMER

    # ---- the synthesis graph. Empty or zero on a draft that did not run one,
    # and a draft that did not run one is the default; see
    # ``IncidentRecorder.reasons``.
    #: The report prose. Empty when no synthesis ran, which is what keeps the
    #: draft byte-identical to the one this agent has always produced.
    narrative: str = Field(default="", max_length=NERIS_DRAFT_MAX_CHARS)
    #: ``deterministic`` or ``model``. Which one shipped is the interesting
    #: fact for a reviewer comparing two reports, not the prose itself.
    narrative_source: str = Field(default="", max_length=20)
    #: Why a composed draft was refused, when one was. A stable code.
    narrative_rejection: str = Field(default="", max_length=60)
    #: Refs the head agent judged material, highest priority first. Ids and
    #: canonical keys -- what the report leads with, never what it asserts.
    leading_refs: tuple[str, ...] = ()
    #: Threads the slow loop opened that this incident closed, and threads it
    #: examined and deliberately left open. Both are outcomes worth reporting:
    #: an unresolved question is a correct state, and a silent one is not.
    questions_resolved: tuple[str, ...] = ()
    questions_left_open: tuple[str, ...] = ()
    #: Why the synthesis stopped, and how many nodes it took to get there.
    graph_stop: str = Field(default="", max_length=40)
    graph_steps: int = Field(default=0, ge=0)


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
        memory: MemoryBank | None = None,
        memory_scopes: Collection[Scope] | None = None,
        model: ModelClient | None = None,
        planner: ReasoningPlanner | None = None,
        traces: GraphCassette | None = None,
        use_langgraph: bool = True,
        max_graph_steps: int = DEFAULT_MAX_STEPS,
        agent_version: str = "1.0.0",
    ) -> None:
        self._log = incident_log
        self._write_actions = write_actions
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._rms = rms
        # Optional, exactly like ``rms`` above and like the collaborators on the
        # slow-loop watchers. With neither a bank nor a model wired this agent
        # produces the draft it has always produced, byte for byte. The
        # synthesis is something a deployment opts into by giving the recorder
        # somewhere to remember and something to write with.
        self._memory = memory
        # The bank gates recall on a memory's statutory class, and the class of
        # memory this agent may read is a property of the *catalog* rather than
        # of this file: reading the descriptor means a recorder cannot close a
        # thread it would not have been allowed to be shown. Overridable so a
        # deployment can hand it the incident grant's scopes instead.
        self._memory_scopes = frozenset(
            memory_scopes if memory_scopes is not None else descriptor_for(AGENT_ID).required_scopes
        )
        self._model = model
        self._planner = planner
        self._traces = traces
        self._use_langgraph = use_langgraph
        self._max_graph_steps = max_graph_steps
        self._agent_version = agent_version

    @property
    def reasons(self) -> bool:
        """Whether this instance runs the synthesis graph at all.

        One predicate, read here and by nothing else, so "does this deployment
        reason" has a single answer rather than two conditions that can drift.
        """
        return self._memory is not None or self._model is not None

    # ------------------------------------------------- persist before transmit

    async def record_emission(self, emission: BriefEmission) -> BriefEmission:
        """Write a brief emission to the log and return it marked persisted.

        The returned emission is the only thing a transport may send, because
        an unpersisted one raises at :meth:`BriefEmission.require_persisted`.
        The order here is the whole contract: append first, mark second, return
        third.
        """
        sealed = emission if emission.content_hash else emission.sealed()
        stored = await self._commit(
            emission.incident_id,
            lambda sequence: IncidentLogEntry(
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
            ),
        )
        # A brief is the interceptor's work and it does not come through
        # `_append`, so without this the one agent producing a brief every few
        # seconds had a single `grant_minted` to its name for the whole
        # incident. Attributed to the emission's own agents, which is who
        # composed it.
        for agent_id, agent_version in (
            sealed.agent_versions or {AGENT_ID: self._agent_version}
        ).items():
            await self._record_step(stored, actor=agent_id, actor_version=agent_version)
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
            # The notifier's work, written down by the recorder. Both names go
            # on the entry: without the actor, every notification this agent
            # sent was filed under whoever wrote the log and `agency-notifier`
            # read as an agent that had done nothing.
            actor="agency-notifier",
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

    async def record_intake(
        self,
        incident_id: str,
        *,
        channel: str,
        source_ref: str,
        accepted: bool,
        reported_keys: Sequence[str],
        unknowns: Sequence[str],
        model_ref: str,
        screen: str,
        screen_findings: Sequence[str],
        dropped_values: int,
        rejection_reason: str | None,
    ) -> IncidentLogEntry:
        """What the 911 or CAD narrative was read as.

        Attribute names and outcomes only. The transcript itself is a citizen's
        words on a recorded emergency line, and the incident log is a
        department record that is written through to the records system -- a
        copy of the call in it would be a second, less governed home for the
        same content.

        ``accepted=False`` is recorded as loudly as a successful read, because
        "the intake was never read" is exactly the thing an investigation needs
        to be able to see and exactly the thing an absent log entry hides.
        """
        return await self._append(
            incident_id,
            LogEntryType.INTAKE_READ,
            content={
                "channel": channel,
                "source_ref": source_ref,
                "accepted": accepted,
                "reported_keys": list(reported_keys),
                "unknowns": list(unknowns),
                "model_ref": model_ref,
                "screen": screen,
                "screen_findings": list(screen_findings),
                "dropped_values": dropped_values,
                "rejection_reason": (rejection_reason or "")[:300],
            },
        )

    async def record_handoff(
        self,
        incident_id: str,
        *,
        agent_ref: str,
        rule_ids: Sequence[str],
        intake_keys: Sequence[str],
        note: str,
        started: bool,
        missing_scopes: Sequence[str] = (),
    ) -> IncidentLogEntry:
        """Which agent was woken, under which rule, with what.

        The rule ids are the point. "Who was told" is answerable from a list of
        agent names, but "why were they told, and why was nobody else" is only
        answerable if the rule that selected them is in the record next to them.
        """
        return await self._append(
            incident_id,
            LogEntryType.AGENT_HANDOFF,
            content={
                "agent_ref": agent_ref,
                "rule_ids": list(rule_ids),
                "intake_keys": list(intake_keys),
                "note": note[:500],
                "started": started,
                # Present when a rule selected this agent and the incident grant
                # could not cover it. Named, because "nobody told the recorder"
                # is only answerable if the reason is beside the fact.
                "missing_scopes": list(missing_scopes),
            },
        )

    async def record_entry_package(
        self,
        content: Mapping[str, Any],
        *,
        incident_id: str,
        agent_id: str,
        agent_version: str = "1.0.0",
    ) -> IncidentLogEntry:
        """One state of one entry package: staged, half-approved, or sent.

        The package has no collection of its own and this is why. The log is
        already append-only, gapless, sealable and written through to the
        records system, and "what was the crew handed, who signed each half of
        it, and when" is the question it exists to answer. Every state change
        appends rather than edits, so the approval history *is* the entry
        sequence and a later version cannot quietly replace an earlier one.

        The content comes from
        :func:`~firstdue.incident.packages.package_content`, which is the only
        thing that builds one -- a package that reached the log by another route
        would be a document nobody could validate on the way back out.
        """
        return await self._append(
            incident_id,
            LogEntryType.ENTRY_PACKAGE,
            actor=agent_id,
            actor_version=agent_version,
            content=content,
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
        self,
        incident_id: str,
        entry_type: LogEntryType,
        *,
        content: Mapping[str, Any],
        actor: str | None = None,
        actor_version: str = "1.0.0",
    ) -> IncidentLogEntry:
        """Append one entry.

        ``actor`` names the agent whose *work* this records, when that is not
        the recorder. Both end up in ``agent_versions``, because both were
        involved and the record should say so: this recorder wrote the entry,
        and some other agent did the thing it describes. Without the
        distinction every entry in the log was attributed to whoever wrote it,
        which made agents that never write their own entries -- `sensor-fusion`
        registering building faces -- indistinguishable from agents that did
        nothing at all.
        """
        versions = {AGENT_ID: self._agent_version}
        if actor and actor != AGENT_ID:
            versions[actor] = actor_version
        entry = await self._commit(
            incident_id,
            lambda sequence: IncidentLogEntry(
                entry_id=self._ids.new_id("entry"),
                incident_id=incident_id,
                sequence=sequence,
                entry_type=entry_type,
                occurred_at=self._clock.now(),
                profile_snapshot_id=content.get("profile_snapshot_id", "") or "pending",
                agent_versions=versions,
                content=dict(content),
            ),
        )
        await self._record_step(entry, actor=actor, actor_version=actor_version)
        return entry

    async def _commit(
        self,
        incident_id: str,
        build: Callable[[int], IncidentLogEntry],
    ) -> IncidentLogEntry:
        """Append one entry, taking the next free sequence.

        ``build`` is called once per attempt with the sequence to claim, rather
        than being handed a finished entry to renumber: the content hash covers
        the sequence, so an entry rebuilt at a new number has to be *built* at
        it. It also means the timestamp is the instant the entry was actually
        committed rather than the instant the first attempt was made.

        Two lost races are retried and nothing else.

        The first is the sequence: somebody committed the number this attempt
        read. The second is the transaction itself -- the Firestore backend
        writes the counter and the entry together, and a document several
        agents are appending to at once is a document whose transaction can
        exhaust its own attempts and come back as a
        :class:`~firstdue.errors.WriteContentionError`. Both mean the entry did
        not land and nothing committed under its id, so building it again at a
        fresh number is the same write, made a moment later.

        A sealed log is neither: it carries no ``expected`` in its details and
        is re-raised untouched, because an entry arriving after the incident
        closed is a real violation and retrying it would be a writer hammering
        a closed record. See :data:`MAX_APPEND_ATTEMPTS`.
        """
        last: Exception | None = None
        for attempt in range(MAX_APPEND_ATTEMPTS):
            sequence = await self._log.next_sequence(incident_id)
            try:
                return await self._log.append(build(sequence))
            except AppendOnlyViolationError as exc:
                if "expected" not in exc.details:
                    raise
                last = exc
            except StaleVersionError as exc:
                last = exc
            logger.info(
                "log_append_retried",
                extra={
                    "incident_id": incident_id,
                    "attempted_sequence": sequence,
                    "attempt": attempt + 1,
                    "error_type": type(last).__name__,
                },
            )
        raise (
            last
            if last is not None
            else AppendOnlyViolationError(  # pragma: no cover
                "the incident log could not be appended to",
                details={"incident_id": incident_id},
            )
        )

    async def _record_step(
        self,
        entry: IncidentLogEntry,
        *,
        actor: str | None,
        actor_version: str,
    ) -> None:
        """Say in the audit log that this happened, under the name that did it.

        The incident log and the audit log answer different questions and are
        read by different things. The incident log is the department's record of
        *the fire*; the audit log is the record of *the fleet*, and it is the
        only evidence the console has for whether an agent is working.

        Every entry above passed through here and none of them landed in the
        audit log, so two agents that do their whole job through this recorder
        -- `sensor-fusion` registering building faces, and the recorder itself
        writing the record -- had no audit trail at all and the console drew
        both as idle for the length of an incident they were busy through. An
        agent that works and leaves no trace is indistinguishable from one that
        did not run.

        Attributed exactly as the entry is: the acting agent where the entry
        names one, the recorder where it does not. Crediting the recorder for
        another agent's analysis is the same mistake in the other direction.
        """
        acting = actor or AGENT_ID
        await self._audit.record_event(
            AuditEvent(
                audit_id=self._ids.new_id("audit"),
                kind=AuditEventKind.AGENT_STEP,
                occurred_at=entry.occurred_at,
                actor=acting,
                actor_version=actor_version if acting != AGENT_ID else self._agent_version,
                target=entry.incident_id,
                # Set, so this step can be counted against *this* fire.
                #
                # Without it the console could only ever total an agent's whole
                # session, and the incident agents' counters opened at whatever
                # the last few incidents had left behind -- "45 recorded" before
                # this one had done anything. It is also the field the Firestore
                # sink promotes to a queryable column, so a per-incident read is
                # a filter rather than a scan.
                incident_id=entry.incident_id,
                correlation_id=entry.incident_id,
                # The entry type and where it sits in the log -- enough to find
                # the entry itself, and nothing copied out of it. The content is
                # in the incident log with its provenance; a second uncited copy
                # here would be a claim nobody could check.
                detail={
                    "entry": entry.entry_type.value,
                    "sequence": str(entry.sequence),
                },
            )
        )

    async def record_analysis(
        self,
        incident_id: str,
        *,
        agent_id: str,
        agent_version: str = "1.0.0",
        headline: str,
        detail: str = "",
        refs: Sequence[str] = (),
    ) -> IncidentLogEntry:
        """What one agent concluded, in its own name.

        The entry the per-agent cards are built from. ``headline`` is one line
        an officer reads; ``refs`` are ids and canonical keys, never values --
        the same rule the focus keeps, for the same reason: a summary that
        carried a measurement would be a second copy of a fact with no source,
        no confidence and no span behind it.
        """
        return await self._append(
            incident_id,
            LogEntryType.AGENT_ANALYSIS,
            actor=agent_id,
            actor_version=agent_version,
            content={
                "agent_ref": f"{agent_id}@{agent_version}",
                "headline": headline[:200],
                "detail": detail[:300],
                "refs": list(refs)[:12],
            },
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

    async def neris_draft(
        self, incident: Incident, *, deadline: datetime | None = None
    ) -> NerisDraft:
        """Assemble the draft report from what was recorded, and close what it settled.

        The counted draft is the floor and is produced first, unconditionally.
        A deployment that wired a bank or a model then runs the synthesis graph
        over the same record: it leads the report with what the head agent
        judged material, and it closes the slow-loop questions this incident
        actually answered.

        ``deadline`` is the caller's, and the tighter of it and the descriptor's
        own fifteen seconds bounds the graph. It is optional because nothing
        waits on this call; what passing it buys is a synthesis that stops
        cleanly with a plain report rather than one killed mid-sentence.
        """
        draft = await self._counted_draft(incident)
        if not self.reasons:
            return draft
        return await self._synthesise(incident, draft, deadline=deadline)

    async def _counted_draft(self, incident: Incident) -> NerisDraft:
        """The draft this agent has always produced. The floor and the fallback.

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
            intake_reads=by_type.get(LogEntryType.INTAKE_READ, 0),
            agent_handoffs=by_type.get(LogEntryType.AGENT_HANDOFF, 0),
            log_entries=len(log.entries),
            log_sealed_at=log.sealed_at,
        )

    # ----------------------------------------------------------- the synthesis

    async def _synthesise(
        self, incident: Incident, draft: NerisDraft, *, deadline: datetime | None
    ) -> NerisDraft:
        """Run the synthesis over the same record, and fold what it produced in.

        The counted draft goes in and a copy of it comes out. That is the shape
        deliberately: the graph writes the report's *prose* and closes questions
        in the bank, and it never touches a count. A synthesis that could alter
        ``observed_facts`` would be a model amending the log's own arithmetic.
        """
        budget = graph_budget(
            AGENT_ID,
            deadline=deadline,
            started=self._clock.now(),
            max_steps=self._max_graph_steps,
        )
        synthesis = NerisSynthesis(
            incident=incident,
            log=self._log,
            budget=budget,
            disclaimer=NERIS_DISCLAIMER,
            memory=self._memory,
            memory_scopes=self._memory_scopes,
            model=self._model,
            planner=self._planner,
            agent_version=self._agent_version,
        )
        digest = request_digest("neris-synthesis", incident.incident_id, str(draft.log_entries))
        run = await run_graph(
            synthesis.spec(),
            NerisGraphState(
                district_id=incident.district_id,
                incident_id=incident.incident_id,
                address_id=incident.address_id,
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

        state = run.state
        left_open = tuple(
            question_id
            for question_id in state.examined_questions
            if question_id not in state.resolved_questions
        )
        return draft.model_copy(
            update={
                "narrative": state.narrative,
                "narrative_source": state.narrative_source,
                "narrative_rejection": state.draft_rejection,
                "leading_refs": tuple(lead.ref for lead in state.leads),
                "questions_resolved": state.resolved_questions,
                "questions_left_open": left_open,
                "graph_stop": str(run.trace.stop),
                "graph_steps": len(run.trace.records),
            }
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
