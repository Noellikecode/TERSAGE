"""The hazard watcher's cross-check graph, and the four things it must not do.

Everything here runs without LangGraph installed. The nodes and the router are
ordinary code in this repository and the built-in driver runs them; one test
compiles the identical node set into a real ``StateGraph`` and asserts the two
produce the same reasoning chain, and only that one skips when the package is
absent. If the graph only worked under the framework, the framework would be
the thing under test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.memory.audit import InMemoryAuditSink
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
    NodeResult,
    run_graph,
)
from firstdue.agents.graphs.hazard import (
    NODE_SURVEY,
    HazardCrossCheck,
    HazardGraphState,
    detect_ambiguities,
    normalize_facility_name,
)
from firstdue.agents.hazard_watcher import AGENT_ID, HazardWatcher
from firstdue.domain.enums import Classification, Scope, SourceType
from firstdue.domain.profiles import BuildingProfile
from firstdue.errors import SourceUnavailableError
from firstdue.observability.tracing import TRACER
from firstdue.ports.audit import AuditEventKind
from firstdue.ports.grounding import Resolution
from firstdue.ports.sources import SourceHealth, SourceRecord, SourceSnapshot
from firstdue.services.materialization import ProfileMaterializer
from firstdue.services.memory_bank import MemoryBank
from firstdue.sources.catalog import EPA, NREL, PHMSA, TIER_II

DISTRICT = "sffd-district-03"
BRYANT = "sf-1550-bryant"
MISSION = "sf-2130-mission"
OBSERVED = datetime(2026, 3, 1, tzinfo=UTC)

#: The narrative on a Tier II record. If any of it reaches a span, the span is
#: carrying a confidential filing, which is the failure this file exists to
#: make impossible to ship.
FILING_TEXT = "Tier II filing: anhydrous ammonia, rear ground-floor mechanical room, 2200 kg."

TIER_II_READER = frozenset({Scope.READ_PUBLIC_RECORDS, Scope.READ_TIER_II_METADATA})


# --------------------------------------------------------------- test doubles


class _Registry:
    """One hazard registry, backed by a list rather than a network."""

    def __init__(
        self,
        source_id: str,
        records: Sequence[SourceRecord] = (),
        *,
        unavailable: bool = False,
    ) -> None:
        self._source_id = source_id
        self._records = tuple(records)
        self._unavailable = unavailable
        self.fetches = 0

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> SourceType:
        return SourceType.EPA_FRS

    @property
    def classification(self) -> Classification:
        return Classification.PUBLIC

    async def fetch(self, *, address_id=None, since=None, cursor=None) -> SourceSnapshot:
        self.fetches += 1
        if self._unavailable:
            raise SourceUnavailableError("registry down", details={"source_id": self._source_id})
        return SourceSnapshot(
            source_id=self._source_id,
            snapshot_id=f"{self._source_id}:snap:{self.fetches}",
            fetched_at=OBSERVED,
            records=self._records,
        )

    async def health(self) -> SourceHealth:  # pragma: no cover - not exercised
        raise NotImplementedError


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
            confidence=0.95,
            evidence=("test://binding",),
            method="test-grounding/1",
        )


class _NeverBinds:
    """A grounding service that declines, which is the interesting case."""

    async def resolve_reference(
        self, reference: str, *, district_id: str, candidates: tuple[str, ...], deadline_ms: int
    ) -> Resolution:
        return Resolution(
            resolved=False,
            confidence=0.1,
            method="test-grounding/1",
            declined_reason="nothing settled it",
        )


def _epa(name: str, *, address_id: str | None, ref: str, street: str = "") -> SourceRecord:
    return SourceRecord(
        record_ref=ref,
        address_id=address_id,
        classification=Classification.PUBLIC,
        fields={"facility_name": name, "programs": ["TRI"], "street_address": street},
        observed_at=OBSERVED,
    )


def _tier_ii(name: str, *, address_id: str, ref: str) -> SourceRecord:
    return SourceRecord(
        record_ref=ref,
        address_id=address_id,
        classification=Classification.TIER_II_CONFIDENTIAL,
        fields={
            "facility_name": name,
            "present": True,
            "storage_location": "rear ground-floor mechanical room",
        },
        document_text=FILING_TEXT,
        observed_at=OBSERVED,
    )


def _budget(*, seconds: float = 60.0, max_steps: int = DEFAULT_MAX_STEPS, elapsed=None):
    """A budget whose clock a test controls.

    ``elapsed`` is a list of monotonic readings, consumed one per call, so a
    test can exhaust a wall-clock budget without spending one.
    """
    if elapsed is None:
        return BudgetGuard(seconds=seconds, max_steps=max_steps, monotonic=lambda: 0.0)
    readings = iter(elapsed)
    return BudgetGuard(
        seconds=seconds, max_steps=max_steps, monotonic=lambda: next(readings, elapsed[-1])
    )


async def _run(crosscheck: HazardCrossCheck, *, budget: BudgetGuard, use_langgraph: bool = False):
    return await run_graph(
        crosscheck.spec(),
        HazardGraphState(district_id=DISTRICT, address_ids=(BRYANT, MISSION)),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        budget=budget,
        request_digest="test-digest",
        use_langgraph=use_langgraph,
    )


@pytest.fixture
def bank(clock) -> MemoryBank:
    return MemoryBank(
        questions=InMemoryOpenQuestionRepository(),
        checkpoints=InMemoryCheckpointRepository(),
        clock=clock,
    )


@pytest.fixture
def profiles() -> InMemoryProfileRepository:
    return InMemoryProfileRepository()


@pytest.fixture
async def watcher_parts(profiles, clock, ids):
    """The repositories a HazardWatcher needs, with two profiles on file."""
    for address_id in (BRYANT, MISSION):
        await profiles.create(BuildingProfile(address_id=address_id, district_id=DISTRICT))
    facts = InMemoryFactRepository()
    materializer = ProfileMaterializer(
        profiles=profiles,
        conflicts=InMemoryConflictRepository(),
        locks=InMemoryLockRepository(),
        clock=clock,
        ids=ids,
    )
    return profiles, facts, materializer


@pytest.fixture
def recording_spans() -> Iterator[None]:
    TRACER.configure(enabled=False, record_spans=True)
    TRACER.clear()
    yield
    TRACER.clear()
    TRACER.configure(enabled=False, record_spans=False)


# ------------------------------------------------------- noticing ambiguity


def test_two_clerks_spelling_one_name_normalize_alike() -> None:
    assert normalize_facility_name("ACME PLATING INC") == normalize_facility_name("Acme Plating")


def test_a_different_business_does_not_normalize_alike() -> None:
    """The normalizer must not manufacture the ambiguity it detects."""
    assert normalize_facility_name("Acme Plating") != normalize_facility_name("Acme Plumbing")


def test_two_spellings_at_one_parcel_are_an_ambiguity() -> None:
    gathered = {
        EPA: (
            _epa("ACME PLATING INC", address_id=BRYANT, ref="epa/1"),
            _epa("Acme Plating", address_id=BRYANT, ref="epa/2"),
        )
    }

    found = detect_ambiguities(gathered, address_ids=(BRYANT,))

    assert [item.kind for item in found] == ["ALIASED"]
    assert found[0].address_id == BRYANT
    assert set(found[0].candidates) == {"ACME PLATING INC", "Acme Plating"}


def test_an_ambiguity_raised_by_a_tier_ii_row_is_a_tier_ii_question() -> None:
    """Strictest classification wins, because the bank gates recall on it."""
    gathered = {
        EPA: (_epa("ACME PLATING INC", address_id=BRYANT, ref="epa/1"),),
        TIER_II: (_tier_ii("Acme Plating", address_id=BRYANT, ref="tier-ii/1"),),
    }

    found = detect_ambiguities(gathered, address_ids=(BRYANT,))

    assert found[0].classification is Classification.TIER_II_CONFIDENTIAL


def test_an_ambiguitys_key_carries_no_filed_name() -> None:
    """The key reaches durable memory. An occupant's name must not ride on it."""
    gathered = {
        EPA: (
            _epa("ACME PLATING INC", address_id=BRYANT, ref="epa/1"),
            _epa("Acme Plating", address_id=BRYANT, ref="epa/2"),
        )
    }

    key = detect_ambiguities(gathered, address_ids=(BRYANT,))[0].key

    assert "acme" not in key.lower()
    assert "plating" not in key.lower()


