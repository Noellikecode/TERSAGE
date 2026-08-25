"""The interceptor's focus graph, and the four things it must not do.

The feature under test is the collision: a caller reports people on the third
floor, the permit on file says two storeys, the lidar measured three,
``structure-watch`` raised a conflict over the pair in March, and
``hazard-watcher`` is still carrying a question nobody has answered. Every one of
those was already stored and none of them were ever put side by side. Here they
are, and the assertions are about *which ids reach which agent*.

Everything runs without LangGraph installed -- the nodes and the router are
ordinary code in this repository and the built-in driver runs them. One test
compiles the identical node set into a real ``StateGraph`` and asserts the two
produce the same reasoning chain; only that one skips when the package is
absent.

The four things:

* it may not assert a value, at any point, by any route;
* it may not choose the fleet -- that is ``plan_handoffs``, by capability match;
* it may not block, delay or alter the instant brief (ADR 0004);
* it may not raise, park, or return nothing when its budget runs out.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

import pytest

from firstdue.adapters.clock import FixedClock
from firstdue.adapters.memory.memory_bank import (
    InMemoryCheckpointRepository,
    InMemoryOpenQuestionRepository,
)
from firstdue.adapters.memory.repositories import InMemoryIncidentLogRepository
from firstdue.agents.graphs.base import STOP, BudgetGuard, GraphStop, run_graph
from firstdue.agents.graphs.interceptor import (
    BEARS_ON,
    NODE_GATHER,
    Collision,
    FocusComposer,
    FocusGraphState,
    audience_for,
    detect_collisions,
    fallback_focus,
)
from firstdue.domain.briefs import BriefEmission, BriefStage
from firstdue.domain.conflicts import Conflict
from firstdue.domain.enums import (
    ApprovalThreshold,
    Capability,
    Classification,
    Department,
    Loop,
    Scope,
    SourceType,
)
from firstdue.domain.facts import StructuralFact
from firstdue.domain.geometry import GeometrySpec
from firstdue.domain.keys import IntakeKeys, Keys
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.domain.registry import AgentDescriptor
from firstdue.domain.values import IntegerValue
from firstdue.errors import ValidationError
from firstdue.incident.focus import FocusKind, focus_scope, read_focus
from firstdue.incident.intake import IntakeChannel, IntakeReader, IntakeReading, ReportedItem
from firstdue.incident.interceptor import AGENT_ID, IncidentInterceptor
from firstdue.observability.tracing import TRACER
from firstdue.registry.descriptors import active_descriptors
from firstdue.security.armor import LocalInjectionDetector
from firstdue.services.memory_bank import MemoryBank

NOW = datetime(2026, 8, 21, 3, 14, tzinfo=UTC)
MARCH = datetime(2026, 3, 4, 9, 0, tzinfo=UTC)
ADDRESS = "sf-0450-hayes"
DISTRICT = "sffd-district-03"
INCIDENT = "inc-1"

PERMIT_FACT = "fact_permit_storeys"
LIDAR_FACT = "fact_lidar_storeys"
CONFLICT = "conflict_storeys"

#: What the caller actually said. Not one word of it may reach a pointer, a log
#: entry, or a span -- these strings are what the assertions grep for.
TRANSCRIPT = "There are people on the third floor. It is a three storey building."
REPORTED_FLOOR = "third floor"

PUBLIC_READER = frozenset({Scope.READ_PUBLIC_RECORDS, Scope.READ_PROFILE})


# ------------------------------------------------------------------ helpers


def _fact(key: str, *, fact_id: str, source_type: SourceType) -> StructuralFact:
    return StructuralFact(
        fact_id=fact_id,
        address_id=ADDRESS,
        canonical_key=key,
        value=IntegerValue(integer=2),
        source_type=source_type,
        source_ref=f"{source_type.value.lower()}/1",
        source_snapshot_id="snap-1",
        observed_at=MARCH,
        ingested_at=MARCH,
        confidence=0.9,
        classification=Classification.PUBLIC,
    )


def _snapshot(*, geometry: bool = True, version: int = 7) -> ProfileSnapshot:
    """The building as the slow loop left it: a permit, a measurement, a fight."""
    spec = None
    if geometry:
        spec = GeometrySpec(
            address_id=ADDRESS,
            generated_at=MARCH,
            footprint=((0.0, 0.0), (10.0, 0.0), (10.0, 8.0)),
            collapse_zone_radius_m=12.0,
        )
    return ProfileSnapshot(
        address_id=ADDRESS,
        district_id=DISTRICT,
        profile_version=version,
        snapshot_id=f"snap-{version}",
        read_at=NOW,
        facts={
            Keys.STORIES: _fact(Keys.STORIES, fact_id=PERMIT_FACT, source_type=SourceType.PERMIT),
            Keys.HAZARD_TIER_II_PRESENT: _fact(
                Keys.HAZARD_TIER_II_PRESENT,
                fact_id="fact_tier_ii",
                source_type=SourceType.TIER_II,
            ),
        },
        conflicts=(
            Conflict(
                conflict_id=CONFLICT,
                address_id=ADDRESS,
                canonical_key=Keys.STORIES,
                rule_id="rule_storey_disagreement",
                severity=4,
                fact_ids=(PERMIT_FACT, LIDAR_FACT),
                summary="the permit and the lidar measurement disagree",
                detected_at=MARCH,
            ),
        ),
        geometry=spec,
        open_referral_ids=("referral_hayes_1",),
        last_human_survey=MARCH,
    )


def _reading() -> IntakeReading:
    """A 911 read that reported the floor of origin. Keys, and a quoted span."""
    from firstdue.domain.facts import SourceSpan

    start = TRANSCRIPT.index(REPORTED_FLOOR)
    return IntakeReading(
        incident_id=INCIDENT,
        channel=IntakeChannel.CALL_911,
        source_ref="call/CAD-1",
        items=(
            ReportedItem(
                intake_key=IntakeKeys.REPORTED_FLOOR_OF_ORIGIN,
                raw_value=REPORTED_FLOOR,
                span=SourceSpan(
                    locator="call/CAD-1",
                    start_offset=start,
                    end_offset=start + len(REPORTED_FLOOR),
                    quoted_text=REPORTED_FLOOR,
                ),
                channel=IntakeChannel.CALL_911,
                source_ref="call/CAD-1",
                model_confidence=0.7,
            ),
        ),
        model_ref="scripted/1",
    )


def _bank(clock: FixedClock | None = None) -> MemoryBank:
    """A bank whose clock is in March, so the threads it opens are old ones.

    That is the situation under test: the slow loop wrote these down months
    before anybody dialled 911, and nothing in the incident loop has read them
    since.
    """
    return MemoryBank(
        questions=InMemoryOpenQuestionRepository(),
        checkpoints=InMemoryCheckpointRepository(),
        clock=clock or FixedClock(MARCH),
    )


async def _thread(bank: MemoryBank, *, evidence: tuple[str, ...] = (PERMIT_FACT,)):
    """The question ``structure-watch`` opened in March and nobody answered."""
    return await bank.open(
        district_id=DISTRICT,
        address_id=ADDRESS,
        question="Is the third storey permitted, or is it unpermitted construction?",
        waiting_on="a human survey of the building",
        opened_by="structure-watch",
        opened_by_version="1.0.0",
        classification=Classification.PUBLIC,
        evidence_fact_ids=evidence,
    )


def _interceptor(**overrides) -> IncidentInterceptor:
    payload: dict[str, object] = {
        "intake": IntakeReader(model=_NoModel(), screen=LocalInjectionDetector()),
        "memory": _bank(),
    }
    payload.update(overrides)
    return IncidentInterceptor(**payload)  # type: ignore[arg-type]


class _NoModel:
    """The intake never runs in this file; the reader still wants a client."""

    async def extract(self, **_: object) -> object:  # pragma: no cover - never called
        raise AssertionError("the focus graph does not read the intake")


class _RaisingPlanner:
    """A planner that comes apart mid-graph. The focus must survive it."""

    async def choose(self, **_: object) -> str | None:
        raise RuntimeError("the planner endpoint is unreachable")


class _ReversingPlanner:
    """Picks the last option rather than the first. Reordering, and nothing else."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, ...]] = []

    async def choose(
        self, *, node: str, options: tuple[str, ...], counts: Mapping[str, int], deadline_ms: int
    ) -> str | None:
        self.seen.append(options)
        return options[-1]


