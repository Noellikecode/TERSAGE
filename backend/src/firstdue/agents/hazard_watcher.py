"""Hazard Watcher -- federal registries and the one confidential filing.

EPA (FRS, TRI, RMP), PHMSA pipelines, NREL EV charging, and Tier II. Four very
different data sources with one thing in common: **absence here is dangerous to
misread.** "No Tier II filing on record" and "no hazardous materials present"
are different statements, and a watcher that returned an empty list for a source
it could not reach would collapse them.

So every fact this agent writes carries its classification, and a source that is
unavailable produces an ``UNAVAILABLE`` fact naming the source rather than
nothing at all.

Tier II is the reason this agent is published by county emergency management
rather than by the fire department: the filings are confidential, the county
holds them, and the fire department subscribes to a pinned version of the agent
that reads them. The subscription is the authorization boundary.

**The cross-check, and the line it does not cross.** Given somewhere to
remember (a :class:`~firstdue.services.memory_bank.MemoryBank`) or something to
ask (a :class:`~firstdue.ports.grounding.GroundingService`), this agent stops
reading the four registries in a fixed order and starts cross-checking them
against each other -- because ``ACME PLATING INC`` in the EPA registry and
``Acme Plating`` on a Tier II filing at the same parcel are either one facility
or two, and the fixed pass could not tell. See
:mod:`firstdue.agents.graphs.hazard`.

**The model may not author a fact.** The graph decides *what to look up, what to
cross-check, and when it is done*, and that is the whole of its authority. Every
value it causes to be written still goes through :func:`_values_for` and
:meth:`HazardWatcher._settle` -- the record's own fields, the record's
timestamp, its classification, the derived fact id, the confidence this module
assigns -- and there is no path from a graph node to a
:class:`~firstdue.domain.facts.StructuralFact`. A graph that could emit a
``hazard.tier_ii_present`` of its own would not be an improvement to this agent;
it would be the end of the reason anyone trusts its output.

With neither collaborator wired -- the default, and what ``make demo`` and the
whole test suite run -- none of that happens and this agent behaves exactly as
it always has.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
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
from firstdue.agents.graphs.hazard import (
    CROSS_CHECK_ORDER,
    HazardCrossCheck,
    HazardGraphState,
)
from firstdue.domain.enums import Classification, SourceType
from firstdue.domain.facts import StructuralFact, natural_fact_id
from firstdue.domain.keys import Keys
from firstdue.domain.profiles import ProfileEvent, ProfileEventType
from firstdue.domain.values import (
    BooleanValue,
    FactValue,
    QuantityValue,
    TextValue,
    UnavailableValue,
)
from firstdue.errors import AppendOnlyViolationError, SourceUnavailableError, StaleVersionError
from firstdue.extraction.recorded import request_digest
from firstdue.observability.logging import get_logger
from firstdue.ports.audit import AuditEvent, AuditEventKind, AuditSink
from firstdue.ports.clock import Clock, IdGenerator
from firstdue.ports.grounding import GroundingService
from firstdue.ports.repositories import FactRepository, ProfileRepository
from firstdue.ports.sources import SourceAdapter, SourceRecord
from firstdue.services.materialization import ProfileMaterializer
from firstdue.services.memory_bank import MemoryBank
from firstdue.sources.catalog import EPA, NREL, PHMSA, TIER_II

logger = get_logger(__name__)

AGENT_ID: Final[str] = "hazard-watcher"

SOURCE_TYPES: Final[dict[str, SourceType]] = {
    EPA: SourceType.EPA_FRS,
    PHMSA: SourceType.PHMSA_PIPELINE,
    NREL: SourceType.NREL_EV,
    TIER_II: SourceType.TIER_II,
}

#: The attribute each source settles. Used to write an explicit UNAVAILABLE
#: when the source is down, so absence never reads as "nothing there".
SOURCE_KEYS: Final[dict[str, str]] = {
    EPA: Keys.HAZARD_TIER_II_PRESENT,
    PHMSA: Keys.HAZARD_PIPELINE_PROXIMITY_M,
    NREL: Keys.HAZARD_EV_CHARGER,
    TIER_II: Keys.HAZARD_TIER_II_PRESENT,
}


class HazardWatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    district_id: str
    addresses_touched: tuple[str, ...] = ()
    facts_written: int = Field(default=0, ge=0)
    #: The ids of the facts this pass appended, for the run record.
    written_fact_ids: tuple[str, ...] = ()
    #: Facts written as UNAVAILABLE because a registry could not be reached.
    unavailable_facts: int = Field(default=0, ge=0)
    unavailable_sources: tuple[str, ...] = ()
    #: Classifications actually written, so the console can show what was touched.
    classifications: tuple[str, ...] = ()

    # ---- the cross-check graph. All zero on a pass that did not run one, and
    # a pass that did not run one is the default; see ``HazardWatcher.reasons``.
    #: Why the graph stopped: ``CLOSED``, or the ceiling that ended it.
    graph_stop: str = Field(default="", max_length=40)
    #: Nodes executed. Bounded, and visible so an operator can see the bound.
    graph_steps: int = Field(default=0, ge=0)
    #: Identities the cross-check settled from one registry against another.
    identities_settled: int = Field(default=0, ge=0)
    #: Threads left open in the memory bank, one per unsettled identity.
    open_question_ids: tuple[str, ...] = ()


def _values_for(source_id: str, record: SourceRecord) -> list[tuple[str, FactValue, float]]:
    """Every (key, value, confidence) one hazard record supports."""
    fields = record.fields
    if source_id == TIER_II:
        values: list[tuple[str, FactValue, float]] = [
            (Keys.HAZARD_TIER_II_PRESENT, BooleanValue(boolean=bool(fields.get("present"))), 0.95)
        ]
        location = fields.get("storage_location")
        if location:
            values.append(
                (Keys.HAZARD_TIER_II_LOCATION, TextValue(text=str(location)[:2000]), 0.95)
            )
        return values

    if source_id == EPA:
        programs = fields.get("programs") or []
        return [
            (
                Keys.HAZARD_TIER_II_PRESENT,
                BooleanValue(boolean=bool({"RMP", "TRI"} & set(programs))),
                0.9,
            )
        ]

    if source_id == PHMSA:
        proximity = fields.get("proximity_m")
        if proximity is None:
            return []
        return [
            (
                Keys.HAZARD_PIPELINE_PROXIMITY_M,
                QuantityValue(magnitude=float(proximity), unit="m"),
                0.9,
            )
        ]

    if source_id == NREL:
        return [(Keys.HAZARD_EV_CHARGER, BooleanValue(boolean=True), 0.9)]

    return []


#: How many identity threads one pass may open.
#:
#: A person works these one at a time, so a pass that opened a hundred and
#: eighty-seven of them in one go was not helping anybody -- and it cost the
#: agent its whole budget doing it. Twelve is the same ceiling
#: `geometry-watcher` uses, for the same reason: a district larger than one
#: budget is the ordinary case, and the work carries over to the next pass.
MAX_QUESTIONS_PER_PASS: Final[int] = 12

#: How many addresses one pass applies hazard facts to and materialises.
#:
#: Three Firestore round-trips apiece -- read, write, materialise -- at about
#: 1.8 seconds each against a real project. A city-wide EPA sweep names
#: hundreds of buildings, and applying all of them serially spent this agent's
#: whole 180-second budget and was killed before it finished. The rest carry
#: over: they are still pending on the next pass.
MAX_ADDRESSES_PER_PASS: Final[int] = 15

#: How much of a pass the cross-check graph may spend, leaving the rest to
#: apply what it gathered and to say that it ran.
#:
#: The same split ``records-watcher`` makes between retrieval and extraction,
#: and for the same reason: without it the graph's budget *is* the run's budget,
#: so a graph that spends its allowance legally leaves nothing for the apply
#: loop and the pass record, and the runtime cancels the coroutine before this
#: agent has written anything at all. Weighted towards the graph because the
#: parks and the registry reads are where the seconds go; what is kept back has
#: to cover fifteen addresses of Firestore and one audit append.
_CROSSCHECK_SHARE: Final[float] = 0.6

#: Stop applying this far short of the deadline.
#:
#: Sized to the tail, not to one address: each address is a profile read, a
#: fact write and a materialisation, and the pass record comes after all of
#: them. Stopping with only one address's worth of slack would be stopping
#: early *and* still being killed before the record, which is the worst of both.
_STOP_MARGIN_MS: Final[int] = 20_000


def _crosscheck_deadline(deadline: datetime | None, *, started: datetime) -> datetime | None:
    """The slice of the pass the cross-check graph may spend.

    ``None`` stays ``None``: an unbounded caller is a test or a one-off, and
    inventing a bound for it would be inventing a policy nobody published.
    """
    if deadline is None:
        return None
    remaining = (deadline - started).total_seconds()
    if remaining <= 0:
        return deadline
    return started + timedelta(seconds=remaining * _CROSSCHECK_SHARE)


class HazardWatcher:
    """Federal and confidential hazard registries, with classification intact."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        facts: FactRepository,
        materializer: ProfileMaterializer,
        clock: Clock,
        memory: MemoryBank | None = None,
        grounding: GroundingService | None = None,
        planner: ReasoningPlanner | None = None,
        traces: GraphCassette | None = None,
        use_langgraph: bool = True,
        max_graph_steps: int = DEFAULT_MAX_STEPS,
        agent_version: str = "1.0.0",
        audit: AuditSink | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        #: Optional, so every existing caller constructs unchanged. Without it
        #: this agent reads federal registries and leaves nothing in the log,
        #: which on the console is indistinguishable from not having run.
        self._audit = audit
        self._ids = ids
        self._profiles = profiles
        self._facts = facts
        self._materializer = materializer
        self._clock = clock
        # Optional, exactly like ``bus`` and ``vectors`` on the other watchers,
        # and for a stronger reason: with neither of them wired this agent runs
        # the fixed pass it has always run, byte for byte. The reasoning graph
        # is something a deployment opts into by giving the agent somewhere to
        # remember and something to ask, not a change of behaviour it inherits.
        self._memory = memory
        self._grounding = grounding
        self._planner = planner
        self._traces = traces
        self._use_langgraph = use_langgraph
        self._max_graph_steps = max_graph_steps
        self._agent_version = agent_version

    @property
    def reasons(self) -> bool:
        """Whether this instance runs the cross-check graph at all.

        One predicate, read here and by nothing else, so "does this deployment
        reason" has a single answer rather than two conditions that can drift.
        """
        return self._memory is not None or self._grounding is not None

    async def poll(
        self,
        *,
        district_id: str,
        sources: Sequence[SourceAdapter],
        correlation_id: str,
        deadline: datetime | None = None,
    ) -> HazardWatchResult:
        """Read the district's hazard registries and write what they say.

        ``deadline`` is the caller's, and the tighter of it and the descriptor's
        own ``latency_target_ms`` bounds the cross-check -- see
        :func:`~firstdue.agents.graphs.base.graph_budget`. It is optional
        because the runtime already enforces a deadline around this call; what
        passing it buys is a graph that *parks and checkpoints* before the
        runtime kills it, rather than a run that dies mid-thought with nothing
        written down.
        """
        now = self._clock.now()
        if not self.reasons:
            gathered, unavailable = await self._read_every_registry(sources)
            return await self._settle(
                district_id=district_id,
                gathered=gathered,
                unavailable=unavailable,
                correlation_id=correlation_id,
                now=now,
                deadline=deadline,
            )
        return await self._poll_by_graph(
            district_id=district_id,
            sources=sources,
            correlation_id=correlation_id,
            deadline=deadline,
            now=now,
        )

    # -------------------------------------------------------- the fixed pass

    async def _read_every_registry(
        self, sources: Sequence[SourceAdapter]
    ) -> tuple[dict[str, tuple[SourceRecord, ...]], tuple[str, ...]]:
        """Read all four, in the order the caller listed them. No decisions."""
        gathered: dict[str, tuple[SourceRecord, ...]] = {}
        unavailable: list[str] = []
        for source in sources:
            if source.source_id not in SOURCE_TYPES:
                continue
            try:
                snapshot = await source.fetch()
            except SourceUnavailableError as exc:
                logger.warning(
                    "hazard_source_unavailable",
                    extra={"source_id": source.source_id, "error_code": str(exc.code)},
                )
                unavailable.append(source.source_id)
                continue
            gathered[source.source_id] = snapshot.records
        return gathered, tuple(unavailable)

    # --------------------------------------------------------- the graph pass

    async def _poll_by_graph(
        self,
        *,
        district_id: str,
        sources: Sequence[SourceAdapter],
        correlation_id: str,
        deadline: datetime | None,
        now: datetime,
    ) -> HazardWatchResult:
        """Cross-check identities first, then write facts the ordinary way.

        The two halves are deliberately sequential and deliberately separate.
        The graph decides what to read and binds stray registry rows to
        buildings; :meth:`_settle` then does exactly what the fixed pass does
        with whatever the graph gathered. There is no path by which a node's
        decision reaches a value, because the nodes and the fact construction do
        not meet.
        """
        # The graph gets a share of the pass, not all of it -- see
        # `_CROSSCHECK_SHARE`. `_settle` and the pass record below inherit the
        # remainder, which is what makes a truncated cross-check still leave
        # evidence that it ran.
        budget = graph_budget(
            AGENT_ID,
            deadline=_crosscheck_deadline(deadline, started=now),
            started=now,
            max_steps=self._max_graph_steps,
        )
        crosscheck = HazardCrossCheck(
            sources=sources,
            budget=budget,
            planner=self._planner,
            grounding=self._grounding,
        )
        profiles = await self._profiles.list_by_district(district_id)
        digest = request_digest(
            "hazard-crosscheck",
            district_id,
            ",".join(sorted(source.source_id for source in sources)),
        )
        run = await run_graph(
            crosscheck.spec(),
            HazardGraphState(
                district_id=district_id,
                correlation_id=correlation_id,
                address_ids=tuple(sorted(profile.address_id for profile in profiles)),
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
        questions = await self._open_questions(state, stop=run.trace.stop)
        result = await self._settle(
            district_id=district_id,
            gathered=state.gathered,
            unavailable=state.unavailable,
            correlation_id=correlation_id,
            now=now,
            deadline=deadline,
        )
        return result.model_copy(
            update={
                "graph_stop": str(run.trace.stop),
                "graph_steps": len(run.trace.records),
                "identities_settled": len(state.settled),
                "open_question_ids": questions,
            }
        )

    async def _open_questions(self, state: HazardGraphState, *, stop: GraphStop) -> tuple[str, ...]:
        """Open a thread for everything this pass could not finish.

        One per unsettled identity rather than one per pass: a person works
        these individually, and a single "some identities are unclear" thread
        would accumulate eliminations from unrelated buildings until nobody
        could tell which registry had been tried for which facility.

        And one more when a ceiling stopped the pass before it had even worked
        out what was ambiguous. That case has no identity to name and is exactly
        the case that must not pass silently: a pass that ran out of budget
        looks, from the outside, identical to a pass that found nothing -- and
        this agent exists because those two are not the same statement.
        """
        opened: list[str] = []
        # Bounded per pass.
        #
        # One thread per unsettled identity is right -- see above -- and
        # unbounded it is not. Measured against the live district on
        # 2026-08-27, with EPA FRS unreachable and PHMSA and Tier II having no
        # public endpoint by statute, a single pass raised **187** ambiguities
        # and parked every one of them. Each park opens a question and
        # checkpoints it, each of those writes to the Memory Bank, and the
        # bank's write quota answers 429 and is retried: 187 parks took 178
        # seconds, the agent was killed at its 180-second budget, and one
        # console poll ran for five minutes and fifty seconds.
        #
        # The ceiling does not lose anything. An ambiguity that is not parked
        # this pass is still ambiguous next pass and is raised again; what the
        # cap changes is how many threads one pass opens for identities it
        # cannot settle anyway while its registries are down.
        ordered = sorted(state.ambiguities, key=lambda a: a.key)
        deferred = max(0, len(ordered) - MAX_QUESTIONS_PER_PASS)
        if deferred:
            logger.info(
                "hazard_questions_deferred",
                extra={"opened": MAX_QUESTIONS_PER_PASS, "deferred": deferred},
            )
        for ambiguity in ordered[:MAX_QUESTIONS_PER_PASS]:
            question_id = await park(
                self._memory,
                agent_id=AGENT_ID,
                agent_version=self._agent_version,
                question=ambiguity.question,
                classification=ambiguity.classification,
                state=state.model_copy(update={"waiting_on": ambiguity.waiting_on}),
                address_id=ambiguity.address_id,
            )
            if question_id is not None:
                opened.append(question_id)

        if state.ambiguities or stop is GraphStop.CLOSED:
            return tuple(opened)

        unread = tuple(
            source_id
            for source_id in CROSS_CHECK_ORDER
            if source_id not in state.queried and source_id not in state.unavailable
        )
        question_id = await park(
            self._memory,
            agent_id=AGENT_ID,
            agent_version=self._agent_version,
            # Fixed text: the id is derived from it, so a pass that reworded
            # the sentence would open a second thread beside the one already
            # being carried for this district.
            question="Which hazard registries did the cross-check not reach?",
            # The registries it did not reach are named on ``waiting_on``, and
            # this is a question about the pass rather than about a filing, so
            # nothing confidential is in it.
            classification=Classification.PUBLIC,
            state=state.model_copy(
                update={"waiting_on": f"a full read of: {', '.join(unread) or 'nothing'}"}
            ),
        )
        if question_id is not None:
            opened.append(question_id)
        return tuple(opened)

    # ------------------------------------------------------------ internals

    async def _settle(
        self,
        *,
        district_id: str,
        gathered: Mapping[str, tuple[SourceRecord, ...]],
        unavailable: Sequence[str],
        correlation_id: str,
        now: datetime,
        deadline: datetime | None = None,
    ) -> HazardWatchResult:
        """Turn registry rows into facts. The only place this agent writes.

        Shared by both passes on purpose. Whatever decided *which* rows to read,
        what happens to a row afterwards is one function -- the same values, the
        same confidences, the same derived fact ids, the same classification
        travelling through to the vector guard.
        """
        pending: dict[str, list[StructuralFact]] = {}
        unavailable_facts = 0
        classifications: set[str] = set()

        for source_id in unavailable:
            source_type = SOURCE_TYPES[source_id]
            # Every address in the district gets an explicit UNAVAILABLE for
            # this attribute. "The registry is down" is an operational fact, and
            # it is not the same as "no hazard here".
            for profile in await self._profiles.list_by_district(district_id):
                pending.setdefault(profile.address_id, []).append(
                    self._unavailable_fact(profile.address_id, source_id, source_type, now)
                )
                unavailable_facts += 1

        for source_id, records in gathered.items():
            source_type = SOURCE_TYPES[source_id]
            for record in records:
                if record.address_id is None:
                    continue
                classifications.add(str(record.classification))
                for key, value, confidence in _values_for(source_id, record):
                    pending.setdefault(record.address_id, []).append(
                        self._fact(
                            record.address_id,
                            key,
                            value,
                            record=record,
                            source_type=source_type,
                            confidence=confidence,
                            now=now,
                        )
                    )

        written: list[str] = []
        touched: list[str] = []
        # Bounded per pass, and for the same reason the questions above are.
        #
        # Each address here costs a profile read, a fact write and a
        # materialisation -- three Firestore round-trips, about 1.8 seconds
        # against a real project. A city-wide registry sweep returns hundreds
        # of rows, so an unbounded loop spent 160 seconds materialising a
        # district and was killed at its budget with the tail unwritten.
        #
        # Deterministic order, so successive passes walk the same list and the
        # ones deferred today are the ones taken next. Nothing is dropped: a
        # hazard fact not applied this pass is still pending next pass.
        addresses = sorted(pending)
        deferred_addresses = max(0, len(addresses) - MAX_ADDRESSES_PER_PASS)
        if deferred_addresses:
            logger.info(
                "hazard_addresses_deferred",
                extra={
                    "applied": MAX_ADDRESSES_PER_PASS,
                    "deferred": deferred_addresses,
                    "district_id": district_id,
                },
            )
        for position, address_id in enumerate(addresses[:MAX_ADDRESSES_PER_PASS]):
            if self._past(deadline):
                # The count is the cap on an ordinary pass; this is the cap on
                # a pass whose registries were slow. Everything applied so far
                # is already committed and everything below is still pending,
                # so stopping here costs a pass rather than the work -- and it
                # buys the one thing that cannot be recovered next pass, which
                # is the record that this pass happened at all.
                deferred_addresses += min(len(addresses), MAX_ADDRESSES_PER_PASS) - position
                break
            # A registry row can name a building this district has no profile
            # for -- another district, or one nothing has filed on yet. Hazard
            # facts do not create profiles; the records watcher does that.
            existing = await self._profiles.get(address_id)
            if existing is None or existing.district_id != district_id:
                continue
            touched.append(address_id)
            applied = await self._apply(address_id, district_id, pending[address_id], now)
            written.extend(applied)
            await self._materializer.run(
                address_id,
                owner=f"{AGENT_ID}:{district_id}",
                correlation_id=correlation_id,
            )
            # Per address, as it is applied. The pass record below is the
            # summary; this is the work, and it is what a console watching a
            # multi-minute pass has to show while the pass is still running.
            await self._record_step(
                address_id,
                correlation_id=correlation_id,
                written=len(applied),
                hazards=pending[address_id],
            )

        await self._record_pass(
            district_id,
            correlation_id=correlation_id,
            touched=len(touched),
            written=len(written),
            deferred_addresses=deferred_addresses,
            unavailable=unavailable,
        )

        return HazardWatchResult(
            district_id=district_id,
            addresses_touched=tuple(touched),
            facts_written=len(written),
            written_fact_ids=tuple(written),
            unavailable_facts=unavailable_facts,
            unavailable_sources=tuple(unavailable),
            classifications=tuple(sorted(classifications)),
        )

    def _past(self, deadline: datetime | None) -> bool:
        """Whether this pass has spent its budget down to the commit tail.

        The injected clock, never the wall clock -- the same trap
        ``records-watcher`` and ``geometry-watcher`` name: a deadline derived
        from a ``SteppingClock`` compared against ``datetime.now()`` reads as
        spent before the first address and every pass would apply nothing.
        """
        if deadline is None:
            return False
        return self._clock.now() >= deadline - timedelta(milliseconds=_STOP_MARGIN_MS)

    async def _record_pass(
        self,
        district_id: str,
        *,
        correlation_id: str,
        touched: int,
        written: int,
        deferred_addresses: int,
        unavailable: Sequence[str],
    ) -> None:
        """One line in the log saying what this pass read and what it could not.

        The unavailable registries are the point. A pass that found no hazards
        because it read four registries and a pass that found none because
        three of them refused are different statements, and this agent exists
        to keep them apart -- so the log has to say which one happened.
        """
        if self._audit is None or self._ids is None:
            return
        detail = {
            "addresses": str(touched),
            "facts_written": str(written),
            "deferred": str(deferred_addresses),
        }
        if unavailable:
            detail["unavailable"] = ",".join(unavailable)
        await self._audit.record_event(
            AuditEvent(
                audit_id=self._ids.new_id("audit"),
                kind=AuditEventKind.AGENT_PASS,
                occurred_at=self._clock.now(),
                actor=AGENT_ID,
                actor_version=self._agent_version,
                target=district_id,
                # The pass's own id, not a fresh one -- see `_record_step`. A
                # closing line that floated loose could not be grouped with the
                # steps it closes.
                correlation_id=correlation_id,
                detail=detail,
            )
        )

    async def _record_step(
        self,
        address_id: str,
        *,
        correlation_id: str,
        written: int,
        hazards: Sequence[StructuralFact],
    ) -> None:
        """One address, as its registry hits are applied.

        Canonical keys and the registries they came from -- both are this
        agent's own vocabulary, not text out of a filing, so neither can carry
        a document's words into the log.
        """
        if self._audit is None or self._ids is None:
            return
        keys = sorted({fact.canonical_key for fact in hazards})
        registries = sorted({fact.source_type for fact in hazards})
        await self._audit.record_event(
            AuditEvent(
                audit_id=self._ids.new_id("audit"),
                kind=AuditEventKind.AGENT_STEP,
                occurred_at=self._clock.now(),
                actor=AGENT_ID,
                actor_version=self._agent_version,
                target=address_id,
                correlation_id=correlation_id,
                detail={
                    "facts_written": str(written),
                    "hazards": ",".join(keys) if keys else "none",
                    "registries": ",".join(registries) if registries else "none",
                },
            )
        )

    async def _apply(
        self,
        address_id: str,
        district_id: str,
        facts: Sequence[StructuralFact],
        now: datetime,
    ) -> tuple[str, ...]:
        """Append this address's facts, and report the ids that landed.

        The ids rather than a count, because the run record is what makes a pass
        reconstructible years later and a run record naming no facts cannot be
        replayed against them -- the same reason the records watcher has always
        returned them.
        """
        profile = await self._profiles.get(address_id)
        if profile is None:
            return ()

        written: list[str] = []
        updated = profile
        for fact in sorted(facts, key=lambda f: (f.canonical_key, f.fact_id)):
            try:
                await self._facts.append(fact)
            except AppendOnlyViolationError:
                continue
            try:
                updated = updated.with_fact(
                    fact,
                    event=ProfileEvent(
                        event_id=f"pevt_{fact.fact_id.removeprefix('fact_')}",
                        sequence=updated.next_sequence,
                        occurred_at=now,
                        type=ProfileEventType.FACT_WRITTEN,
                        actor=AGENT_ID,
                        actor_version=self._agent_version,
                        summary=f"{fact.source_type} recorded {fact.canonical_key}",
                        canonical_keys=(fact.canonical_key,),
                        fact_ids=(fact.fact_id,),
                    ),
                )
            except AppendOnlyViolationError:
                continue
            written.append(fact.fact_id)

        if updated.profile_version != profile.profile_version:
            try:
                await self._profiles.save(updated, expected_version=profile.profile_version)
            except StaleVersionError:
                return ()
        return tuple(written)

    def _unavailable_fact(
        self, address_id: str, source_id: str, source_type: SourceType, now: datetime
    ) -> StructuralFact:
        key = SOURCE_KEYS.get(source_id, Keys.HAZARD_TIER_II_PRESENT)
        value = UnavailableValue(source_id=source_id, reason="source unreachable")
        return StructuralFact(
            fact_id=natural_fact_id(
                address_id=address_id,
                canonical_key=key,
                source_ref=f"{source_id}/unavailable",
                observed_at=now,
                rendered_value=value.render(),
            ),
            address_id=address_id,
            canonical_key=key,
            value=value,
            source_type=source_type,
            source_ref=f"{source_id}/unavailable",
            source_snapshot_id=f"{source_id}:unavailable:{now.isoformat()}",
            observed_at=now,
            ingested_at=now,
            confidence=0.0,
            classification=Classification.PUBLIC,
            produced_by_agent=AGENT_ID,
            produced_by_version=self._agent_version,
        )

    def _fact(
        self,
        address_id: str,
        key: str,
        value: Any,
        *,
        record: SourceRecord,
        source_type: SourceType,
        confidence: float,
        now: datetime,
    ) -> StructuralFact:
        return StructuralFact(
            fact_id=natural_fact_id(
                address_id=address_id,
                canonical_key=key,
                source_ref=record.record_ref,
                observed_at=record.observed_at,
                rendered_value=value.render(),
            ),
            address_id=address_id,
            canonical_key=key,
            value=value,
            source_type=source_type,
            source_ref=record.record_ref,
            source_snapshot_id=record.record_ref,
            observed_at=record.observed_at,
            ingested_at=now,
            confidence=confidence,
            # The record's own classification travels with the fact. A Tier II
            # filing stays TIER_II_CONFIDENTIAL all the way to the vector guard.
            classification=record.classification,
            produced_by_agent=AGENT_ID,
            produced_by_version=self._agent_version,
        )