# ------------------------------------------------------------ the loop


async def test_every_registry_is_read_even_once_the_graph_is_confident() -> None:
    """A registry nobody read produces neither a fact nor an UNAVAILABLE."""
    registries = [
        _Registry(EPA, (_epa("Solo Tenant", address_id=BRYANT, ref="epa/1"),)),
        _Registry(TIER_II),
        _Registry(PHMSA),
        _Registry(NREL),
    ]
    crosscheck = HazardCrossCheck(sources=registries, budget=_budget())

    run = await _run(crosscheck, budget=_budget())

    assert run.trace.stop is GraphStop.CLOSED
    assert set(run.state.queried) == {EPA, TIER_II, PHMSA, NREL}
    assert all(registry.fetches == 1 for registry in registries)


async def test_a_second_registry_settles_a_spelling_with_no_model_at_all() -> None:
    """One filed name in the corroborating registry closes the question."""
    registries = [
        _Registry(
            EPA,
            (
                _epa("ACME PLATING INC", address_id=BRYANT, ref="epa/1"),
                _epa("Acme Plating", address_id=BRYANT, ref="epa/2"),
            ),
        ),
        _Registry(TIER_II, (_tier_ii("Acme Plating", address_id=BRYANT, ref="tier-ii/1"),)),
        _Registry(PHMSA),
        _Registry(NREL),
    ]
    crosscheck = HazardCrossCheck(sources=registries, budget=_budget())

    run = await _run(crosscheck, budget=_budget())

    assert run.trace.stop is GraphStop.CLOSED
    assert run.state.settled
    assert not run.state.ambiguities
    assert "cross_check" in run.trace.node_sequence