class _InventingPlanner:
    """Answers with something that was never on the list."""

    async def choose(self, **_: object) -> str | None:
        return "the building has three storeys"


def _descriptor(
    agent_id: str, *, capabilities: set[Capability], scopes: set[Scope]
) -> AgentDescriptor:
    return AgentDescriptor(
        agent_id=agent_id,
        version="1.0.0",
        publisher_department=Department.FIRE,
        loop=Loop.INCIDENT,
        role_summary="a test agent",
        capabilities=frozenset(capabilities),
        required_scopes=frozenset(scopes),
        classifications_accessed=frozenset({Classification.PUBLIC}),
        approval_threshold=ApprovalThreshold.NONE,
        input_schema_ref="firstdue.schemas.Test",
        output_schema_ref="firstdue.schemas.Test",
        latency_target_ms=1_000,
        published_at=MARCH,
    )


@pytest.fixture
def recording_spans() -> Iterator[None]:
    TRACER.configure(enabled=False, record_spans=True)
    TRACER.clear()
    yield
    TRACER.clear()
    TRACER.configure(enabled=False, record_spans=False)


# ---------------------------------------------------- noticing the collision


def test_the_collision_is_what_the_focus_leads_with() -> None:
    """Reported, disputed, and still an open question. Priority one."""
    question = None
    collisions = detect_collisions(
        _snapshot(),
        reported_keys=(IntakeKeys.REPORTED_FLOOR_OF_ORIGIN,),
        questions=(),
    )
    top = collisions[0]
    assert top.canonical_key == Keys.STORIES
    assert top.reported is True
    assert top.conflict_ids == (CONFLICT,)
    assert set(top.fact_ids) == {PERMIT_FACT, LIDAR_FACT}
    # Reported *and* disputed. Top of the scale, which is the whole point:
    # this is the one worth interrupting a size-up for.
    assert top.severity == 4
    assert top.priority == 1
    assert question is None


