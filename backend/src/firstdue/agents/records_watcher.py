"""Records Watcher -- filings become facts.

Polls the permit, assessor, inspection, and violation feeds for a district,
resolves each record to a building through the city adapter, extracts
provenanced facts, and appends them to the profile. Then it materializes: the
deterministic conflict engine runs, and any disagreement is recorded.

Everything about it is idempotent by construction rather than by flag. Fact ids
are derived from the observation's natural key, conflict ids from the rule and
the facts, so a second poll of an unchanged source re-derives the same ids and
writes nothing. That is what makes "run the demo twice, get no duplicates" a
property of the arithmetic instead of a check somebody has to remember.

A source that is down does not stop the pass. Its records are missing from this
poll and the profile says the source was unavailable -- never that the hazard
was absent.

**Retrieval, when there is somewhere to remember.** Given a
:class:`~firstdue.services.memory_bank.MemoryBank` or a
:class:`~firstdue.ports.grounding.GroundingService`, the fixed four-feed pass
becomes agentic retrieval: which feed to read next, the references the filings
themselves cite, and a chase for the cited record -- looping until nothing is
outstanding. A filing nobody has published yet becomes an open question naming
the permit number and the feeds already searched, so the next pass waits instead
of re-failing. See :mod:`firstdue.agents.graphs.records`.

**The model may not author a fact.** The graph decides *what to look up and when
the picture has closed*, and that is all. What it produces is a set of
:class:`~firstdue.ports.sources.SourceSnapshot` objects; they go to the same
:class:`~firstdue.extraction.extractor.FactExtractor`, behind the same screen,
and every fact that comes out carries the same character span, snapshot id, and
provenance it always did. No node holds a
:class:`~firstdue.domain.facts.StructuralFact`, so none can invent one -- and a
value an officer cannot trace to a line in a filed document would not be a fact
at all.

With neither collaborator wired -- the default, and what ``make demo`` and the
whole test suite run -- none of that happens and this agent behaves exactly as
it always has.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.agents.graphs.base import (
    DEFAULT_MAX_STEPS,
    GROUNDING_DEADLINE_MS,
    GraphCassette,
    GraphStop,
    ReasoningPlanner,
    graph_budget,
    park,
    run_graph,
)
from firstdue.agents.graphs.records import RecordsGraphState, RecordsRetrieval
from firstdue.domain.conflicts import ConflictStatus
from firstdue.domain.enums import Classification, SourceType
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import Keys
from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType
from firstdue.domain.vectors import VectorPayload
from firstdue.errors import (
    AppendOnlyViolationError,
    ClassificationViolationError,
    SourceUnavailableError,
    StaleVersionError,
)
from firstdue.extraction.extractor import FactExtractor
from firstdue.extraction.recorded import request_digest
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditEvent, AuditEventKind, AuditSink
from firstdue.ports.city import CityAdapter
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.grounding import GroundingService
from firstdue.ports.repositories import FactRepository, ProfileRepository
from firstdue.ports.sources import SourceAdapter, SourceRecord, SourceSnapshot
from firstdue.ports.vectors import VectorIndex
from firstdue.registry.descriptors import descriptor_for
from firstdue.reliability.budget import budget_seconds
from firstdue.services.materialization import ProfileMaterializer
from firstdue.services.memory_bank import MemoryBank
from firstdue.sources.catalog import ASSESSOR, INSPECTIONS, PERMITS, VIOLATIONS

logger = get_logger(__name__)

#: How much of the budget to leave unspent for the write.
#:
#: Not just the record in hand. `_extract_and_apply` accumulates facts and
#: commits them *after* the loop -- profiles, conflict detection, the narrative
#: index -- so a margin sized to one extraction stopped the loop with seconds to
#: spare and was then killed mid-commit, writing nothing. The margin has to
#: cover the whole tail, not the last document.
#:
#: Generous on purpose: extracting fewer records and keeping them beats
#: extracting more and losing them all, which is the trade the slow loop makes
#: everywhere else.
_STOP_MARGIN_MS: Final[int] = 25_000

#: How much of a pass retrieval may spend, leaving the rest to extract and write.
#:
#: Weighted towards extraction because that is where the model calls are: a
#: fetch is tens of milliseconds and an extraction is the better part of a
#: second, so a pass that split evenly would still read far more than it could
#: ever write down.
_RETRIEVAL_SHARE: Final[float] = 0.35

#: How many records one pass will extract before deferring the rest.
#:
#: A count, not just a clock. The deadline is still a backstop, but it is a poor
#: primary bound here: model latency varies, the commit at the end of the pass
#: is proportional to what was extracted, and a pass that stopped on time could
#: still be killed while writing. A count is predictable -- at roughly nine
#: hundred milliseconds an extraction this is well inside a 120-second budget
#: with the commit paid for.
#:
#: The remainder is not lost. It is counted in `records_deferred` and read again
#: next pass: the slow loop is the part of this system that is allowed to take
#: months, and a district that ingests in slices is the design rather than a
#: degradation of it.
_MAX_RECORDS_PER_PASS: Final[int] = 40

AGENT_ID: Final[str] = "records-watcher"

#: Structured columns that are already facts. No model is involved in these:
#: a filed column is a filing, and reading it does not require judgement.
FIELD_MAPS: Final[dict[str, dict[str, str]]] = {
    PERMITS: {"stories_filed": Keys.STORIES},
    ASSESSOR: {
        "year_built": Keys.YEAR_BUILT,
        "construction_type": Keys.CONSTRUCTION_TYPE,
        "use_code": Keys.OCCUPANCY_TYPE,
        "footprint_area_m2": Keys.FOOTPRINT_AREA_M2,
    },
    INSPECTIONS: {},
    VIOLATIONS: {"status": Keys.OPEN_VIOLATION},
}

#: Which merge tier each source's records land in.
SOURCE_TYPES: Final[dict[str, SourceType]] = {
    PERMITS: SourceType.PERMIT,
    ASSESSOR: SourceType.ASSESSOR,
    INSPECTIONS: SourceType.FIRE_INSPECTION,
    VIOLATIONS: SourceType.VIOLATION,
}

WATCHED_SOURCES: Final[tuple[str, ...]] = (PERMITS, ASSESSOR, INSPECTIONS, VIOLATIONS)


class WatchResult(BaseModel):
    """What one watcher pass did to a district."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    district_id: str
    addresses_touched: tuple[str, ...] = ()
    facts_written: int = Field(default=0, ge=0)
    #: Facts re-derived identically and therefore not written again.
    facts_deduped: int = Field(default=0, ge=0)
    conflicts_detected: tuple[str, ...] = ()
    #: The ids of the facts this pass actually appended. Carried on the run
    #: record, which is what makes a run reconstructible two years later.
    written_fact_ids: tuple[str, ...] = ()
    #: Sources that could not be reached on this pass. Rendered as UNAVAILABLE.
    unavailable_sources: tuple[str, ...] = ()
    #: Records this pass read but ran out of budget before extracting.
    #:
    #: A district is bigger than one pass. Reporting the remainder is what keeps
    #: "we finished" and "we ran out of time" from producing the same empty
    #: number -- the same distinction the source states draw, applied to the
    #: agent's own work.
    records_deferred: int = Field(default=0, ge=0)
    #: Injection patterns the screen removed from ingested documents.
    screen_findings: tuple[str, ...] = ()
    #: Documents withheld from the model because the screen could not run. Not
    #: the same as a document that screened to nothing, and reported separately
    #: for that reason -- a pass that read nothing must not look like a pass
    #: that found nothing.
    documents_screen_unavailable: int = Field(default=0, ge=0)
    documents_triaged_out: int = Field(default=0, ge=0)
    #: Narratives written to the semantic index for later recall.
    narratives_indexed: int = Field(default=0, ge=0)

    # ---- the retrieval graph. All zero on a pass that did not run one, and a
    # pass that did not run one is the default; see ``RecordsWatcher.reasons``.
    #: Why retrieval stopped: ``CLOSED``, or the ceiling that ended it.
    graph_stop: str = Field(default="", max_length=40)
    #: Nodes executed. Bounded, and visible so an operator can see the bound.
    graph_steps: int = Field(default=0, ge=0)
    #: Cited filings this pass went and found.
    references_followed: int = Field(default=0, ge=0)
    #: Cited filings nothing has published yet. Each one is a thread.
    references_outstanding: int = Field(default=0, ge=0)
    #: Threads left open in the memory bank, one per outstanding reference.
    open_question_ids: tuple[str, ...] = ()