async def test_an_ambiguity_nothing_settles_ends_unresolved_and_names_what_was_tried() -> None:
    registries = [
        _Registry(
            EPA,
            (
                _epa("ACME PLATING INC", address_id=BRYANT, ref="epa/1"),
                _epa("Acme Plating", address_id=BRYANT, ref="epa/2"),
            ),
        ),
        _Registry(TIER_II),
        _Registry(PHMSA),
        _Registry(NREL),
    ]
    crosscheck = HazardCrossCheck(sources=registries, budget=_budget())

    run = await _run(crosscheck, budget=_budget())

    assert run.trace.stop is GraphStop.UNRESOLVED
    assert run.state.ambiguities
    # Every registry it asked is on the record, so tomorrow's pass does not
    # re-ask any of them.
    assert {token.split("/")[0] for token in run.state.ruled_out} == {TIER_II, PHMSA, NREL}


async def test_a_stray_registry_row_is_bound_only_from_the_offered_candidates() -> None:
    grounding = _AlwaysBinds()
    registries = [
        _Registry(EPA, (_epa("Stray Co", address_id=None, ref="epa/1", street="1550 Bryant St"),)),
        _Registry(TIER_II),
        _Registry(PHMSA),
        _Registry(NREL),
    ]
    crosscheck = HazardCrossCheck(sources=registries, budget=_budget(), grounding=grounding)

    run = await _run(crosscheck, budget=_budget())

    assert grounding.calls == ["1550 Bryant St"]
    bound = [record for records in run.state.gathered.values() for record in records]
    assert all(record.address_id in (BRYANT, MISSION) for record in bound)


async def test_a_declined_binding_leaves_the_row_unplaced() -> None:
    """A decline is the correct answer under ambiguity, and it must stick."""
    registries = [
        _Registry(EPA, (_epa("Stray Co", address_id=None, ref="epa/1", street="Rear structure"),)),
        _Registry(TIER_II),
        _Registry(PHMSA),
        _Registry(NREL),
    ]
    crosscheck = HazardCrossCheck(sources=registries, budget=_budget(), grounding=_NeverBinds())

    run = await _run(crosscheck, budget=_budget())

    assert run.trace.stop is GraphStop.UNRESOLVED
    assert run.state.gathered[EPA][0].address_id is None


# ------------------------------------------------------------- the ceilings