def test_an_unreported_conflict_ranks_below_a_reported_one() -> None:
    reported = Collision(canonical_key=Keys.STORIES, reported=True, conflict_ids=("conflict_a",))
    quiet = Collision(canonical_key=Keys.ROOF_TYPE, conflict_ids=("conflict_b",))
    assert reported.priority < quiet.priority
    assert sorted((quiet, reported), key=lambda c: c.rank)[0] is reported


def test_a_question_is_attached_by_evidence_ids_and_never_by_its_text() -> None:
    """An id match is a fact about the record. A text match is an opinion."""
    from firstdue.domain.memory import OpenQuestion, derive_question_id

    def _q(question: str, evidence: tuple[str, ...]) -> OpenQuestion:
        return OpenQuestion(
            question_id=derive_question_id(
                district_id=DISTRICT,
                address_id=ADDRESS,
                opened_by="structure-watch",
                question=question,
            ),
            district_id=DISTRICT,
            address_id=ADDRESS,
            question=question,
            opened_by="structure-watch",
            opened_by_version="1.0.0",
            opened_at=MARCH,
            last_examined_at=MARCH,
            waiting_on="a survey",
            evidence_fact_ids=evidence,
            classification=Classification.PUBLIC,
            confidence=0.5,
        )

    anchored = _q("Is the third storey permitted?", (PERMIT_FACT,))
    # Names the same attribute in prose and cites nothing. It is still carried,
    # ranked below, rather than guessed onto structure.stories.
    floating = _q("Who owns the structure.stories record here?", ())

    collisions = detect_collisions(_snapshot(), questions=(anchored, floating))
    by_key = {c.key: c for c in collisions}
    assert anchored.question_id in by_key[Keys.STORIES].question_ids
    assert floating.question_id not in by_key[Keys.STORIES].question_ids
    assert f"question:{floating.question_id}" in by_key


