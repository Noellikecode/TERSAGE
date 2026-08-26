"""The hazard watcher's cross-check graph -- who is actually at this address.

Federal registries do not agree about names. ``ACME PLATING INC`` in the EPA
Facility Registry Service and ``Acme Plating`` on a Tier II filing at the same
parcel are either one facility filed twice or two tenants sharing a building,
and the two readings imply different things at 03:00: one ammonia inventory or
two, one responsible operator or two. The fixed pass this graph sits on top of
could not tell them apart, because it never asked -- it mapped keys and moved
on.

So this graph does what a records clerk does. It reads the anchor registry,
notices where identity is ambiguous, picks another registry that might settle
it, reads that one, and repeats until the ambiguity is gone or there is nothing
left to ask. What it cannot settle it hands to a person as an open question
carrying the registries it already tried, so tomorrow's pass does not spend its
budget re-proving today's dead ends.

Two boundaries make this safe to run unattended.

**Deciding what to read is not deciding what is true.** Every hazard fact this
agent writes still comes out of :func:`~firstdue.agents.hazard_watcher._values_for`
and the record it was read from, with the source's classification, the record's
timestamp, and the natural fact id intact. The graph changes which records were
fetched and which references got bound to a building; it never changes what a
record says, and there is no path from a node's decision to a value.

**The graph never decides a registry goes unread.** It chooses the *order* of
the cross-check and which registry answers the identity question, and then the
``sweep`` node reads whatever is left before the pass closes. That is not
thoroughness for its own sake: this agent's entire doctrine is that "no Tier II
filing on record" and "no hazardous materials present" are different statements,
so a registry an agent skipped because it felt confident would be the exact
failure the ``UNAVAILABLE`` fact exists to prevent.

Where a registry row names a street and no building, the grounding service is
asked which of *this district's* buildings it means -- the case
:mod:`firstdue.ports.grounding` was written for -- and its answer is re-checked
against that closed list before anything uses it. Everything else is settled by
one registry corroborating another, which is evidence rather than an opinion
about evidence. An identity nobody can pin is left unpinned.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.agents.graphs.base import (
    GROUNDING_DEADLINE_MS,
    NODE_PARK,
    PLANNER_DEADLINE_MS,
    STOP,
    BudgetGuard,
    FixedOrderPlanner,
    GraphSpec,
    GraphState,
    GraphStop,
    NodeResult,
    ReasoningPlanner,
    bounded,
)
from firstdue.domain.enums import Classification
from firstdue.errors import SourceUnavailableError
from firstdue.observability.logging import get_logger
from firstdue.ports.grounding import GroundingService, Resolution
from firstdue.ports.sources import SourceAdapter, SourceRecord
from firstdue.sources.catalog import EPA, NREL, PHMSA, TIER_II

logger = get_logger(__name__)

NODE_SURVEY: Final[str] = "survey"
NODE_SCREEN: Final[str] = "screen"
NODE_PLAN: Final[str] = "plan"
NODE_CROSS_CHECK: Final[str] = "cross_check"
NODE_SWEEP: Final[str] = "sweep"

#: The order the cross-check considers registries, and the planner's default.
#:
#: EPA anchors because it is the registry that *names facilities*: a row there
#: has a primary name and a registry id, which is what an identity question is
#: about. Tier II is next because a filing is a statement by the occupant
#: themselves and settles a name outright when it exists. PHMSA and NREL name
#: pipeline operators and charging networks rather than building tenants, so
#: they can corroborate an address and almost never a name.
CROSS_CHECK_ORDER: Final[tuple[str, ...]] = (EPA, TIER_II, PHMSA, NREL)

#: Corporate suffixes and articles that carry no identity. Two filings that
#: differ only by these are the same name spelled by two clerks, which is the
#: ambiguity worth noticing; two filings that differ by anything else are two
#: names and this graph does not conflate them.
_NOISE_WORDS: Final[frozenset[str]] = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "llp",
        "lp",
        "ltd",
        "limited",
        "co",
        "company",
        "corp",
        "corporation",
        "the",
        "and",
        "of",
        "dba",
    }
)

_PUNCTUATION = re.compile(r"[^a-z0-9]+")


def normalize_facility_name(name: str) -> str:
    """Reduce a filed facility name to what two clerks would agree on.

    Case, punctuation, and corporate suffixes are the differences that mean
    nothing. Everything else is kept, because a normalizer aggressive enough to
    merge ``Acme Plating`` with ``Acme Plumbing`` would manufacture the very
    ambiguity it is supposed to detect.
    """
    words = [word for word in _PUNCTUATION.sub(" ", name.lower()).split() if word]
    kept = [word for word in words if word not in _NOISE_WORDS]
    return " ".join(kept or words)


class FacilityAmbiguity(BaseModel):
    """One identity this pass cannot settle from what it has read so far.

    Two shapes, kept apart because they are answered by different evidence:
    ``UNPLACED`` is a registry row that names a street and no building, and is
    settled by finding the building; ``ALIASED`` is two filed names at one
    building that normalize alike, and is settled by another registry naming one
    of them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(pattern=r"^(UNPLACED|ALIASED)$")
    #: What is being resolved: a street address, or a normalized facility name.
    reference: str = Field(min_length=1, max_length=300)
    #: The closed list an answer must come from. Nothing else is acceptable.
    candidates: tuple[str, ...] = ()
    #: The building this is about, once known.
    address_id: str | None = Field(default=None, max_length=120)
    record_refs: tuple[str, ...] = ()
    #: The highest classification among the rows that raised this ambiguity.
    #: It travels onto the open question, because a thread about who is filing
    #: Tier II is a Tier II thread whatever the question text says -- and the
    #: bank gates recall on exactly this.
    classification: Classification = Classification.PUBLIC

    @property
    def key(self) -> str:
        """Stable, contentless id for this ambiguity.

        Stable because ``ruled_out`` has to name the same ambiguity in next
        week's pass as in this one, and contentless because it does: the key is
        written to durable memory and to an open question, and a key that
        carried the filed name would put a Tier II occupant into a store nothing
        screened it into. Hashing the reference keeps both properties.
        """
        digest = hashlib.sha256(self.reference.encode("utf-8")).hexdigest()[:12]
        return f"{self.kind}:{self.address_id or '-'}:{digest}"

    @property
    def waiting_on(self) -> str:
        """What a person would have to supply, said without naming an occupant."""
        if self.kind == "UNPLACED":
            # An address is the subject of this system and is never redacted;
            # see `observability/redaction.py`.
            return f"the building at {self.reference}"
        return f"which filed name is one facility at {self.address_id}"

    @property
    def question(self) -> str:
        """The sentence the memory bank derives this thread's id from.

        Stable across passes and free of occupant names, for the same two
        reasons :attr:`key` is: the id is a hash of this text, so a reworded
        question would open a second thread beside the one already being
        carried, and the text itself is stored.
        """
        if self.kind == "UNPLACED":
            return f"Which building in this district is the registry row for {self.reference}?"
        return (
            f"Are the filings at {self.address_id} one facility under two names, or two "
            f"facilities?"
        )