async def test_the_step_bound_stops_a_graph_before_it_runs_away() -> None:
    """An unbounded graph is an outage, so the bound is asserted, not assumed."""
    registries = [
        _Registry(
            EPA,
            (
                _epa("ACME PLATING INC", address_id=BRYANT, ref="epa/1"),
                _epa("Acme Plating", address_id=BRYANT, ref="epa/2"),
            ),
        ),
        _Registry(TIER_II),
        _Registry(PHMSA),
        _Registry(NREL),
    ]
    budget = _budget(max_steps=3)
    crosscheck = HazardCrossCheck(sources=registries, budget=budget)

    run = await _run(crosscheck, budget=budget)

    assert run.trace.stop is GraphStop.OUT_OF_STEPS
    # The bound, plus the park node the router sends an exhausted graph to.
    assert len(run.trace.records) <= 3 + 1


async def test_running_out_of_time_parks_rather_than_raising() -> None:
    budget = _budget(seconds=10.0, elapsed=[0.0, 99.0])
    crosscheck = HazardCrossCheck(sources=[_Registry(EPA)], budget=budget)

    run = await _run(crosscheck, budget=budget)

    assert run.trace.stop is GraphStop.OUT_OF_TIME
    assert run.trace.node_sequence[0] == NODE_SURVEY
    assert run.trace.node_sequence[-1] == "park"


async def test_the_router_stops_a_parked_graph() -> None:
    """``park`` is terminal. A router that could leave it would never stop."""
    budget = _budget()
    crosscheck = HazardCrossCheck(sources=[_Registry(EPA)], budget=budget)
    parked = HazardGraphState(district_id=DISTRICT, stop=GraphStop.OUT_OF_TIME)

    assert crosscheck.route(parked) == STOP


# ---------------------------------------------------- the agent, end to end


async def test_budget_exhaustion_opens_a_question_and_checkpoints_it(
    watcher_parts, bank, clock
) -> None:
    """Out of budget is a state to record, never a failure and never a guess."""
    profiles, facts, materializer = watcher_parts
    watcher = HazardWatcher(
        profiles=profiles,
        facts=facts,
        materializer=materializer,
        clock=clock,
        memory=bank,
        use_langgraph=False,
        max_graph_steps=1,
    )

    result = await watcher.poll(
        district_id=DISTRICT,
        sources=[_Registry(EPA, (_epa("Solo", address_id=BRYANT, ref="epa/1"),))],
        correlation_id="corr-1",
        # A deadline already in the past, which is what a runtime hands an
        # agent it is about to run out of patience with.
        deadline=clock.now() - timedelta(seconds=1),
    )

    assert result.graph_stop in {str(GraphStop.OUT_OF_TIME), str(GraphStop.OUT_OF_STEPS)}
    assert result.open_question_ids

    carried = await bank.recall(district_id=DISTRICT, scopes=TIER_II_READER)
    assert [question.opened_by for question in carried] == [AGENT_ID]
    assert carried[0].waiting_on
    assert await bank.resume(carried[0].question_id, scopes=TIER_II_READER) is not None


async def test_an_open_question_is_reopened_rather_than_duplicated(
    watcher_parts, bank, clock
) -> None:
    """Two passes on one stuck district carry one thread, not two."""
    profiles, facts, materializer = watcher_parts
    watcher = HazardWatcher(
        profiles=profiles,
        facts=facts,
        materializer=materializer,
        clock=clock,
        memory=bank,
        use_langgraph=False,
        max_graph_steps=1,
    )
    sources = [_Registry(EPA, (_epa("Solo", address_id=BRYANT, ref="epa/1"),))]

    first = await watcher.poll(district_id=DISTRICT, sources=sources, correlation_id="corr-1")
    second = await watcher.poll(district_id=DISTRICT, sources=sources, correlation_id="corr-2")

    assert first.open_question_ids == second.open_question_ids
    assert len(await bank.recall(district_id=DISTRICT, scopes=TIER_II_READER)) == 1