def test_the_reported_alarm_level_bears_on_nothing() -> None:
    """Recorded, never applied. The same rule the wake table states."""
    assert BEARS_ON[IntakeKeys.REPORTED_ALARM_LEVEL] == ()
    assert (
        detect_collisions(
            _snapshot(geometry=False, version=1).model_copy(update={"conflicts": ()}),
            reported_keys=(IntakeKeys.REPORTED_ALARM_LEVEL,),
        )
        == ()
    )


# -------------------------------------------------- who is pointed at what


@pytest.mark.authorization
def test_the_graph_does_not_choose_the_fleet() -> None:
    """Attention is matched against declared authority, never against an id."""
    catalog = active_descriptors()
    assert audience_for(FocusKind.GEOMETRY, catalog) == ("sensor-fusion",)
    assert audience_for(FocusKind.HAZARD, catalog) == ("agency-notifier",)
    assert audience_for(FocusKind.OPEN_QUESTION, catalog) == ("incident-recorder",)
    assert set(audience_for(FocusKind.CONFLICT, catalog)) == {
        "agency-notifier",
        "incident-recorder",
        "sensor-fusion",
    }


@pytest.mark.authorization
def test_an_agent_that_declared_nothing_is_pointed_nowhere() -> None:
    silent = _descriptor("quiet-agent", capabilities={Capability.READ}, scopes={Scope.READ_AUDIT})
    assert audience_for(FocusKind.CONFLICT, (silent,)) == ()


@pytest.mark.authorization
def test_a_new_agent_is_pointed_at_the_right_things_without_an_edit() -> None:
    """Adding an agent to the catalog routes attention to it automatically."""
    newcomer = _descriptor(
        "aerial-survey",
        capabilities={Capability.READ},
        scopes={Scope.READ_PROFILE, Scope.READ_GEOMETRY},
    )
    assert "aerial-survey" in audience_for(FocusKind.GEOMETRY, (*active_descriptors(), newcomer))


async def test_open_questions_from_the_bank_reach_the_focus() -> None:
    """The whole point of the feature, in one assertion.

    The thread was opened in March by a slow-loop agent that could not finish.
    Nothing in the incident loop has ever read it. It arrives on the recorder's
    focus, by id, with the date it was opened in the reason.
    """
    bank = _bank()
    thread = await _thread(bank)
    focus = await _interceptor(memory=bank).compose_focus(
        incident_id=INCIDENT,
        snapshot=_snapshot(),
        now=NOW,
        reading=_reading(),
        authorised_scopes=PUBLIC_READER,
    )
    assert focus is not None
    assert thread.question_id in focus.open_question_ids

    recorder = focus.for_agent("incident-recorder")
    assert recorder is not None
    threads = [p for p in recorder.pointers if p.kind is FocusKind.OPEN_QUESTION]
    assert [p.ref for p in threads] == [thread.question_id]
    assert "2026-03-04" in threads[0].reason


