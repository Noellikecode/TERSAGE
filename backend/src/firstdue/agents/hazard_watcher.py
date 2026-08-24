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
from firstdue.ports.clock import Clock
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
    ) -> None:
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
        budget = graph_budget(
            AGENT_ID, deadline=deadline, started=now, max_steps=self._max_graph_steps
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
        for ambiguity in state.ambiguities:
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
        for address_id in sorted(pending):
            # A registry row can name a building this district has no profile
            # for -- another district, or one nothing has filed on yet. Hazard
            # facts do not create profiles; the records watcher does that.
            existing = await self._profiles.get(address_id)
            if existing is None or existing.district_id != district_id:
                continue
            touched.append(address_id)
            written.extend(await self._apply(address_id, district_id, pending[address_id], now))
            await self._materializer.run(
                address_id,
                owner=f"{AGENT_ID}:{district_id}",
                correlation_id=correlation_id,
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