async def test_with_no_bank_and_no_grounding_the_agent_is_the_agent_it_was(
    watcher_parts, clock
) -> None:
    """The default configuration, which is what the whole suite runs in."""
    profiles, facts, materializer = watcher_parts
    watcher = HazardWatcher(profiles=profiles, facts=facts, materializer=materializer, clock=clock)

    result = await watcher.poll(
        district_id=DISTRICT,
        sources=[
            _Registry(TIER_II, (_tier_ii("Acme", address_id=BRYANT, ref="tier-ii/1"),)),
            _Registry(NREL, unavailable=True),
        ],
        correlation_id="corr-1",
    )

    assert watcher.reasons is False
    assert result.graph_steps == 0
    assert result.graph_stop == ""
    assert result.open_question_ids == ()
    # And it still does the thing it has always done.
    assert result.facts_written > 0
    assert result.unavailable_sources == (NREL,)
    assert result.unavailable_facts == 2
    assert str(Classification.TIER_II_CONFIDENTIAL) in result.classifications


async def test_the_graph_writes_no_fact_itself(watcher_parts, bank, clock) -> None:
    """Every fact carries a record's provenance, never a node's decision."""
    profiles, facts, materializer = watcher_parts
    watcher = HazardWatcher(
        profiles=profiles,
        facts=facts,
        materializer=materializer,
        clock=clock,
        memory=bank,
        use_langgraph=False,
    )

    result = await watcher.poll(
        district_id=DISTRICT,
        sources=[_Registry(TIER_II, (_tier_ii("Acme", address_id=BRYANT, ref="tier-ii/1"),))],
        correlation_id="corr-1",
    )

    assert result.facts_written > 0
    stored = await facts.list_for_address(BRYANT)
    assert stored
    for fact in stored:
        assert fact.produced_by_agent == AGENT_ID
        # Traceable to a row a registry published, which is what a node's
        # decision can never be.
        assert fact.source_ref == "tier-ii/1"
        assert fact.extracted_by_model is False


def test_a_node_result_has_nowhere_to_put_a_value() -> None:
    """The structural half of the rule above, asserted on the type itself.

    ``updates`` reaches the state and the state reaches the deterministic
    settle; ``decision`` and ``counts`` reach the trace and the span. A field
    that carried a fact would be a fourth thing, and there is not one.
    """
    assert {field.name for field in fields(NodeResult)} == {"decision", "updates", "counts"}
    assert NodeResult(decision="read:epa-frs").counts == {}


# -------------------------------------------------------------- the trace


async def test_every_node_leaves_a_span_that_says_what_it_decided(
    recording_spans,
) -> None:
    registries = [
        _Registry(TIER_II, (_tier_ii("Acme", address_id=BRYANT, ref="tier-ii/1"),)),
        _Registry(EPA, (_epa("Acme", address_id=BRYANT, ref="epa/1"),)),
    ]
    budget = _budget()
    crosscheck = HazardCrossCheck(sources=registries, budget=budget)

    run = await _run(crosscheck, budget=budget)

    spans = [span for span in TRACER.spans if span.name == f"agent.{AGENT_ID}"]
    assert len(spans) == len(run.trace.records)
    assert [span.attributes["graph_node"] for span in spans] == list(run.trace.node_sequence)
    assert all("graph.decision" in span.attributes for span in spans)


async def test_no_span_carries_a_word_of_a_confidential_filing(recording_spans) -> None:
    """A span that never held a document cannot leak one -- asserted, not hoped."""
    registries = [_Registry(TIER_II, (_tier_ii("Acme", address_id=BRYANT, ref="tier-ii/1"),))]
    budget = _budget()
    crosscheck = HazardCrossCheck(sources=registries, budget=budget)

    await _run(crosscheck, budget=budget)

    rendered = " ".join(str(value) for span in TRACER.spans for value in span.attributes.values())
    for word in ("ammonia", "mechanical room", "2200", FILING_TEXT):
        assert word not in rendered


async def test_a_checkpoint_carries_positions_and_not_records(bank, clock) -> None:
    state = HazardGraphState(
        district_id=DISTRICT,
        queried=(EPA,),
        gathered={EPA: (_tier_ii("Acme", address_id=BRYANT, ref="tier-ii/1"),)},
        waiting_on="the building at 1550 Bryant St",
    )

    payload = state.checkpoint_payload()

    assert payload["queried"] == [EPA]
    assert "gathered" not in payload
    assert "ammonia" not in str(payload)


# ------------------------------------------------------------- replayability


