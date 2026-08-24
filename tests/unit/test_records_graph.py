"""The records watcher's retrieval graph -- following filings, and stopping.

As with the hazard graph, everything here runs whether or not LangGraph is
installed; one test compiles the same node set into a real ``StateGraph`` and
asserts the chain is identical, and only that one skips. The nodes are the thing
under test, not the framework running them.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.clock import DeterministicIdGenerator
from firstdue.adapters.memory.memory_bank import (
    InMemoryCheckpointRepository,
    InMemoryOpenQuestionRepository,
)
from firstdue.adapters.memory.repositories import (
    InMemoryConflictRepository,
    InMemoryFactRepository,
    InMemoryLockRepository,
    InMemoryProfileRepository,
)
from firstdue.agents.graphs.base import (
    DEFAULT_MAX_STEPS,
    STOP,
    BudgetGuard,
    GraphCassette,
    GraphStop,
    run_graph,
)
from firstdue.agents.graphs.records import (
    NODE_CHASE,
    NODE_PLAN,
    RETRIEVAL_ORDER,
    RecordsGraphState,
    RecordsRetrieval,
    references_in,
)
from firstdue.agents.records_watcher import AGENT_ID, RecordsWatcher
from firstdue.domain.enums import Classification, Scope, SourceType
from firstdue.domain.profiles import BuildingProfile
from firstdue.errors import SourceUnavailableError
from firstdue.extraction.extractor import FactExtractor
from firstdue.observability.tracing import TRACER
from firstdue.ports.city import NormalizedAddress
from firstdue.ports.grounding import Resolution
from firstdue.ports.sources import SourceHealth, SourceRecord, SourceSnapshot
from firstdue.services.materialization import ProfileMaterializer
from firstdue.services.memory_bank import MemoryBank
from firstdue.sources.catalog import ASSESSOR, INSPECTIONS, PERMITS, VIOLATIONS

DISTRICT = "sffd-district-03"
HAYES = "sf-0450-hayes"
OBSERVED = datetime(2026, 3, 1, tzinfo=UTC)

#: The prose on a permit. A permit narrative is a citizen's document; if any of
#: it reaches a span, the span is carrying one.
NARRATIVE = "Alteration to an existing 2-storey wood-frame dwelling at 450 Hayes St."

PUBLIC_READER = frozenset({Scope.READ_PUBLIC_RECORDS})


# --------------------------------------------------------------- test doubles


class _Feed:
    """One municipal feed, backed by a list rather than an open-data platform."""

    def __init__(
        self,
        source_id: str,
        records: Sequence[SourceRecord] = (),
        *,
        unavailable: bool = False,
        archive: Sequence[SourceRecord] | None = None,
    ) -> None:
        self._source_id = source_id
        self._records = tuple(records)
        # What a read of the *whole* feed turns up, as opposed to the
        # incremental window. This is what a chase is actually for.
        self._archive = tuple(archive) if archive is not None else None
        self._unavailable = unavailable
        self.fetches = 0
        self.full_reads = 0

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> SourceType:
        return SourceType.PERMIT

    @property
    def classification(self) -> Classification:
        return Classification.PUBLIC

    async def fetch(self, *, address_id=None, since=None, cursor=None) -> SourceSnapshot:
        self.fetches += 1
        if self._unavailable:
            raise SourceUnavailableError("feed down", details={"source_id": self._source_id})
        records = self._records
        if since is None and self._archive is not None:
            self.full_reads += 1
            records = self._archive
        return SourceSnapshot(
            source_id=self._source_id,
            snapshot_id=f"{self._source_id}:snap:{self.fetches}",
            fetched_at=OBSERVED,
            records=records,
        )

    async def health(self) -> SourceHealth:  # pragma: no cover - not exercised
        raise NotImplementedError


class _City:
    """A gazetteer that knows one street and refuses everything else."""

    municipality_id = "test-city"
    default_jurisdiction_id = "test-jurisdiction"

    def normalize_address(self, raw: str) -> NormalizedAddress | None:
        if "hayes" not in raw.lower():
            return None
        return NormalizedAddress(
            address_id=HAYES,
            display="450 Hayes St",
            district_id=DISTRICT,
            jurisdiction_id=self.default_jurisdiction_id,
            latitude=37.77,
            longitude=-122.42,
        )

    def get_address(self, address_id: str) -> NormalizedAddress | None:
        return self.normalize_address("hayes") if address_id == HAYES else None

    def list_districts(self) -> Sequence[str]:
        return (DISTRICT,)

    def source_ids(self) -> Sequence[str]:
        return RETRIEVAL_ORDER


class _AlwaysBinds:
    """A grounding service that binds the first candidate it is offered."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve_reference(
        self, reference: str, *, district_id: str, candidates: tuple[str, ...], deadline_ms: int
    ) -> Resolution:
        self.calls.append(reference)
        return Resolution(
            resolved=True,
            address_id=candidates[0],
            confidence=0.9,
            evidence=("test://binding",),
            method="test-grounding/1",
        )