async def test_the_collision_reaches_each_agent_that_can_act_on_it() -> None:
    bank = _bank()
    await _thread(bank)
    focus = await _interceptor(memory=bank).compose_focus(
        incident_id=INCIDENT,
        snapshot=_snapshot(),
        now=NOW,
        reading=_reading(),
        authorised_scopes=PUBLIC_READER,
    )
    assert focus is not None
    assert set(focus.agent_ids) == {"agency-notifier", "incident-recorder", "sensor-fusion"}

    fusion = focus.for_agent("sensor-fusion")
    assert fusion is not None
    assert CONFLICT in fusion.refs
    # It reads geometry, so it is told the measured picture is what a disputed
    # storey count would move. The notifier, which does not, is not.
    assert f"geometry/{ADDRESS}" in fusion.refs

    notifier = focus.for_agent("agency-notifier")
    assert notifier is not None
    assert f"geometry/{ADDRESS}" not in notifier.refs
    assert not [p for p in notifier.pointers if p.kind is FocusKind.OPEN_QUESTION]


@pytest.mark.authorization
async def test_a_grant_without_the_read_scope_recalls_nothing() -> None:
    """Recall is gated on the incident grant, and the gate is the bank's."""
    bank = _bank()
    await _thread(bank)
    focus = await _interceptor(memory=bank).compose_focus(
        incident_id=INCIDENT,
        snapshot=_snapshot(),
        now=NOW,
        reading=_reading(),
        authorised_scopes=frozenset({Scope.READ_GEOMETRY}),
    )
    assert focus is not None
    assert focus.open_question_ids == ()


# -------------------------------------------------- it never asserts a value


@pytest.mark.invariant
async def test_every_reference_in_the_focus_came_off_the_snapshot() -> None:
    """The closed-list check, end to end. No pointer names anything else."""
    bank = _bank()
    thread = await _thread(bank)
    snapshot = _snapshot()
    focus = await _interceptor(memory=bank).compose_focus(
        incident_id=INCIDENT,
        snapshot=snapshot,
        now=NOW,
        reading=_reading(),
        authorised_scopes=PUBLIC_READER,
    )
    assert focus is not None
    scope = focus_scope(snapshot, questions=(thread,))
    assert focus.unresolved_against(scope) == ()
    assert focus.profile_version == snapshot.profile_version


@pytest.mark.invariant
async def test_nothing_the_caller_said_reaches_the_focus() -> None:
    bank = _bank()
    await _thread(bank)
    focus = await _interceptor(memory=bank).compose_focus(
        incident_id=INCIDENT,
        snapshot=_snapshot(),
        now=NOW,
        reading=_reading(),
        authorised_scopes=PUBLIC_READER,
    )
    assert focus is not None
    rendered = focus.model_dump_json()
    for fragment in (REPORTED_FLOOR, "third", "three storey", TRANSCRIPT):
        assert fragment not in rendered


@pytest.mark.invariant
async def test_a_planner_that_invents_an_option_changes_nothing() -> None:
    """The planner picks from a closed list. An answer off it is discarded."""
    bank = _bank()
    await _thread(bank)
    kwargs = {
        "incident_id": INCIDENT,
        "snapshot": _snapshot(),
        "now": NOW,
        "reading": _reading(),
        "authorised_scopes": PUBLIC_READER,
    }
    honest = await _interceptor(memory=bank).compose_focus(**kwargs)  # type: ignore[arg-type]
    lying = await _interceptor(memory=bank, planner=_InventingPlanner()).compose_focus(
        **kwargs  # type: ignore[arg-type]
    )
    assert honest is not None and lying is not None
    assert honest.refs == lying.refs


async def test_a_planner_may_reorder_and_may_do_nothing_else() -> None:
    bank = _bank()
    await _thread(bank)
    planner = _ReversingPlanner()
    kwargs = {
        "incident_id": INCIDENT,
        "snapshot": _snapshot(),
        "now": NOW,
        "reading": _reading(),
        "authorised_scopes": PUBLIC_READER,
    }
    default = await _interceptor(memory=bank).compose_focus(**kwargs)  # type: ignore[arg-type]
    reordered = await _interceptor(memory=bank, planner=planner).compose_focus(
        **kwargs  # type: ignore[arg-type]
    )
    assert default is not None and reordered is not None
    assert planner.seen, "the planner was consulted"
    # Every option it was offered was a collision key this pass derived.
    derived = {c.key for c in detect_collisions(_snapshot(), reported_keys=(), questions=())}
    for options in planner.seen:
        assert not set(options) - derived - {f"question:{q}" for q in default.open_question_ids}
    # Same references, whatever order they were considered in.
    assert set(default.refs) == set(reordered.refs)