async def test_a_recorded_chain_replays_without_divergence(tmp_path) -> None:
    """The graph trace is the replayable unit; a re-run reproduces it exactly."""

    def build() -> HazardCrossCheck:
        return HazardCrossCheck(
            sources=[
                _Registry(EPA, (_epa("Solo", address_id=BRYANT, ref="epa/1"),)),
                _Registry(TIER_II),
            ],
            budget=_budget(),
        )

    cassette = GraphCassette(fixtures_dir=tmp_path, record=True)
    first = await _run(build(), budget=_budget())
    cassette.store(first.trace)

    replayed = await run_graph(
        build().spec(),
        HazardGraphState(district_id=DISTRICT, address_ids=(BRYANT, MISSION)),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        budget=_budget(),
        request_digest=first.trace.request_digest,
        use_langgraph=False,
        recorded=cassette.load(first.trace.request_digest),
    )

    assert replayed.trace.diverged_at is None
    assert replayed.trace.decisions == first.trace.decisions


async def test_a_changed_chain_is_reported_as_a_divergence() -> None:
    """A cassette that could not notice a change would pin nothing."""
    quiet = HazardCrossCheck(sources=[_Registry(EPA)], budget=_budget())
    baseline = await _run(quiet, budget=_budget())

    noisy = HazardCrossCheck(
        sources=[
            _Registry(
                EPA,
                (
                    _epa("ACME PLATING INC", address_id=BRYANT, ref="epa/1"),
                    _epa("Acme Plating", address_id=BRYANT, ref="epa/2"),
                ),
            ),
            _Registry(TIER_II),
        ],
        budget=_budget(),
    )
    changed = await run_graph(
        noisy.spec(),
        HazardGraphState(district_id=DISTRICT, address_ids=(BRYANT, MISSION)),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        budget=_budget(),
        request_digest=baseline.trace.request_digest,
        use_langgraph=False,
        recorded=baseline.trace,
    )

    assert changed.trace.diverged_at is not None


# ---------------------------------------------------------------- fake mode


def test_fake_mode_never_imports_langgraph() -> None:
    """The whole slow-loop wiring, imported, with the package left alone.

    A subprocess rather than an assertion on this one, because by the time this
    file runs another test has already imported LangGraph deliberately. What
    has to hold is that *wiring the fleet* does not: fake mode is the
    credential-free, dependency-free path, and an import at module scope
    anywhere under ``agents/`` would quietly make the extra mandatory.
    """
    probe = (
        "import sys, firstdue.demo.scenario, firstdue.agents.graphs;"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] "
        "in {'langgraph', 'langchain_core', 'langchain_google_vertexai'});"
        "print(leaked)"
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "USE_FAKE_AGENTS": "true"},
    )

    assert completed.stdout.strip() == "[]"


# ------------------------------------------------------------- with LangGraph


async def test_langgraph_runs_the_same_nodes_to_the_same_chain() -> None:
    """The framework is an executor. Swapping it in must change nothing."""
    pytest.importorskip("langgraph")

    def build() -> HazardCrossCheck:
        return HazardCrossCheck(
            sources=[
                _Registry(
                    EPA,
                    (
                        _epa("ACME PLATING INC", address_id=BRYANT, ref="epa/1"),
                        _epa("Acme Plating", address_id=BRYANT, ref="epa/2"),
                    ),
                ),
                _Registry(TIER_II, (_tier_ii("Acme Plating", address_id=BRYANT, ref="t/1"),)),
                _Registry(PHMSA),
            ],
            budget=_budget(),
        )

    builtin = await _run(build(), budget=_budget(), use_langgraph=False)
    compiled = await _run(build(), budget=_budget(), use_langgraph=True)

    assert compiled.trace.decisions == builtin.trace.decisions
    assert compiled.trace.stop is builtin.trace.stop
    assert compiled.state.settled == builtin.state.settled


async def test_a_pass_with_no_registry_wired_closes_instead_of_spinning() -> None:
    """A node that returned no update would be routed straight back to itself."""
    budget = _budget(max_steps=6)
    crosscheck = HazardCrossCheck(sources=[], budget=budget)

    run = await _run(crosscheck, budget=budget)

    assert run.trace.stop is GraphStop.CLOSED
    assert run.trace.node_sequence == (NODE_SURVEY,)