def _permit(
    number: str,
    *,
    ref: str | None = None,
    cites: str | None = None,
    address: str = "450 Hayes St",
    document_text: str | None = NARRATIVE,
) -> SourceRecord:
    fields: dict[str, object] = {
        "permit_number": number,
        "street_address": address,
        "stories_filed": 2,
    }
    if cites is not None:
        fields["prior_permit"] = cites
    return SourceRecord(
        record_ref=ref or f"permit/{number}",
        address_id=None,
        classification=Classification.PUBLIC,
        fields=fields,
        document_text=document_text,
        observed_at=OBSERVED,
    )


def _budget(*, seconds: float = 60.0, max_steps: int = DEFAULT_MAX_STEPS, elapsed=None):
    """A budget whose clock the test controls; see the hazard graph suite."""
    if elapsed is None:
        return BudgetGuard(seconds=seconds, max_steps=max_steps, monotonic=lambda: 0.0)
    readings = iter(elapsed)
    return BudgetGuard(
        seconds=seconds, max_steps=max_steps, monotonic=lambda: next(readings, elapsed[-1])
    )


async def _run(retrieval: RecordsRetrieval, *, budget: BudgetGuard, use_langgraph: bool = False):
    return await run_graph(
        retrieval.spec(),
        RecordsGraphState(district_id=DISTRICT),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        budget=budget,
        request_digest="test-digest",
        use_langgraph=use_langgraph,
    )


def _feeds(permits: Sequence[SourceRecord] = (), **overrides) -> list[_Feed]:
    """The four watched feeds, with only the permit feed carrying anything."""
    return [
        overrides.get(PERMITS, _Feed(PERMITS, permits)),
        overrides.get(VIOLATIONS, _Feed(VIOLATIONS)),
        overrides.get(INSPECTIONS, _Feed(INSPECTIONS)),
        overrides.get(ASSESSOR, _Feed(ASSESSOR)),
    ]


@pytest.fixture
def bank(clock) -> MemoryBank:
    return MemoryBank(
        questions=InMemoryOpenQuestionRepository(),
        checkpoints=InMemoryCheckpointRepository(),
        clock=clock,
    )


@pytest.fixture
async def watcher_parts(clock, ids):
    """Everything a RecordsWatcher needs, with one profile already on file."""
    profiles = InMemoryProfileRepository()
    await profiles.create(BuildingProfile(address_id=HAYES, district_id=DISTRICT))
    facts = InMemoryFactRepository()
    materializer = ProfileMaterializer(
        profiles=profiles,
        conflicts=InMemoryConflictRepository(),
        locks=InMemoryLockRepository(),
        clock=clock,
        ids=ids,
    )
    extractor = FactExtractor(ids=DeterministicIdGenerator("records-graph"), model=None)
    return profiles, facts, materializer, extractor


def _watcher(parts, clock, ids, **overrides) -> RecordsWatcher:
    profiles, facts, materializer, extractor = parts
    return RecordsWatcher(
        profiles=profiles,
        facts=facts,
        city=_City(),
        extractor=extractor,
        materializer=materializer,
        clock=clock,
        ids=ids,
        use_langgraph=False,
        **overrides,
    )


@pytest.fixture
def recording_spans() -> Iterator[None]:
    TRACER.configure(enabled=False, record_spans=True)
    TRACER.clear()
    yield
    TRACER.clear()
    TRACER.configure(enabled=False, record_spans=False)


# --------------------------------------------------------- reading references


def test_a_reference_is_read_from_a_filed_column() -> None:
    assert references_in(_permit("2019-1", cites="2018-04871")) == ("2018-04871",)