# ------------------------------------------------ running out is a state


@pytest.mark.degraded
async def test_budget_exhaustion_yields_the_deterministic_fallback() -> None:
    """One step is enough to gather and nothing else. It still emits a focus."""
    bank = _bank()
    await _thread(bank)
    focus = await _interceptor(memory=bank, max_graph_steps=1).compose_focus(
        incident_id=INCIDENT,
        snapshot=_snapshot(),
        now=NOW,
        reading=_reading(),
        authorised_scopes=PUBLIC_READER,
    )
    assert focus is not None
    assert focus.pointer_count > 0
    assert CONFLICT in focus.refs


@pytest.mark.degraded
async def test_a_planner_that_raises_costs_the_order_and_not_the_focus() -> None:
    bank = _bank()
    thread = await _thread(bank)
    focus = await _interceptor(memory=bank, planner=_RaisingPlanner()).compose_focus(
        incident_id=INCIDENT,
        snapshot=_snapshot(),
        now=NOW,
        reading=_reading(),
        authorised_scopes=PUBLIC_READER,
    )
    assert focus is not None
    assert CONFLICT in focus.refs
    # The recall node had already run, so the thread is not lost with the graph.
    assert thread.question_id in focus.open_question_ids


@pytest.mark.degraded
async def test_an_unreachable_bank_costs_the_questions_and_never_the_pass() -> None:
    class _DeadBank:
        async def recall(self, **_: object):
            raise TimeoutError("firestore is unreachable")

    focus = await _interceptor(memory=_DeadBank()).compose_focus(
        incident_id=INCIDENT,
        snapshot=_snapshot(),
        now=NOW,
        reading=_reading(),
        authorised_scopes=PUBLIC_READER,
    )
    assert focus is not None
    assert focus.open_question_ids == ()
    assert CONFLICT in focus.refs


@pytest.mark.degraded
def test_the_fallback_ranks_the_profiles_own_conflicts_and_questions() -> None:
    """No graph at all, and still a focus an officer can act on."""
    focus = fallback_focus(
        incident_id=INCIDENT,
        snapshot=_snapshot(),
        descriptors=active_descriptors(),
        reported_keys=(IntakeKeys.REPORTED_FLOOR_OF_ORIGIN,),
        composed_by_version="1.0.0",
        composed_at=NOW,
    )
    assert CONFLICT in focus.refs
    fusion = focus.for_agent("sensor-fusion")
    assert fusion is not None
    assert fusion.by_priority()[0].priority <= 2


def test_no_collaborators_means_no_focus_and_no_change() -> None:
    """The default. Byte-identical to the interceptor that shipped before this."""
    assert _interceptor(memory=None).composes_focus is False


async def test_a_deployment_that_wired_nothing_composes_nothing() -> None:
    assert (
        await IncidentInterceptor(
            intake=IntakeReader(model=_NoModel(), screen=LocalInjectionDetector())
        ).compose_focus(incident_id=INCIDENT, snapshot=_snapshot(), now=NOW)
        is None
    )


# ------------------------------------------------------ ADR 0004 is absolute


@pytest.mark.invariant
def test_the_instant_stage_refuses_a_model() -> None:
    """The type, not the ordering, is what makes this unconditional."""
    with pytest.raises(ValidationError):
        BriefEmission(
            emission_id="emit_1",
            incident_id=INCIDENT,
            version=1,
            stage=BriefStage.INSTANT,
            model_invoked=True,
            profile_snapshot_id="snap-7",
            produced_at=NOW,
        )