class HazardGraphState(GraphState):
    """What the cross-check knows. Records live here and go no further."""

    #: Every building in the district. The closed candidate list an unplaced
    #: registry row may be bound to, and nothing outside it.
    address_ids: tuple[str, ...] = ()
    #: Registries read this pass, in the order they were read.
    queried: tuple[str, ...] = ()
    #: Registries that raised. Rendered as UNAVAILABLE facts by the agent, the
    #: same as they always were -- an unreachable registry is not an absence.
    unavailable: tuple[str, ...] = ()
    #: Records per registry, address-bound where this pass managed to bind them.
    #: Never checkpointed: a Tier II filing is confidential and durable memory
    #: is not where it goes.
    gathered: dict[str, tuple[SourceRecord, ...]] = Field(default_factory=dict)
    #: Identities still open.
    ambiguities: tuple[FacilityAmbiguity, ...] = ()
    #: Identities this pass settled, for the report and the trace.
    settled: tuple[str, ...] = ()
    #: How many registries had been read the last time ``screen`` ran.
    screened: int = 0
    #: The registry ``plan`` chose and ``cross_check`` has yet to read.
    chosen: str = Field(default="", max_length=120)

    @property
    def records(self) -> tuple[tuple[str, SourceRecord], ...]:
        """Every gathered record with the registry it came from."""
        return tuple(
            (source_id, record)
            for source_id in sorted(self.gathered)
            for record in self.gathered[source_id]
        )

    def checkpoint_payload(self) -> dict[str, Any]:
        payload = super().checkpoint_payload()
        payload.update(
            {
                "queried": bounded(self.queried),
                "unavailable": bounded(self.unavailable),
                "settled": bounded(self.settled),
                "open_ambiguities": bounded([item.key for item in self.ambiguities]),
            }
        )
        return payload