def test_a_narrative_is_not_a_reference() -> None:
    """Taking the next lookup from prose would be taking it from ingested text."""
    prose = _permit("2019-1", document_text="See prior permit 2018-04871 for the stair.")

    assert references_in(prose) == ()


def test_a_column_holding_a_list_names_several() -> None:
    record = _permit("2019-1")
    record = record.model_copy(update={"fields": {**record.fields, "references": ["a-1", "b-2"]}})

    assert references_in(record) == ("a-1", "b-2")


# ------------------------------------------------------------------ the loop


async def test_retrieval_closes_when_nothing_is_outstanding() -> None:
    retrieval = RecordsRetrieval(sources=_feeds([_permit("2019-1")]), budget=_budget())

    run = await _run(retrieval, budget=_budget())

    assert run.trace.stop is GraphStop.CLOSED
    assert set(run.state.queried) == set(RETRIEVAL_ORDER)
    assert run.state.outstanding == ()


async def test_a_cited_permit_already_in_the_window_needs_no_chase() -> None:
    permits = [_permit("2019-1", cites="2018-04871"), _permit("2018-04871")]
    retrieval = RecordsRetrieval(sources=_feeds(permits), budget=_budget())

    run = await _run(retrieval, budget=_budget())

    assert run.trace.stop is GraphStop.CLOSED
    assert run.state.followed == ("2018-04871",)
    assert NODE_CHASE not in run.trace.node_sequence


async def test_a_cited_permit_outside_the_window_is_chased_and_found() -> None:
    """The incremental window is what hid it. The chase reads past the window."""
    archive = _Feed(
        PERMITS,
        [_permit("2019-1", cites="2018-04871")],
        archive=[_permit("2019-1", cites="2018-04871"), _permit("2018-04871")],
    )
    retrieval = RecordsRetrieval(sources=_feeds(**{PERMITS: archive}), budget=_budget())

    run = await run_graph(
        retrieval.spec(),
        RecordsGraphState(district_id=DISTRICT, since=OBSERVED),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        budget=_budget(),
        request_digest="test-digest",
        use_langgraph=False,
    )

    assert NODE_CHASE in run.trace.node_sequence
    assert archive.full_reads == 1
    assert run.state.followed == ("2018-04871",)
    assert run.trace.stop is GraphStop.CLOSED


async def test_a_permit_nobody_has_published_is_asked_about_once_and_then_waited_on() -> None:
    """Asking again tonight will not publish it. Re-asking is the thing to stop."""
    permits = _Feed(PERMITS, [_permit("2019-1", cites="201804-3321")])
    retrieval = RecordsRetrieval(sources=_feeds(**{PERMITS: permits}), budget=_budget())

    run = await _run(retrieval, budget=_budget())

    assert run.trace.stop is GraphStop.UNRESOLVED
    assert run.state.outstanding == ("201804-3321",)
    assert run.state.ruled_out == (f"{PERMITS}/201804-3321",)
    # Chased exactly once, not once per pass through the router.
    assert sum(1 for node in run.trace.node_sequence if node == NODE_CHASE) == 1


async def test_a_feed_that_is_down_is_reported_and_never_rendered_empty() -> None:
    retrieval = RecordsRetrieval(
        sources=_feeds(**{VIOLATIONS: _Feed(VIOLATIONS, unavailable=True)}), budget=_budget()
    )

    run = await _run(retrieval, budget=_budget())

    assert run.state.unavailable == (VIOLATIONS,)
    assert VIOLATIONS not in run.state.queried


# ------------------------------------------------------------- the ceilings


async def test_the_step_bound_stops_retrieval() -> None:
    budget = _budget(max_steps=2)
    retrieval = RecordsRetrieval(sources=_feeds([_permit("2019-1")]), budget=budget)

    run = await _run(retrieval, budget=budget)

    assert run.trace.stop is GraphStop.OUT_OF_STEPS
    assert len(run.trace.records) <= 2 + 1


async def test_running_out_of_time_parks_rather_than_raising() -> None:
    budget = _budget(seconds=5.0, elapsed=[0.0, 99.0])
    retrieval = RecordsRetrieval(sources=_feeds([_permit("2019-1")]), budget=budget)

    run = await _run(retrieval, budget=budget)

    assert run.trace.stop is GraphStop.OUT_OF_TIME
    assert run.trace.node_sequence[0] == NODE_PLAN
    assert run.trace.node_sequence[-1] == "park"


