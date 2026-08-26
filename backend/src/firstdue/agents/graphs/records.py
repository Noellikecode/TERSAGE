"""The records watcher's retrieval graph -- reading until the picture closes.

A fixed pass over four feeds reads what those four feeds happened to publish
today. That is fine until a filing points somewhere: an alteration permit that
supersedes an earlier one, a violation that cites the inspection that found it.
The cited record is the one that says what changed, and a watcher that only
walks its own list will not read it this week, next week, or ever -- because
nothing in the fixed pass is ever surprised.

So this graph retrieves rather than polls. It decides which feed to pull next,
reads the references the filings themselves cite, goes looking for the cited
record, and stops when nothing is left outstanding. A reference it cannot find
becomes an open question naming the permit number it is waiting on and the
feeds it already searched, which is the difference between an agent that waits
weeks for a record to be published and one that re-fails on it every night.

**References are read from filed columns, never from prose.** A permit's
``prior_permit`` column is a filing and reading it needs no judgement, exactly
like the columns in ``FIELD_MAPS``. Document text stays on the other side of
the screen: this graph never opens it, and the extractor -- which does -- gets
it screened, as it always has. A graph that took its next lookup from a
narrative would be taking instructions from ingested text, which is the whole
attack ``security/armor.py`` exists to stop.

**Retrieval does not touch extraction.** Everything this graph produces is a
set of :class:`~firstdue.ports.sources.SourceSnapshot` objects. They go to the
same :class:`~firstdue.extraction.extractor.FactExtractor`, in the same order,
under the same screen, and the facts that come out carry the same spans and
provenance they always did. The graph changed which filings were read; it
cannot change what any of them says.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from pydantic import Field

from firstdue.agents.graphs.base import (
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
from firstdue.errors import SourceUnavailableError
from firstdue.observability.logging import get_logger
from firstdue.ports.sources import SourceAdapter, SourceRecord, SourceSnapshot
from firstdue.sources.catalog import ASSESSOR, INSPECTIONS, PERMITS, VIOLATIONS

logger = get_logger(__name__)

NODE_PLAN: Final[str] = "plan"
NODE_FETCH: Final[str] = "fetch"
NODE_FOLLOW: Final[str] = "follow"
NODE_CHASE: Final[str] = "chase"
NODE_CLOSE: Final[str] = "close"

#: The order retrieval considers the feeds, and the planner's default.
#:
#: Permits first because a permit is the filing that cites other filings, so
#: reading it first is what gives the rest of the pass something to follow.
#: Violations next because a violation cites the inspection behind it. The
#: assessor's roll cites nothing and is read for its columns alone, so it is
#: last and is never worth reordering.
RETRIEVAL_ORDER: Final[tuple[str, ...]] = (PERMITS, VIOLATIONS, INSPECTIONS, ASSESSOR)

#: Columns in which a filing names another filing. Structured columns only --
#: see the module docstring on why a narrative is not one of these.
REFERENCE_FIELDS: Final[tuple[str, ...]] = (
    "prior_permit",
    "prior_permit_number",
    "parent_permit",
    "related_permit",
    "supersedes",
    "references",
    "related_inspection",
    "source_inspection",
)

#: Columns by which a filing names *itself*, so a chased reference can be
#: recognised when it arrives.
IDENTITY_FIELDS: Final[tuple[str, ...]] = (
    "permit_number",
    "inspection_id",
    "case_number",
    "complaint_number",
)

#: How many outstanding references one pass will chase. A filing chain longer
#: than this is a records dispute, not a retrieval problem, and the open
#: question is a better answer to it than another twenty lookups.
MAX_REFERENCES: Final[int] = 12


def _identity_of(record: SourceRecord) -> tuple[str, ...]:
    """Every id a filing publishes for itself."""
    return tuple(
        str(record.fields[column]).strip()
        for column in IDENTITY_FIELDS
        if record.fields.get(column) not in (None, "")
    )


def references_in(record: SourceRecord) -> tuple[str, ...]:
    """Every other filing this one names, from its filed columns.

    A column holding a list names several; a column holding a scalar names one.
    Both shapes occur in municipal exports and neither is worth a branch at the
    call site.
    """
    found: list[str] = []
    for column in REFERENCE_FIELDS:
        raw = record.fields.get(column)
        if raw in (None, ""):
            continue
        values = raw if isinstance(raw, list | tuple) else [raw]
        found.extend(str(value).strip() for value in values if str(value).strip())
    return tuple(dict.fromkeys(found))


class RecordsGraphState(GraphState):
    """What retrieval knows. Snapshots live here and are handed on whole."""

    since: datetime | None = None
    #: Feeds pulled this pass, in the order they were pulled.
    queried: tuple[str, ...] = ()
    #: Feeds that raised. Reported unchanged -- a feed that was down is not a
    #: district with no filings.
    unavailable: tuple[str, ...] = ()
    #: Every snapshot retrieved, in retrieval order. Handed to the extractor
    #: exactly as a fixed pass would have handed it.
    snapshots: tuple[SourceSnapshot, ...] = ()
    #: Filing ids cited by something read this pass and not yet found.
    outstanding: tuple[str, ...] = ()
    #: Filing ids cited and then found. Counted, so the trace shows the chase
    #: actually closed something rather than merely ran.
    followed: tuple[str, ...] = ()
    #: How many snapshots had been read the last time ``follow`` ran.
    followed_through: int = 0
    #: The feed ``plan`` chose and ``fetch`` has yet to read.
    chosen: str = Field(default="", max_length=120)

    @property
    def records(self) -> tuple[SourceRecord, ...]:
        return tuple(record for snapshot in self.snapshots for record in snapshot.records)

    def checkpoint_payload(self) -> dict[str, Any]:
        payload = super().checkpoint_payload()
        payload.update(
            {
                "queried": bounded(self.queried),
                "unavailable": bounded(self.unavailable),
                "outstanding": bounded(self.outstanding),
                "followed": len(self.followed),
                "snapshots": len(self.snapshots),
            }
        )
        return payload


class RecordsRetrieval:
    """The nodes of agentic retrieval, bound to one pass's collaborators."""

    def __init__(
        self,
        *,
        sources: Sequence[SourceAdapter],
        budget: BudgetGuard,
        planner: ReasoningPlanner | None = None,
        max_pages: int = 50,
    ) -> None:
        self._sources = {source.source_id: source for source in sources}
        self._budget = budget
        self._planner = planner or FixedOrderPlanner()
        self._max_pages = max_pages
        #: Which feed each snapshot came from, so the agent can extract with
        #: the right field map. Keyed by snapshot id, which is unique per pull.
        self.origins: dict[str, str] = {}

    # -------------------------------------------------------------- helpers

    def _known(self) -> tuple[str, ...]:
        return tuple(source_id for source_id in RETRIEVAL_ORDER if source_id in self._sources)

    def _untried(self, state: RecordsGraphState) -> tuple[str, ...]:
        seen = set(state.queried) | set(state.unavailable)
        return tuple(source_id for source_id in self._known() if source_id not in seen)

    async def _pull(
        self,
        source_id: str,
        *,
        since: datetime | None,
        address_id: str | None = None,
    ) -> tuple[SourceSnapshot, ...] | None:
        """Read one feed to the end of its pagination. ``None`` means it raised.

        The page bound is the one the fixed pass already used, kept because it
        is the difference between a slow feed and an unbounded one.
        """
        snapshots: list[SourceSnapshot] = []
        cursor: str | None = None
        try:
            for _ in range(self._max_pages):
                snapshot = await self._sources[source_id].fetch(
                    address_id=address_id, since=since, cursor=cursor
                )
                snapshots.append(snapshot)
                cursor = snapshot.next_cursor
                if cursor is None:
                    break
        except SourceUnavailableError as exc:
            logger.warning(
                "watcher_source_unavailable",
                extra={"source_id": source_id, "error_code": str(exc.code)},
            )
            return None
        for snapshot in snapshots:
            self.origins[snapshot.snapshot_id] = source_id
        return tuple(snapshots)

    # ---------------------------------------------------------------- nodes

    async def plan(self, state: RecordsGraphState) -> NodeResult:
        """Choose the next feed to read.

        The planner sees feed ids and counts -- how many references are
        outstanding, how many feeds are left -- and may return one of the feeds
        or nothing. It cannot name a feed that was not offered and cannot ask
        for a record. Getting this wrong reorders the pass; it cannot change it.
        """
        options = self._untried(state)
        if not options:
            return NodeResult(decision="nothing_untried", counts={"untried": 0})
        chosen = await self._planner.choose(
            node=NODE_PLAN,
            options=options,
            counts={
                "outstanding": len(state.outstanding),
                "queried": len(state.queried),
                "untried": len(options),
            },
            deadline_ms=PLANNER_DEADLINE_MS,
        )
        picked = chosen if chosen in options else options[0]
        return NodeResult(
            decision=f"plan:{picked}",
            updates={"chosen": picked},
            counts={"untried": len(options), "outstanding": len(state.outstanding)},
        )

    async def fetch(self, state: RecordsGraphState) -> NodeResult:
        """Read the chosen feed. Unreachable is reported, never rendered empty."""
        source_id = state.chosen
        snapshots = await self._pull(source_id, since=state.since)
        if snapshots is None:
            return NodeResult(
                decision=f"unavailable:{source_id}",
                updates={"chosen": "", "unavailable": (*state.unavailable, source_id)},
                counts={"records": 0},
            )
        records = sum(len(snapshot.records) for snapshot in snapshots)
        return NodeResult(
            decision=f"fetch:{source_id}",
            updates={
                "chosen": "",
                "queried": (*state.queried, source_id),
                "snapshots": (*state.snapshots, *snapshots),
            },
            counts={"records": records, "pages": len(snapshots)},
        )

    async def follow(self, state: RecordsGraphState) -> NodeResult:
        """Re-derive what is cited but not yet held.

        Pure set arithmetic over filed columns: every id anything cites, minus
        every id anything published for itself. Runs after each fetch because a
        feed read late can satisfy a reference raised early, and re-deriving is
        cheaper and more obviously correct than maintaining the difference.
        """
        published = {identity for record in state.records for identity in _identity_of(record)}
        cited = {reference for record in state.records for reference in references_in(record)}
        outstanding = tuple(sorted(cited - published))[:MAX_REFERENCES]
        followed = tuple(sorted((cited & published) | set(state.followed)))
        return NodeResult(
            decision=f"outstanding:{len(outstanding)}",
            updates={
                "outstanding": outstanding,
                "followed": followed,
                "followed_through": len(state.snapshots),
            },
            counts={
                "cited": len(cited),
                "outstanding": len(outstanding),
                "followed": len(followed),
            },
        )

    async def chase(self, state: RecordsGraphState) -> NodeResult:
        """Go and look for one cited filing that nothing has published yet.

        A targeted read of the feed that cited it, over the whole feed rather
        than the incremental window -- which is a genuine lookup and involves no
        model at all. There is deliberately no model fallback here: a permit
        number is either published or it is not, and the only honest answer to
        "we cannot find it" is to say so and wait.

        Failing that, the reference stays outstanding and the feed tried goes
        into ``ruled_out``. That record is the point: an unpublished permit does
        not become published because an agent asked again tonight.
        """
        reference = self._next_chase(state)
        if not reference:
            return NodeResult(decision="nothing_to_chase", counts={"outstanding": 0})

        citing = self._citing_source(state, reference)
        # ``since=None`` on purpose: the incremental window is what hid this
        # filing in the first place. A reference is chased across the whole
        # feed or not at all.
        snapshots = await self._pull(citing, since=None) if citing else None
        found = any(
            reference in _identity_of(record)
            for snapshot in snapshots or ()
            for record in snapshot.records
        )

        ruled_out = list(state.ruled_out)
        updates: dict[str, Any] = {}
        if found and snapshots:
            # Only kept when the chase paid: a re-read that turned up nothing
            # new would put the same records through the extractor twice.
            updates["snapshots"] = (*state.snapshots, *snapshots)
        if found:
            updates["followed"] = tuple(sorted({*state.followed, reference}))
            updates["outstanding"] = tuple(r for r in state.outstanding if r != reference)
        else:
            # Left outstanding, but marked tried: the router chases only what
            # is outstanding *and* not already ruled out, so an unpublished
            # filing is asked about once and then waited on. Asking again
            # tonight will not publish it.
            token = f"{citing or 'no-feed'}/{reference}"
            if token not in ruled_out:
                ruled_out.append(token)
            updates["ruled_out"] = tuple(ruled_out)
        return NodeResult(
            decision=f"chase:{'found' if found else 'missing'}",
            updates=updates,
            counts={"outstanding": len(state.outstanding), "ruled_out": len(ruled_out)},
        )

    async def park(self, state: RecordsGraphState) -> NodeResult:
        """Stop, and name what the next pass is waiting for."""
        stop = self._budget.exhausted() or GraphStop.UNRESOLVED
        waiting = (
            f"filing {state.outstanding[0]}" if state.outstanding else "a municipal feed to publish"
        )
        return NodeResult(
            decision=f"park:{stop}",
            updates={"stop": stop, "waiting_on": waiting[:200]},
            counts={"outstanding": len(state.outstanding), "ruled_out": len(state.ruled_out)},
        )

    async def close(self, state: RecordsGraphState) -> NodeResult:
        """Nothing cited is missing and every feed has been read. Done."""
        return NodeResult(
            decision="closed",
            updates={"stop": GraphStop.CLOSED},
            counts={
                "feeds": len(state.queried),
                "snapshots": len(state.snapshots),
                "followed": len(state.followed),
            },
        )

    # --------------------------------------------------------------- router

    def route(self, state: RecordsGraphState) -> str:
        """Where retrieval goes next. Pure, and where both ceilings bite."""
        if state.stop is not None:
            return STOP
        if self._budget.exhausted() is not None:
            return NODE_PARK
        if state.chosen:
            return NODE_FETCH
        if state.followed_through != len(state.snapshots):
            return NODE_FOLLOW
        if self._untried(state):
            return NODE_PLAN
        if self._chaseable(state):
            return NODE_CHASE
        if state.outstanding:
            # Every feed read, every outstanding reference already chased and
            # not found. That is a question for a person, not another lookup.
            return NODE_PARK
        return NODE_CLOSE

    def _chaseable(self, state: RecordsGraphState) -> bool:
        return bool(self._next_chase(state))

    def _next_chase(self, state: RecordsGraphState) -> str:
        """The first outstanding reference this pass has not already tried."""
        for reference in state.outstanding:
            if not any(token.endswith(f"/{reference}") for token in state.ruled_out):
                return reference
        return ""

    def spec(self) -> GraphSpec[RecordsGraphState]:
        return GraphSpec(
            state_type=RecordsGraphState,
            entry=NODE_PLAN,
            nodes={
                NODE_PLAN: self.plan,
                NODE_FETCH: self.fetch,
                NODE_FOLLOW: self.follow,
                NODE_CHASE: self.chase,
                NODE_CLOSE: self.close,
                NODE_PARK: self.park,
            },
            router=self.route,
        )

    # ------------------------------------------------------------ internals

    def _citing_source(self, state: RecordsGraphState, reference: str) -> str:
        """Which feed is most likely to hold a filing with this id.

        The feed whose records cited it, because a permit that names a prior
        permit is naming one from its own system. Falls back to the first feed
        this pass was given, which is where a bare filing number most often
        lives.
        """
        for snapshot in state.snapshots:
            for record in snapshot.records:
                if reference in references_in(record):
                    origin = self.origins.get(snapshot.snapshot_id, "")
                    if origin in self._sources:
                        return origin
        known = self._known()
        return known[0] if known else ""


__all__ = [
    "IDENTITY_FIELDS",
    "MAX_REFERENCES",
    "NODE_CHASE",
    "NODE_CLOSE",
    "NODE_FETCH",
    "NODE_FOLLOW",
    "NODE_PLAN",
    "REFERENCE_FIELDS",
    "RETRIEVAL_ORDER",
    "RecordsGraphState",
    "RecordsRetrieval",
    "references_in",
]