def bound_address(resolution: Resolution, candidates: tuple[str, ...]) -> str | None:
    """The building a resolution bound, or ``None`` for a decline.

    The membership re-check is deliberate belt-and-braces.
    :func:`firstdue.services.grounding.bind` already refuses an id the caller
    did not offer, and this refuses it again at the point of use -- because the
    consequence of a wrong binding here is a hazard attributed to a building
    that does not have it, and that is worth checking on both sides of the port.
    """
    if not resolution.resolved or resolution.address_id is None:
        return None
    return resolution.address_id if resolution.address_id in candidates else None


def _street_address(record: SourceRecord) -> str:
    return str(record.fields.get("street_address") or "").strip()


def _facility_name(record: SourceRecord) -> str:
    return str(record.fields.get("facility_name") or "").strip()


def detect_ambiguities(
    gathered: Mapping[str, tuple[SourceRecord, ...]],
    *,
    address_ids: tuple[str, ...],
) -> tuple[FacilityAmbiguity, ...]:
    """Every identity the records read so far do not settle between them.

    Pure, and deliberately so: what counts as ambiguous is a rule an officer can
    re-derive by hand from the same rows, not a judgement a model made once.
    """
    unplaced: dict[str, list[SourceRecord]] = {}
    named: dict[str, dict[str, list[SourceRecord]]] = {}

    for source_id in sorted(gathered):
        for record in gathered[source_id]:
            if record.address_id is None:
                street = _street_address(record)
                if street:
                    unplaced.setdefault(street, []).append(record)
                continue
            name = _facility_name(record)
            key = normalize_facility_name(name) if name else ""
            if not key:
                continue
            named.setdefault(record.address_id, {}).setdefault(key, []).append(record)

    found: list[FacilityAmbiguity] = []
    for street in sorted(unplaced):
        rows = unplaced[street]
        found.append(
            FacilityAmbiguity(
                kind="UNPLACED",
                reference=street[:300],
                candidates=address_ids,
                record_refs=tuple(sorted(row.record_ref for row in rows)),
                classification=_highest_classification(rows),
            )
        )
    for address_id in sorted(named):
        for key in sorted(named[address_id]):
            rows = named[address_id][key]
            names = tuple(sorted({_facility_name(row) for row in rows}))
            if len(names) < 2:
                continue
            found.append(
                FacilityAmbiguity(
                    kind="ALIASED",
                    reference=key[:300],
                    candidates=names,
                    address_id=address_id,
                    record_refs=tuple(sorted(row.record_ref for row in rows)),
                    classification=_highest_classification(rows),
                )
            )
    return tuple(found)


def _highest_classification(records: Sequence[SourceRecord]) -> Classification:
    """The strictest handling class among the rows that raised an ambiguity.

    Strictest wins, never an average and never the first one seen. A question
    raised partly by a Tier II filing is a Tier II question, and the bank gates
    recall on this field -- so under-classifying here would hand a confidential
    thread to an agent that does not hold the scope for it.
    """
    for level in (
        Classification.TIER_II_CONFIDENTIAL,
        Classification.PHI,
        Classification.RESTRICTED,
    ):
        if any(record.classification is level for record in records):
            return level
    return Classification.PUBLIC