async def test_the_router_stops_a_parked_graph() -> None:
    retrieval = RecordsRetrieval(sources=_feeds(), budget=_budget())
    parked = RecordsGraphState(district_id=DISTRICT, stop=GraphStop.OUT_OF_STEPS)

    assert retrieval.route(parked) == STOP


# ---------------------------------------------------- the agent, end to end


async def test_an_unpublished_reference_opens_a_thread_with_what_was_ruled_out(
    watcher_parts, bank, clock, ids
) -> None:
    watcher = _watcher(watcher_parts, clock, ids, memory=bank)

    result = await watcher.poll(
        district_id=DISTRICT,
        sources=_feeds([_permit("2019-1", cites="201804-3321")]),
        correlation_id="corr-1",
    )

    assert result.references_outstanding == 1
    assert result.open_question_ids

    carried = await bank.recall(district_id=DISTRICT, scopes=PUBLIC_READER)
    assert len(carried) == 1
    assert "201804-3321" in carried[0].question
    assert carried[0].ruled_out == (f"{PERMITS}/201804-3321",)
    # And the position is durable, so the next pass resumes rather than restarts.
    assert await bank.resume(carried[0].question_id, scopes=PUBLIC_READER) is not None


async def test_budget_exhaustion_opens_a_question_rather_than_failing(
    watcher_parts, bank, clock, ids
) -> None:
    watcher = _watcher(watcher_parts, clock, ids, memory=bank, max_graph_steps=1)

    result = await watcher.poll(
        district_id=DISTRICT,
        sources=_feeds([_permit("2019-1")]),
        correlation_id="corr-1",
        deadline=clock.now() - timedelta(seconds=1),
    )

    assert result.graph_stop in {str(GraphStop.OUT_OF_TIME), str(GraphStop.OUT_OF_STEPS)}
    assert result.open_question_ids
    carried = await bank.recall(district_id=DISTRICT, scopes=PUBLIC_READER)
    assert carried[0].waiting_on.startswith("a full read of:")


async def test_a_second_stuck_pass_carries_one_thread_not_two(
    watcher_parts, bank, clock, ids
) -> None:
    watcher = _watcher(watcher_parts, clock, ids, memory=bank)
    sources = _feeds([_permit("2019-1", cites="201804-3321")])

    first = await watcher.poll(district_id=DISTRICT, sources=sources, correlation_id="corr-1")
    second = await watcher.poll(district_id=DISTRICT, sources=sources, correlation_id="corr-2")

    assert first.open_question_ids == second.open_question_ids
    assert len(await bank.recall(district_id=DISTRICT, scopes=PUBLIC_READER)) == 1


async def test_with_no_bank_and_no_grounding_the_agent_is_the_agent_it_was(
    watcher_parts, clock, ids
) -> None:
    watcher = _watcher(watcher_parts, clock, ids)

    result = await watcher.poll(
        district_id=DISTRICT,
        sources=_feeds([_permit("2019-1", cites="201804-3321")]),
        correlation_id="corr-1",
    )

    assert watcher.reasons is False
    assert result.graph_steps == 0
    assert result.graph_stop == ""
    assert result.open_question_ids == ()
    assert result.references_outstanding == 0
    # And it still turns filed columns into facts.
    assert result.facts_written > 0


async def test_a_record_the_gazetteer_refuses_is_dropped_without_grounding(
    watcher_parts, clock, ids
) -> None:
    """A permit filed against the wrong building is worse than one nobody saw."""
    watcher = _watcher(watcher_parts, clock, ids)

    result = await watcher.poll(
        district_id=DISTRICT,
        sources=_feeds([_permit("2019-1", address="the rear structure")]),
        correlation_id="corr-1",
    )

    assert result.facts_written == 0
    assert result.addresses_touched == ()


async def test_grounding_can_bind_a_record_the_gazetteer_refused(watcher_parts, clock, ids) -> None:
    grounding = _AlwaysBinds()
    watcher = _watcher(watcher_parts, clock, ids, grounding=grounding)

    result = await watcher.poll(
        district_id=DISTRICT,
        sources=_feeds([_permit("2019-1", address="the rear structure")]),
        correlation_id="corr-1",
    )

    assert grounding.calls == ["the rear structure"]
    assert result.addresses_touched == (HAYES,)