@pytest.mark.invariant
async def test_the_instant_brief_is_untouched_by_a_graph_that_fails() -> None:
    """Composed after the brief is on the record, so it cannot reach it.

    The emission here is already sealed and persisted -- exactly the state it is
    in when ``compose_focus`` is called on a real dispatch. The graph is then
    given a planner that raises. The brief's content hash, version, stage and
    ``model_invoked`` flag are all unchanged afterwards, which is the property
    ADR 0004 asks for.
    """
    instant = BriefEmission(
        emission_id="emit_1",
        incident_id=INCIDENT,
        version=1,
        stage=BriefStage.INSTANT,
        profile_snapshot_id="snap-7",
        produced_at=NOW,
    ).sealed()
    persisted = instant.mark_persisted(at=NOW)
    before = persisted.content_hash

    bank = _bank()
    await _thread(bank)
    focus = await _interceptor(memory=bank, planner=_RaisingPlanner()).compose_focus(
        incident_id=INCIDENT,
        snapshot=_snapshot(),
        now=NOW,
        reading=_reading(),
        authorised_scopes=PUBLIC_READER,
    )

    assert focus is not None, "a failed graph still produces a focus"
    assert persisted.content_hash == before
    assert persisted.stage is BriefStage.INSTANT
    assert persisted.model_invoked is False
    assert persisted.narrative is None
    assert persisted.require_persisted() is persisted


# ------------------------------------------------------------------- spans


@pytest.mark.invariant
@pytest.mark.usefixtures("recording_spans")
async def test_spans_carry_node_names_decisions_and_counts_only() -> None:
    bank = _bank()
    await _thread(bank)
    focus = await _interceptor(memory=bank).compose_focus(
        incident_id=INCIDENT,
        snapshot=_snapshot(),
        now=NOW,
        reading=_reading(),
        authorised_scopes=PUBLIC_READER,
    )
    assert focus is not None

    spans = [s for s in TRACER.spans if s.name == f"agent.{AGENT_ID}"]
    assert spans, "every node emits a span"
    assert {str(s.attributes["graph_node"]) for s in spans} >= {"gather", "recall", "collide"}
    for span in spans:
        assert "graph.decision" in span.attributes
        rendered = " ".join(str(v) for v in span.attributes.values())
        for fragment in (REPORTED_FLOOR, "three storey", TRANSCRIPT, "propane"):
            assert fragment not in rendered


# -------------------------------------------------------------- persistence


async def test_the_focus_persists_to_the_log_and_reads_back() -> None:
    log = InMemoryIncidentLogRepository()
    bank = _bank()
    await _thread(bank)
    interceptor = _interceptor(memory=bank, incident_log=log)

    focus = await interceptor.compose_focus(
        incident_id=INCIDENT,
        snapshot=_snapshot(),
        now=NOW,
        reading=_reading(),
        authorised_scopes=PUBLIC_READER,
    )
    assert focus is not None
    entry = await interceptor.record_focus(focus, now=NOW)
    assert entry is not None
    assert entry.sequence == 0

    stored = await read_focus(log, INCIDENT)
    assert stored is not None
    assert stored.refs == focus.refs
    assert stored.profile_version == focus.profile_version
    assert TRANSCRIPT not in str(entry.content)


async def test_recording_without_a_log_is_not_an_error() -> None:
    """A routing unit test wires no log. Composing must still be callable."""
    bank = _bank()
    focus = await _interceptor(memory=bank).compose_focus(
        incident_id=INCIDENT, snapshot=_snapshot(), now=NOW, authorised_scopes=PUBLIC_READER
    )
    assert focus is not None
    assert await _interceptor(memory=bank).record_focus(focus, now=NOW) is None


# ------------------------------------------------------------- the router