def _retrieval_deadline(deadline: datetime | None, *, started: datetime) -> datetime | None:
    """The slice of the pass retrieval may spend.

    :data:`_RETRIEVAL_SHARE` of what is left, so extraction inherits the rest.
    A pass with no deadline at all keeps none: an unbounded caller is a test or
    a one-off, and inventing a bound for it would be inventing a policy nobody
    asked for.
    """
    if deadline is None:
        return None
    remaining = (deadline - started).total_seconds()
    if remaining <= 0:
        return deadline
    return started + timedelta(seconds=remaining * _RETRIEVAL_SHARE)


class RecordsWatcher:
    """Turns municipal filings into provenanced facts on a building profile."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        facts: FactRepository,
        city: CityAdapter,
        extractor: FactExtractor,
        materializer: ProfileMaterializer,
        clock: Clock,
        ids: IdGenerator,
        audit: AuditSink | None = None,
        vectors: VectorIndex | None = None,
        memory: MemoryBank | None = None,
        grounding: GroundingService | None = None,
        planner: ReasoningPlanner | None = None,
        traces: GraphCassette | None = None,
        use_langgraph: bool = True,
        max_graph_steps: int = DEFAULT_MAX_STEPS,
        agent_version: str = "1.0.0",
    ) -> None:
        self._profiles = profiles
        self._facts = facts
        self._city = city
        self._extractor = extractor
        self._materializer = materializer
        self._clock = clock
        self._ids = ids
        self._audit = audit
        self._vectors = vectors
        # Optional, like ``audit`` and ``vectors`` above. With neither wired
        # this agent runs the fixed four-feed pass it has always run; the
        # retrieval graph is opted into by giving it somewhere to remember and
        # something to ask, never inherited.
        self._memory = memory
        self._grounding = grounding
        self._planner = planner
        self._traces = traces
        self._use_langgraph = use_langgraph
        self._max_graph_steps = max_graph_steps
        self._agent_version = agent_version

    @property
    def reasons(self) -> bool:
        """Whether this instance runs the retrieval graph at all."""
        return self._memory is not None or self._grounding is not None

    async def poll(
        self,
        *,
        district_id: str,
        sources: Sequence[SourceAdapter],
        correlation_id: str,
        since: datetime | None = None,
        deadline: datetime | None = None,
    ) -> WatchResult:
        """Poll every watched source for a district and materialize the results.

        ``deadline`` is the caller's; the tighter of it and this agent's own
        catalogued ``latency_target_ms`` bounds the retrieval graph. Passing it
        is what lets a graph park and checkpoint before the runtime kills the
        run, rather than dying mid-chase with nothing written down.
        """
        # The agent's own budget, when the caller did not name one.
        #
        # `AgentInput` carries no deadline: the runtime enforces one by killing
        # the run from outside, which the agent cannot see coming. So a pass
        # over a district larger than its budget spent the whole allowance and
        # was killed with everything it had read still unwritten -- the sources
        # polled, the quota spent, the profile untouched.
        #
        # Derived from `latency_target_ms` on this agent's own descriptor, which
        # is the same number `graph_budget` obeys and the same promise the
        # catalog makes about it. An agent that could not see its own budget
        # could not stop inside it.
        deadline = deadline or self._own_deadline()

        if not self.reasons:
            retrieved, unavailable = await self._read_every_feed(sources, since=since)
            return await self._extract_and_apply(
                district_id=district_id,
                retrieved=retrieved,
                unavailable=unavailable,
                correlation_id=correlation_id,
                deadline=deadline,
            )
        return await self._poll_by_graph(
            district_id=district_id,
            sources=sources,
            correlation_id=correlation_id,
            since=since,
            deadline=deadline,
        )

    # -------------------------------------------------------- the fixed pass

    async def _read_every_feed(
        self, sources: Sequence[SourceAdapter], *, since: datetime | None
    ) -> tuple[tuple[tuple[str, SourceSnapshot], ...], tuple[str, ...]]:
        """Read all four feeds in the order given. No decisions, no following."""
        retrieved: list[tuple[str, SourceSnapshot]] = []
        unavailable: list[str] = []
        for source in sources:
            if source.source_id not in SOURCE_TYPES:
                continue
            try:
                snapshots = await self._pull_all(source, since=since)
            except SourceUnavailableError as exc:
                logger.warning(
                    "watcher_source_unavailable",
                    extra={"source_id": source.source_id, "error_code": str(exc.code)},
                )
                unavailable.append(source.source_id)
                continue
            retrieved.extend((source.source_id, snapshot) for snapshot in snapshots)
        return tuple(retrieved), tuple(unavailable)

    # -------------------------------------------------------- the graph pass

    async def _poll_by_graph(
        self,
        *,
        district_id: str,
        sources: Sequence[SourceAdapter],
        correlation_id: str,
        since: datetime | None,
        deadline: datetime | None,
    ) -> WatchResult:
        """Retrieve until the picture closes, then extract exactly as ever.

        The two halves never meet. The graph produces snapshots; the extractor
        turns snapshots into facts. A node cannot reach a
        :class:`~firstdue.domain.facts.StructuralFact` because it never holds
        one, and every fact this pass writes carries the span, the snapshot id,
        and the provenance it would have carried under the fixed pass.
        """
        # Retrieval gets a share of the pass, not all of it.
        #
        # The graph and the extraction that follows it drew on the same budget,
        # so a district large enough to keep the planner busy spent the whole
        # 120 seconds *fetching* and was killed before a single fact was
        # written. Reading a district and learning nothing from it is the worst
        # of both: the sources were polled, the quota was spent, and the profile
        # is untouched.
        #
        # Splitting it makes the two costs visible and bounds each. Retrieval
        # stops with what it has; `_extract_and_apply` then works through as
        # much of it as the remainder allows and reports what it deferred.
        started = self._clock.now()
        retrieval_deadline = _retrieval_deadline(deadline, started=started)
        budget = graph_budget(
            AGENT_ID,
            deadline=retrieval_deadline,
            started=started,
            max_steps=self._max_graph_steps,
        )
        retrieval = RecordsRetrieval(sources=sources, budget=budget, planner=self._planner)
        digest = request_digest(
            "records-retrieval",
            district_id,
            ",".join(sorted(source.source_id for source in sources)),
            since.isoformat() if since is not None else "",
        )
        run = await run_graph(
            retrieval.spec(),
            RecordsGraphState(district_id=district_id, correlation_id=correlation_id, since=since),
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
        retrieved = tuple(
            (retrieval.origins[snapshot.snapshot_id], snapshot)
            for snapshot in state.snapshots
            if snapshot.snapshot_id in retrieval.origins
        )
        questions = await self._open_reference_questions(state, stop=run.trace.stop)
        result = await self._extract_and_apply(
            district_id=district_id,
            retrieved=retrieved,
            unavailable=state.unavailable,
            correlation_id=correlation_id,
            # The graph's own budget bounds *retrieval*; this bounds what is
            # done with what it retrieved. Without it the pass spent its whole
            # allowance extracting and was killed before writing any of it.
            deadline=deadline,
        )
        # Parked after materialization on purpose: a conflict does not exist
        # until the facts that disagree have both been written.
        questions += await self._open_conflict_questions(
            district_id=district_id, address_ids=result.addresses_touched
        )
        return result.model_copy(
            update={
                "graph_stop": str(run.trace.stop),
                "graph_steps": len(run.trace.records),
                "references_followed": len(state.followed),
                "references_outstanding": len(state.outstanding),
                "open_question_ids": questions,
            }
        )

    async def _open_conflict_questions(
        self, *, district_id: str, address_ids: Sequence[str]
    ) -> tuple[str, ...]:
        """Park a thread on every disagreement the filed record cannot settle.

        **This is the thread the incident loop closes.** A conflict names an
        attribute two sources disagree about, and no amount of further reading
        breaks the tie -- the permit will keep saying two storeys and the lidar
        will keep measuring three. What settles it is a person standing in the
        building, which is months away and may arrive as a survey or as a fire.

        The question names the conflict id and the canonical key deliberately.
        ``incident-recorder`` matches a thread to an incident by *identifier*,
        never by resemblance, and an IC resolution writes exactly those two
        fields into the incident log. So this sentence is what makes a thread
        opened in March closeable in August, and rewording it would open a
        second thread beside the one already being carried -- the question id is
        derived from the text.

        ``PUBLIC``: a conflict between filed records is itself a public fact
        about public filings, and the question quotes neither side's value.
        """
        if self._memory is None:
            return ()
        opened: list[str] = []
        for address_id in address_ids:
            profile = await self._profiles.get(address_id)
            if profile is None:
                continue
            for conflict in profile.conflicts:
                if conflict.status is not ConflictStatus.OPEN:
                    continue
                question = await self._memory.open(
                    district_id=district_id,
                    address_id=address_id,
                    question=(
                        f"Filed records do not settle {conflict.canonical_key}; "
                        f"{conflict.conflict_id} is open."
                    ),
                    waiting_on="a company survey or an on-scene observation",
                    opened_by=AGENT_ID,
                    opened_by_version=self._agent_version,
                    classification=Classification.PUBLIC,
                    evidence_fact_ids=tuple(conflict.fact_ids),
                )
                opened.append(question.question_id)
        return tuple(opened)

    async def _open_reference_questions(
        self, state: RecordsGraphState, *, stop: GraphStop
    ) -> tuple[str, ...]:
        """Open a thread for everything this pass could not finish.

        One per missing filing rather than one per pass, because that is the
        unit a later pass closes: the permit appears, that thread resolves, and
        the others keep waiting. A single "some records are missing" question
        would never be closeable by anything.

        And one more when a ceiling stopped retrieval before it had read
        everything, because a pass that ran out of budget and a pass that found
        nothing outstanding produce the same empty list and mean opposite
        things -- the same distinction the ``UNAVAILABLE`` fact exists to keep.

        ``PUBLIC`` throughout: permits, violations, inspections and the
        assessor's roll are public records, and these questions name filing
        numbers rather than anything a filing said.
        """
        opened: list[str] = []
        for reference in state.outstanding:
            question_id = await park(
                self._memory,
                agent_id=AGENT_ID,
                agent_version=self._agent_version,
                question=f"Where is the filing cited as {reference}?",
                classification=Classification.PUBLIC,
                state=state.model_copy(update={"waiting_on": f"filing {reference}"}),
            )
            if question_id is not None:
                opened.append(question_id)

        if state.outstanding or stop is GraphStop.CLOSED:
            return tuple(opened)

        unread = tuple(
            source_id
            for source_id in WATCHED_SOURCES
            if source_id not in state.queried and source_id not in state.unavailable
        )
        # Fixed text: the question id is derived from it, so rewording it would
        # open a second thread beside the one already being carried.
        question_id = await park(
            self._memory,
            agent_id=AGENT_ID,
            agent_version=self._agent_version,
            question="Which municipal feeds did retrieval not reach?",
            classification=Classification.PUBLIC,
            state=state.model_copy(
                update={"waiting_on": f"a full read of: {', '.join(unread) or 'nothing'}"}
            ),
        )
        if question_id is not None:
            opened.append(question_id)
        return tuple(opened)

    # ------------------------------------------------------------ extraction

    async def _extract_and_apply(
        self,
        *,
        district_id: str,
        retrieved: Sequence[tuple[str, SourceSnapshot]],
        unavailable: Sequence[str],
        correlation_id: str,
        deadline: datetime | None = None,
    ) -> WatchResult:
        """Turn retrieved snapshots into facts. The only place this agent writes.

        Shared by both passes. Whatever decided *which* filings to read, what
        happens to one afterwards is one function: the same screen, the same
        triage, the same extractor, the same spans, the same derived ids.

        **Bounded by the deadline, and it stops rather than dies.** A district
        is bigger than one pass: 386 structures against a 120-second budget is
        under half a second each, and one extraction costs about nine hundred
        milliseconds. Unbounded, the pass ran to the runtime's kill and wrote
        *nothing* -- every document it had already read and screened thrown away
        because the loop never reached the write. The slow loop's whole premise
        is that it accumulates over months, so a pass that gets through part of
        a district and commits that part is the design working, not degrading.

        What it did not reach is counted and reported, never silently dropped.
        """
        pending: dict[str, list[StructuralFact]] = {}
        findings: set[str] = set()
        screens_unavailable = 0
        triaged = 0
        indexed = 0

        deferred = 0
        extracted = 0
        out_of_time = False

        for source_id, snapshot in retrieved:
            source_type = SOURCE_TYPES[source_id]
            for record in snapshot.records:
                # Checked before the work, not after: stopping with a record
                # half-extracted would leave a fact without the pass that wrote
                # it. Everything already appended below stands.
                if out_of_time or self._past(deadline):
                    out_of_time = True
                    deferred += 1
                    continue
                address_id = await self._resolve_address(record, district_id)
                if address_id is None:
                    continue
                extracted += 1
                outcome = await self._extractor.extract(
                    record,
                    address_id=address_id,
                    snapshot=snapshot,
                    source_type=source_type,
                    ingested_at=self._clock.now(),
                    field_map=FIELD_MAPS.get(source_id, {}),
                )
                if outcome.screen_findings:
                    # An ingested document tried to give instructions. The
                    # instruction was removed, the rest of the narrative was
                    # kept, and the attempt is on the record.
                    await self._audit_event(
                        AuditEventKind.INJECTION_BLOCKED,
                        target=source_id,
                        address_id=address_id,
                        detail={
                            "record_ref": record.record_ref,
                            "patterns": ",".join(outcome.screen_findings),
                            # The screen that actually ran. Hard-coding the
                            # local detector here named the wrong screen on
                            # every deployment with Model Armor configured.
                            "screen": outcome.screen,
                        },
                    )
                if outcome.screen_unavailable_reason is not None:
                    # Nobody read this document. That is an operational fact
                    # an investigator reconstructing the pass needs, and it
                    # is not recoverable from an absence of facts: a document
                    # withheld from the model and a document that said
                    # nothing produce the same empty result and mean
                    # opposite things.
                    screens_unavailable += 1
                    await self._audit_event(
                        AuditEventKind.SCREEN_UNAVAILABLE,
                        target=source_id,
                        address_id=address_id,
                        detail={
                            "record_ref": record.record_ref,
                            "screen": outcome.screen,
                            "reason": outcome.screen_unavailable_reason,
                        },
                    )
                if outcome.model_unavailable_reason == "MODEL_OUTPUT_REJECTED":
                    await self._audit_event(
                        AuditEventKind.MODEL_OUTPUT_REJECTED,
                        target=source_id,
                        address_id=address_id,
                        detail={"record_ref": record.record_ref},
                    )
                findings.update(outcome.screen_findings)
                triaged += 1 if outcome.triaged_out else 0
                pending.setdefault(address_id, []).extend(outcome.facts)
                # The screened text, not the raw record: whatever an
                # ingested document tried to instruct has already been
                # removed, and the injection attempt must not be what a
                # later semantic query recalls.
                indexed += await self._index_narrative(
                    record, address_id=address_id, screened=outcome.screened_text
                )

        written: list[str] = []
        deduped = 0
        conflicts: list[str] = []
        for address_id in sorted(pending):
            stored, skipped = await self._apply(address_id, district_id, pending[address_id])
            written.extend(stored)
            deduped += skipped
            materialized = await self._materializer.run(
                address_id,
                owner=f"{AGENT_ID}:{district_id}",
                correlation_id=correlation_id,
            )
            conflicts.extend(materialized.new_conflict_ids)

        logger.info(
            "records_watcher_pass",
            extra={
                "district_id": district_id,
                "addresses": len(pending),
                "facts_written": len(written),
                "conflicts": len(conflicts),
                "unavailable": len(unavailable),
            },
        )
        return WatchResult(
            district_id=district_id,
            addresses_touched=tuple(sorted(pending)),
            facts_written=len(written),
            facts_deduped=deduped,
            conflicts_detected=tuple(conflicts),
            written_fact_ids=tuple(written),
            unavailable_sources=tuple(unavailable),
            screen_findings=tuple(sorted(findings)),
            documents_screen_unavailable=screens_unavailable,
            documents_triaged_out=triaged,
            narratives_indexed=indexed,
            records_deferred=deferred,
        )

    def _own_deadline(self) -> datetime:
        """When this pass must stop, from the catalogue rather than the caller."""
        started = self._clock.now()
        return started + timedelta(seconds=budget_seconds(descriptor_for(AGENT_ID), None, started))

    def _past(self, deadline: datetime | None) -> bool:
        """Whether the pass has spent its budget.

        A margin is deliberately left: an extraction costs the better part of a
        second, so stopping *at* the deadline would start one that cannot
        finish, and the runtime would kill the run with that record's work
        thrown away. Better to stop one record early and commit what is written.
        """
        if deadline is None:
            return False
        # The injected clock, never the wall clock. Comparing `datetime.now()`
        # against a deadline derived from a `SteppingClock` put every pass
        # instantly past its budget, deferred every record, and produced an
        # empty district -- which is what a system that reads two clocks
        # deserves. Nothing else in this file reads time directly either.
        return self._clock.now() >= deadline - timedelta(milliseconds=_STOP_MARGIN_MS)

    # ------------------------------------------------------------ internals

    async def _pull_all(
        self, source: SourceAdapter, *, since: datetime | None, max_pages: int = 50
    ) -> list[SourceSnapshot]:
        snapshots: list[SourceSnapshot] = []
        cursor: str | None = None
        for _ in range(max_pages):
            snapshot = await source.fetch(since=since, cursor=cursor)
            snapshots.append(snapshot)
            cursor = snapshot.next_cursor
            if cursor is None:
                break
        return snapshots

    async def _resolve_address(self, record: SourceRecord, district_id: str) -> str | None:
        """Which building this record is about, or ``None`` to skip it.

        The city adapter first and almost always: a normalized municipal address
        is arithmetic, and a record it settles needs nothing else.

        A record it cannot settle -- "the rear structure", a filing whose
        address line the gazetteer has never seen -- used to be dropped, on the
        correct principle that a permit filed against the wrong building is
        worse than a permit nobody saw. With a grounding service wired it gets
        one more chance, and the principle survives intact: the service chooses
        from *this district's* building ids or declines, under its own
        confidence floor and ambiguity margin, and a decline still drops the
        record. See :mod:`firstdue.ports.grounding`.
        """
        raw = record.address_id or str(record.fields.get("street_address") or "")
        if not raw:
            return None
        address = self._city.normalize_address(raw)
        if address is None or address.district_id != district_id:
            return await self._ground_address(raw, district_id)
        return address.address_id

    async def _ground_address(self, reference: str, district_id: str) -> str | None:
        """Ask the grounding service which building a stray address line means.

        The candidate list is the district's own profiles, so the answer is one
        of them or nothing -- the resolver cannot mint an id, and the membership
        is re-checked here anyway because the consequence of a wrong binding is
        a filing on the permanent record of a building it was never about.
        """
        if self._grounding is None:
            return None
        candidates = tuple(
            sorted(
                profile.address_id for profile in await self._profiles.list_by_district(district_id)
            )
        )
        if not candidates:
            return None
        # Never raises, by contract: every failure is a decline. Guarded anyway
        # for a third-party implementation that breaks it, because one
        # unmatchable permit must not fail a district poll.
        try:
            resolution = await self._grounding.resolve_reference(
                reference,
                district_id=district_id,
                candidates=candidates,
                deadline_ms=GROUNDING_DEADLINE_MS,
            )
        except Exception as exc:  # pragma: no cover - the port forbids this
            logger.warning(
                "records_grounding_unavailable", extra={"error_type": type(exc).__name__}
            )
            return None
        if not resolution.resolved or resolution.address_id not in candidates:
            return None
        logger.info(
            "records_address_grounded",
            extra={"address_id": resolution.address_id, "method": resolution.method},
        )
        return resolution.address_id

    async def _apply(
        self, address_id: str, district_id: str, facts: Sequence[StructuralFact]
    ) -> tuple[tuple[str, ...], int]:
        """Append facts to the store and the profile.

        Returns the ids actually written and the count re-derived identically.
        """
        profile = await self._profiles.get(address_id)
        if profile is None:
            profile = await self._profiles.create(
                BuildingProfile(address_id=address_id, district_id=district_id)
            )

        written: list[str] = []
        deduped = 0
        updated = profile
        for fact in sorted(facts, key=lambda f: (f.observed_at, f.fact_id)):
            try:
                await self._facts.append(fact)
            except AppendOnlyViolationError:
                # Re-derived identically: the same observation, not a new one.
                deduped += 1
                continue
            try:
                updated = updated.with_fact(fact, event=self._event(updated, fact))
            except AppendOnlyViolationError:
                deduped += 1
                continue
            written.append(fact.fact_id)

        if updated.profile_version != profile.profile_version:
            try:
                await self._profiles.save(updated, expected_version=profile.profile_version)
            except StaleVersionError:
                # Another instance wrote first. Its pass extracted the same
                # facts from the same records, so there is nothing to redo.
                logger.info("watcher_write_contended", extra={"address_id": address_id})
                return (), len(written) + deduped
        return tuple(written), deduped

    async def _index_narrative(
        self, record: SourceRecord, *, address_id: str, screened: str | None
    ) -> int:
        """Add one screened narrative to the semantic index.

        Indexing is best effort and never blocks a pass. Recall is a
        convenience for a human reading the file; facts are what the system
        believes, and they are already written by the time this runs.

        ``PHI`` and ``TIER_II_CONFIDENTIAL`` never arrive here: the payload
        refuses them at construction, and this catches that refusal rather than
        letting one confidential filing fail an entire district poll.
        """
        if self._vectors is None or not screened:
            return 0
        try:
            payload = VectorPayload(
                payload_id=f"vec/{address_id}/{record.record_ref}"[:120],
                address_id=address_id,
                canonical_key=Keys.NARRATIVE,
                text=screened[:8000],
                classification=record.classification,
                source_ref=record.record_ref,
                observed_at=record.observed_at,
            )
        except ClassificationViolationError:
            logger.info(
                "narrative_not_indexed",
                extra={"reason": "classification_forbidden", "source_ref": record.record_ref},
            )
            return 0

        try:
            return await self._vectors.upsert((payload,))
        except Exception:
            logger.warning("narrative_index_failed", extra={"address_id": address_id})
            return 0

    async def _audit_event(
        self,
        kind: AuditEventKind,
        *,
        target: str,
        detail: dict[str, str],
        address_id: str | None = None,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record_event(
            AuditEvent(
                audit_id=self._ids.new_id("audit"),
                kind=kind,
                occurred_at=self._clock.now(),
                actor=AGENT_ID,
                actor_version=self._agent_version,
                target=target,
                address_id=address_id,
                correlation_id=self._ids.new_id("corr"),
                detail=detail,
            )
        )

    def _event(self, profile: BuildingProfile, fact: StructuralFact) -> ProfileEvent:
        return ProfileEvent(
            event_id=f"pevt_{fact.fact_id.removeprefix('fact_')}",
            sequence=profile.next_sequence,
            occurred_at=fact.ingested_at,
            type=ProfileEventType.FACT_WRITTEN,
            actor=AGENT_ID,
            actor_version=self._agent_version,
            summary=f"{fact.source_type} recorded {fact.canonical_key}",
            canonical_keys=(fact.canonical_key,),
            fact_ids=(fact.fact_id,),
        )


def field_map_for(source_id: str) -> Mapping[str, str]:
    return FIELD_MAPS.get(source_id, {})