def test_a_checkpoint_stays_writable_on_a_real_district() -> None:
    """Parking must not fail because the district got big.

    Every list on a checkpoint grows with the district -- addresses settled,
    registries queried, dead ends ruled out -- and `MemoryCheckpoint` refuses a
    payload over MAX_CHECKPOINT_STATE_BYTES, correctly: a memory must never
    become somewhere document contents accumulate.

    Unbounded, those two collide the moment a district is real. At nine
    addresses the payload fit; at 386 the watcher exhausted its budget, tried to
    park, and the park *raised* -- so the pass that ran out of time also lost the
    record of where it got to, which is the one thing parking exists to keep.
    """
    import json

    from firstdue.agents.graphs.base import MAX_CHECKPOINT_ENTRIES
    from firstdue.agents.graphs.hazard import HazardGraphState
    from firstdue.domain.memory import MAX_CHECKPOINT_STATE_BYTES

    state = HazardGraphState(
        district_id="sffd-district-03",
        settled=tuple(f"sf-{i:05d}-mission-street" for i in range(4000)),
        queried=tuple(f"epa-frs:110000{i:06d}" for i in range(4000)),
        ruled_out=tuple(f"epa-frs:{i}:name-mismatch" for i in range(4000)),
    )
    payload = state.checkpoint_payload()

    assert len(payload["settled"]) <= MAX_CHECKPOINT_ENTRIES
    size = len(json.dumps(payload).encode("utf-8"))
    assert size <= MAX_CHECKPOINT_STATE_BYTES, f"{size} bytes"


def test_the_tail_is_kept_because_it_is_the_frontier() -> None:
    """Truncation drops the oldest, not the newest.

    The next pass resumes from where this one stopped, so the recent entries are
    the ones worth carrying; the older ones have already done their work.
    """
    from firstdue.agents.graphs.base import MAX_CHECKPOINT_ENTRIES, bounded

    kept = bounded([str(i) for i in range(MAX_CHECKPOINT_ENTRIES * 3)])

    assert len(kept) == MAX_CHECKPOINT_ENTRIES
    assert kept[-1] == str(MAX_CHECKPOINT_ENTRIES * 3 - 1)


async def test_a_pass_out_of_budget_still_says_it_ran(watcher_parts, clock, ids) -> None:
    """The console's only evidence is what an agent recorded.

    A pass that spent its allowance is the case where a line in the log matters
    most and is the case that had none: applying an address is a profile read, a
    fact write and a materialisation, and with nothing held back for them the
    runtime cancelled the coroutine before the closing record. From the fleet
    panel that is indistinguishable from an agent that never started.

    So a pass whose deadline has already passed must defer rather than apply,
    and must still write its ``AGENT_PASS`` saying which of the two happened.
    The fixed pass on purpose: it is the half that reaches ``_settle`` with a
    registry's rows actually in hand, so a zero here is a deferral this guard
    produced rather than an empty gather.
    """
    profiles, facts, materializer = watcher_parts
    audit = InMemoryAuditSink()
    watcher = HazardWatcher(
        profiles=profiles,
        facts=facts,
        materializer=materializer,
        clock=clock,
        audit=audit,
        ids=ids,
    )
    sources = [_Registry(EPA, (_epa("Solo", address_id=BRYANT, ref="epa/1"),))]

    # The control: the same registry row, the same agent, no deadline.
    unbounded = await watcher.poll(district_id=DISTRICT, sources=sources, correlation_id="corr-0")
    assert unbounded.facts_written > 0

    spent = await watcher.poll(
        district_id=DISTRICT,
        sources=sources,
        correlation_id="corr-1",
        deadline=clock.now() - timedelta(seconds=1),
    )

    passes = [e for e in await audit.list_events(limit=50) if e.kind is AuditEventKind.AGENT_PASS]
    assert [e.correlation_id for e in passes] == ["corr-1", "corr-0"]
    assert all(e.actor == AGENT_ID for e in passes)

    # Nothing applied, and the record says so as a deferral rather than as a
    # district with no hazards in it -- which is the distinction this agent
    # exists to keep.
    assert spent.facts_written == 0
    truncated = passes[0]
    assert truncated.detail["addresses"] == "0"
    assert int(truncated.detail["deferred"]) > 0