class HazardCrossCheck:
    """The nodes of the cross-check, bound to one pass's collaborators.

    A class rather than closures because the nodes share the source adapters,
    the planner, and the grounding service, and because the router has to see
    the same budget the driver is charging -- keeping them in one object is what
    stops the two from being wired up differently in the two places that build a
    graph.
    """

    def __init__(
        self,
        *,
        sources: Sequence[SourceAdapter],
        budget: BudgetGuard,
        planner: ReasoningPlanner | None = None,
        grounding: GroundingService | None = None,
    ) -> None:
        self._sources = {source.source_id: source for source in sources}
        self._budget = budget
        self._planner = planner or FixedOrderPlanner()
        self._grounding = grounding

    # -------------------------------------------------------------- helpers

    def _known(self) -> tuple[str, ...]:
        """The hazard registries this pass was actually given, in order."""
        return tuple(source_id for source_id in CROSS_CHECK_ORDER if source_id in self._sources)

    def _untried(self, state: HazardGraphState) -> tuple[str, ...]:
        seen = set(state.queried) | set(state.unavailable)
        return tuple(source_id for source_id in self._known() if source_id not in seen)

    async def _read(self, source_id: str) -> tuple[SourceRecord, ...] | None:
        """Read one registry. ``None`` means it was unreachable, not empty."""
        try:
            snapshot = await self._sources[source_id].fetch()
        except SourceUnavailableError as exc:
            logger.warning(
                "hazard_source_unavailable",
                extra={"source_id": source_id, "error_code": str(exc.code)},
            )
            return None
        return snapshot.records

    def _record_read(
        self, state: HazardGraphState, source_id: str, records: tuple[SourceRecord, ...] | None
    ) -> dict[str, Any]:
        if records is None:
            return {"unavailable": (*state.unavailable, source_id)}
        return {
            "queried": (*state.queried, source_id),
            "gathered": {**state.gathered, source_id: records},
        }

    # ---------------------------------------------------------------- nodes

    async def survey(self, state: HazardGraphState) -> NodeResult:
        """Read the anchor registry. Everything else is a response to it."""
        candidates = self._untried(state)
        if not candidates:
            # No hazard registry was wired into this pass at all. Closing is
            # the honest stop -- there is nothing to read and nothing to ask a
            # person about -- and it is also the only one that terminates: a
            # node that returned no update would be routed straight back here.
            return NodeResult(
                decision="no_registries",
                updates={"stop": GraphStop.CLOSED},
                counts={"registries": 0},
            )
        source_id = candidates[0]
        records = await self._read(source_id)
        return NodeResult(
            decision=f"read:{source_id}",
            updates=self._record_read(state, source_id, records),
            counts={"records": len(records or ()), "registries": len(self._known())},
        )

    async def screen(self, state: HazardGraphState) -> NodeResult:
        """Re-derive which identities are still open, from everything read.

        Runs after every read rather than once, because a registry that answers
        one ambiguity routinely raises another -- a Tier II filing that names an
        occupant the EPA row did not is new evidence and a new question at once.
        """
        found = detect_ambiguities(state.gathered, address_ids=state.address_ids)
        # An identity a previous cross-check settled stays settled. Without
        # this the detector re-derives it from the same rows on every pass
        # through, and the graph loops until it runs out of registries to ask.
        ambiguities = tuple(item for item in found if item.key not in state.settled)
        return NodeResult(
            decision=f"open:{len(ambiguities)}",
            updates={"ambiguities": ambiguities, "screened": len(state.queried)},
            counts={"ambiguous": len(ambiguities), "settled": len(state.settled)},
        )

    async def plan(self, state: HazardGraphState) -> NodeResult:
        """Choose which registry to cross-check against next.

        The planner may reorder the untried registries and may do nothing else:
        it is handed their ids and integer counts, and an answer that is not one
        of them is discarded in favour of the declared order. So the worst a
        planner can do here -- a confused model, a timeout, a wrong guess -- is
        cost this pass one lookup out of order.
        """
        options = self._untried(state)
        if not options:
            return NodeResult(decision="nothing_untried", counts={"untried": 0})
        chosen = await self._planner.choose(
            node=NODE_PLAN,
            options=options,
            counts={
                "ambiguous": len(state.ambiguities),
                "queried": len(state.queried),
                "untried": len(options),
            },
            deadline_ms=PLANNER_DEADLINE_MS,
        )
        picked = chosen if chosen in options else options[0]
        return NodeResult(
            decision=f"plan:{picked}",
            updates={"chosen": picked},
            counts={"untried": len(options), "ambiguous": len(state.ambiguities)},
        )

    async def cross_check(self, state: HazardGraphState) -> NodeResult:
        """Read the chosen registry and try every open identity against it.

        A registry that names one of the candidates settles the question with no
        model involved at all; only what survives that gets put to the grounding
        service, and only against the same closed list. Whatever this registry
        failed to settle is written into ``ruled_out`` under its own id, which
        is the part that makes the next pass cheaper than this one.
        """
        source_id = state.chosen
        records = await self._read(source_id)
        updates = self._record_read(state, source_id, records)
        updates["chosen"] = ""

        gathered = dict(state.gathered)
        if records is not None:
            gathered[source_id] = records

        settled: list[str] = list(state.settled)
        ruled_out: list[str] = list(state.ruled_out)
        for ambiguity in state.ambiguities:
            answer = self._settle_locally(ambiguity, records or ())
            if answer is None:
                answer = await self._settle_by_grounding(ambiguity, district_id=state.district_id)
            if answer is None:
                token = f"{source_id}/{ambiguity.key}"
                if token not in ruled_out:
                    ruled_out.append(token)
                continue
            settled.append(ambiguity.key)
            if ambiguity.kind == "UNPLACED":
                gathered = _bind_street(gathered, ambiguity, answer)

        updates["gathered"] = gathered
        updates["settled"] = tuple(settled)
        updates["ruled_out"] = tuple(ruled_out)
        return NodeResult(
            decision=f"cross_check:{source_id}",
            updates=updates,
            counts={
                "records": len(records or ()),
                "settled": len(settled) - len(state.settled),
                "ruled_out": len(ruled_out),
            },
        )

    async def sweep(self, state: HazardGraphState) -> NodeResult:
        """Read every registry the cross-check did not need. Then close.

        This is the node that makes the graph safe to put in front of this
        particular agent. A hazard registry that goes unread produces neither a
        fact nor an ``UNAVAILABLE``, and an officer reading the profile cannot
        tell that from a registry that had nothing to say.
        """
        gathered = dict(state.gathered)
        queried = list(state.queried)
        unavailable = list(state.unavailable)
        for source_id in self._untried(state):
            records = await self._read(source_id)
            if records is None:
                unavailable.append(source_id)
                continue
            queried.append(source_id)
            gathered[source_id] = records
        return NodeResult(
            decision=f"swept:{len(queried) - len(state.queried)}",
            updates={
                "gathered": gathered,
                "queried": tuple(queried),
                "unavailable": tuple(unavailable),
                "stop": GraphStop.CLOSED,
            },
            counts={"registries": len(queried), "unavailable": len(unavailable)},
        )

    async def park(self, state: HazardGraphState) -> NodeResult:
        """Stop, and say what is unfinished. The agent does the writing.

        Persisting is the agent's job rather than a node's because the node has
        no clock and no memory bank -- and because ``park`` must be reachable in
        a process that wired neither, which is every process running today.
        """
        stop = self._budget.exhausted() or GraphStop.UNRESOLVED
        waiting = state.ambiguities[0].waiting_on if state.ambiguities else "a hazard registry read"
        return NodeResult(
            decision=f"park:{stop}",
            updates={"stop": stop, "waiting_on": waiting[:200]},
            counts={"ambiguous": len(state.ambiguities), "ruled_out": len(state.ruled_out)},
        )

    # --------------------------------------------------------------- router

    def route(self, state: HazardGraphState) -> str:
        """Where the graph goes next. Pure, and the only place the budget bites.

        Both ceilings are checked here, before anything else, so an exhausted
        graph parks rather than starting one more registry read it cannot
        finish -- and so the bound holds identically under either driver.
        """
        if state.stop is not None:
            return STOP
        if self._budget.exhausted() is not None:
            return NODE_PARK
        if not state.queried and not state.unavailable:
            return NODE_SURVEY
        if state.chosen:
            return NODE_CROSS_CHECK
        if state.screened != len(state.queried):
            return NODE_SCREEN
        if state.ambiguities:
            return NODE_PLAN if self._untried(state) else NODE_PARK
        return NODE_SWEEP

    def spec(self) -> GraphSpec[HazardGraphState]:
        return GraphSpec(
            state_type=HazardGraphState,
            entry=NODE_SURVEY,
            nodes={
                NODE_SURVEY: self.survey,
                NODE_SCREEN: self.screen,
                NODE_PLAN: self.plan,
                NODE_CROSS_CHECK: self.cross_check,
                NODE_SWEEP: self.sweep,
                NODE_PARK: self.park,
            },
            router=self.route,
        )

    # ------------------------------------------------------------ internals

    def _settle_locally(
        self, ambiguity: FacilityAmbiguity, records: tuple[SourceRecord, ...]
    ) -> str | None:
        """Settle one identity from the registry itself, with no model at all.

        Preferred over the grounding service wherever it works: a second
        registry naming exactly one of the candidate spellings, or placing the
        street this row named, is evidence rather than an opinion about
        evidence.
        """
        if ambiguity.kind == "UNPLACED":
            for record in records:
                if (
                    record.address_id in ambiguity.candidates
                    and _street_address(record).lower() == ambiguity.reference.lower()
                ):
                    return record.address_id
            return None

        matched = {
            _facility_name(record)
            for record in records
            if record.address_id == ambiguity.address_id
            and _facility_name(record) in ambiguity.candidates
        }
        return matched.pop() if len(matched) == 1 else None

    async def _settle_by_grounding(
        self, ambiguity: FacilityAmbiguity, *, district_id: str
    ) -> str | None:
        """Ask the grounding service which building a stray registry row is about.

        ``UNPLACED`` only, and that is not an oversight.
        :meth:`~firstdue.ports.grounding.GroundingService.resolve_reference`
        answers exactly one question -- which of these building ids does this
        text point at -- and :class:`~firstdue.ports.grounding.Resolution` has
        no field that could carry anything else. Putting filed *names* to it as
        candidates would be asking it a question its return type cannot express,
        so an ``ALIASED`` identity is settled by another registry or by a person.
        """
        if self._grounding is None or ambiguity.kind != "UNPLACED" or not ambiguity.candidates:
            return None
        # Never raises, by contract: every failure is a decline. The try is
        # here for a third-party implementation that breaks that contract, and
        # an unresolved identity is a question rather than an outage either way.
        try:
            resolution = await self._grounding.resolve_reference(
                ambiguity.reference,
                district_id=district_id,
                candidates=ambiguity.candidates,
                deadline_ms=GROUNDING_DEADLINE_MS,
            )
        except Exception as exc:  # pragma: no cover - the port forbids this
            logger.warning("hazard_grounding_unavailable", extra={"error_type": type(exc).__name__})
            return None
        return bound_address(resolution, ambiguity.candidates)


def _bind_street(
    gathered: Mapping[str, tuple[SourceRecord, ...]],
    ambiguity: FacilityAmbiguity,
    address_id: str,
) -> dict[str, tuple[SourceRecord, ...]]:
    """Attach every row that named this street to the building it turned out to be.

    Binding is not authoring. The row keeps its own fields, its own timestamp,
    its own classification and its own ``record_ref``; all that changes is that
    the pass now knows which building the registry was talking about, and the
    ordinary deterministic path takes it from there.
    """
    bound: dict[str, tuple[SourceRecord, ...]] = {}
    for source_id, records in gathered.items():
        bound[source_id] = tuple(
            record.model_copy(update={"address_id": address_id})
            if record.address_id is None and _street_address(record) == ambiguity.reference
            else record
            for record in records
        )
    return bound


__all__ = [
    "CROSS_CHECK_ORDER",
    "NODE_CROSS_CHECK",
    "NODE_PLAN",
    "NODE_SCREEN",
    "NODE_SURVEY",
    "GROUNDING_DEADLINE_MS",
    "NODE_SWEEP",
    "FacilityAmbiguity",
    "HazardCrossCheck",
    "HazardGraphState",
    "bound_address",
    "detect_ambiguities",
    "normalize_facility_name",
]