def test_the_router_parks_before_it_starts_work_it_cannot_finish() -> None:
    exhausted = BudgetGuard(seconds=0.0, max_steps=4, monotonic=lambda: 1.0)
    composer = FocusComposer(snapshot=_snapshot(), budget=exhausted)
    assert (
        composer.route(
            FocusGraphState(district_id=DISTRICT, incident_id=INCIDENT, address_id=ADDRESS)
        )
        == "park"
    )


def test_the_router_stops_once_the_graph_has_stopped() -> None:
    composer = FocusComposer(snapshot=_snapshot(), budget=BudgetGuard(seconds=60.0))
    state = FocusGraphState(
        district_id=DISTRICT, incident_id=INCIDENT, address_id=ADDRESS, stop=GraphStop.CLOSED
    )
    assert composer.route(state) == STOP


def test_the_router_enters_at_gather() -> None:
    composer = FocusComposer(snapshot=_snapshot(), budget=BudgetGuard(seconds=60.0))
    assert composer.spec().entry == NODE_GATHER


# ------------------------------------------------------------- with LangGraph


async def test_langgraph_runs_the_same_nodes_to_the_same_chain() -> None:
    """The framework is an executor. Swapping it in must change nothing."""
    pytest.importorskip("langgraph")

    async def _run(use_langgraph: bool):
        composer = FocusComposer(snapshot=_snapshot(), budget=BudgetGuard(seconds=60.0))
        return await run_graph(
            composer.spec(),
            FocusGraphState(
                district_id=DISTRICT,
                incident_id=INCIDENT,
                address_id=ADDRESS,
                reported_keys=(IntakeKeys.REPORTED_FLOOR_OF_ORIGIN,),
            ),
            agent_id=AGENT_ID,
            agent_version="1.0.0",
            budget=BudgetGuard(seconds=60.0),
            request_digest="digest",
            use_langgraph=use_langgraph,
        )

    builtin = await _run(False)
    compiled = await _run(True)
    assert builtin.trace.decisions == compiled.trace.decisions
    assert builtin.trace.stop is compiled.trace.stop is GraphStop.CLOSED
    assert builtin.state.selected == compiled.state.selected


async def test_a_routed_agent_is_woken_by_the_plan_and_not_by_the_incident_opening() -> None:
    """The plan is the only thing that starts a routed agent, in both topologies.

    ``plan_handoffs`` decides who runs by matching a rule's required capability
    and scopes against what each descriptor declares, and *withholds* the wake
    when the incident grant cannot cover them. In one process that decision was
    enforced, because the interceptor called the runner. Across eleven Cloud Run
    services it was not: an agent subscribed to ``incident.opened`` was started
    by Pub/Sub whatever the plan said, so a handoff withheld for a missing scope
    ran anyway while the log recorded a refusal that never happened.

    Asserted on the routing map rather than on a delivery, because the map is
    what Terraform builds the subscriptions from -- and the subscription is
    where the bypass lived.
    """
    from firstdue.domain.events import Topic
    from firstdue.registry.routing import CONSUMES

    notifier = CONSUMES["agency-notifier"]
    assert Topic.AGENT_WAKE in notifier
    assert (
        Topic.INCIDENT_OPENED not in notifier
    ), "a routed agent subscribed to the incident opening runs whatever the plan decided"


async def test_the_recorder_still_hears_everything() -> None:
    """Completeness is the log's job, and it is not a routing decision.

    The recorder keeps its blanket subscription on purpose: a log that only
    recorded the incidents somebody routed it to would have holes in exactly
    the place a routing mistake happened. It reads and writes the log and
    nothing else, so there is no authority here for a plan to gate.
    """
    from firstdue.domain.events import Topic
    from firstdue.registry.routing import CONSUMES

    assert Topic.INCIDENT_OPENED in CONSUMES["incident-recorder"]
    assert Topic.INCIDENT_CLOSED in CONSUMES["incident-recorder"]