async def test_the_graph_writes_no_fact_itself(watcher_parts, bank, clock, ids) -> None:
    """Every fact still comes out of the extractor, bound to a filed record."""
    profiles, facts, materializer, extractor = watcher_parts
    watcher = _watcher(watcher_parts, clock, ids, memory=bank)

    result = await watcher.poll(
        district_id=DISTRICT,
        sources=_feeds([_permit("2019-1")]),
        correlation_id="corr-1",
    )

    assert result.facts_written > 0
    for fact in await facts.list_for_address(HAYES):
        assert fact.produced_by_agent == AGENT_ID
        assert fact.source_ref == "permit/2019-1"
        assert fact.source_snapshot_id.startswith(PERMITS)


# --------------------------------------------------------------- the trace


async def test_every_node_leaves_a_span_naming_its_decision(recording_spans) -> None:
    budget = _budget()
    retrieval = RecordsRetrieval(sources=_feeds([_permit("2019-1")]), budget=budget)

    run = await _run(retrieval, budget=budget)

    spans = [span for span in TRACER.spans if span.name == f"agent.{AGENT_ID}"]
    assert [span.attributes["graph_node"] for span in spans] == list(run.trace.node_sequence)
    assert all("graph.decision" in span.attributes for span in spans)


async def test_no_span_carries_a_word_of_a_permit_narrative(recording_spans) -> None:
    budget = _budget()
    retrieval = RecordsRetrieval(sources=_feeds([_permit("2019-1")]), budget=budget)

    await _run(retrieval, budget=budget)

    rendered = " ".join(str(value) for span in TRACER.spans for value in span.attributes.values())
    for word in ("wood-frame", "Alteration", NARRATIVE):
        assert word not in rendered


async def test_a_checkpoint_carries_positions_and_not_records() -> None:
    state = RecordsGraphState(
        district_id=DISTRICT,
        queried=(PERMITS,),
        outstanding=("201804-3321",),
        snapshots=(
            SourceSnapshot(
                source_id=PERMITS,
                snapshot_id="p:1",
                fetched_at=OBSERVED,
                records=(_permit("2019-1"),),
            ),
        ),
    )

    payload = state.checkpoint_payload()

    assert payload["outstanding"] == ["201804-3321"]
    assert payload["snapshots"] == 1
    assert "wood-frame" not in str(payload)


# ------------------------------------------------------------- replayability


async def test_a_recorded_chain_replays_without_divergence(tmp_path) -> None:
    def build() -> RecordsRetrieval:
        return RecordsRetrieval(
            sources=_feeds([_permit("2019-1", cites="201804-3321")]), budget=_budget()
        )

    cassette = GraphCassette(fixtures_dir=tmp_path, record=True)
    first = await _run(build(), budget=_budget())
    cassette.store(first.trace)

    replayed = await run_graph(
        build().spec(),
        RecordsGraphState(district_id=DISTRICT),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        budget=_budget(),
        request_digest=first.trace.request_digest,
        use_langgraph=False,
        recorded=cassette.load(first.trace.request_digest),
    )

    assert replayed.trace.diverged_at is None
    assert replayed.trace.decisions == first.trace.decisions


async def test_a_chain_that_changed_is_reported_rather_than_hidden() -> None:
    baseline = await _run(
        RecordsRetrieval(sources=_feeds([_permit("2019-1")]), budget=_budget()),
        budget=_budget(),
    )

    changed = await run_graph(
        RecordsRetrieval(
            sources=_feeds([_permit("2019-1", cites="201804-3321")]), budget=_budget()
        ).spec(),
        RecordsGraphState(district_id=DISTRICT),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        budget=_budget(),
        request_digest=baseline.trace.request_digest,
        use_langgraph=False,
        recorded=baseline.trace,
    )

    assert changed.trace.diverged_at is not None


# ------------------------------------------------------------ with LangGraph


async def test_langgraph_runs_the_same_nodes_to_the_same_chain() -> None:
    pytest.importorskip("langgraph")

    def build() -> RecordsRetrieval:
        return RecordsRetrieval(
            sources=_feeds([_permit("2019-1", cites="201804-3321")]), budget=_budget()
        )

    builtin = await _run(build(), budget=_budget(), use_langgraph=False)
    compiled = await _run(build(), budget=_budget(), use_langgraph=True)

    assert compiled.trace.decisions == builtin.trace.decisions
    assert compiled.trace.stop is builtin.trace.stop
    assert compiled.state.outstanding == builtin.state.outstanding
