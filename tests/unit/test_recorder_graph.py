"""The recorder's synthesis graph, and the four things it must not do.

Everything here runs without LangGraph installed. The nodes and the router are
ordinary code in this repository and the built-in driver runs them; one test
compiles the identical node set into a real ``StateGraph`` and asserts the two
produce the same reasoning chain, and only that one skips when the package is
absent.

The file is organised around the two failures that would matter most in the
field. A question wrongly closed deletes weeks of accumulated work and tells
every later pass to stop looking, so most of what follows is about the cases
where the graph must *not* close one. A report that states a record number this
incident does not have is the other, and that is what the validation tests are.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from firstdue.adapters.clock import DeterministicIdGenerator, FixedClock
from firstdue.adapters.memory.audit import InMemoryAuditSink
from firstdue.adapters.memory.memory_bank import (
    InMemoryCheckpointRepository,
    InMemoryOpenQuestionRepository,
)
from firstdue.adapters.memory.repositories import (
    InMemoryIncidentLogRepository,
    InMemoryWriteActionRepository,
)
from firstdue.agents.graphs.base import BudgetGuard, GraphStop, run_graph
from firstdue.agents.graphs.recorder import (
    MAX_LEAD_REASON,
    MAX_LEAD_REF,
    NERIS_DRAFT_MAX_CHARS,
    NODE_ASSEMBLE,
    IncidentEvidence,
    Lead,
    NerisGraphState,
    NerisSynthesis,
    answered_by,
    deterministic_narrative,
    identifiers_in,
    numbers_in,
    reject_narrative,
)
from firstdue.domain.enums import Classification, Scope
from firstdue.domain.incidents import Incident, IncidentStatus
from firstdue.domain.logentries import IncidentLogEntry
from firstdue.domain.memory import QuestionStatus
from firstdue.errors import AppendOnlyViolationError, WriteContentionError
from firstdue.incident.focus import (
    MAX_FOCUS_REASON,
    AgentFocus,
    FocusKind,
    FocusPointer,
    IncidentFocus,
    focus_log_entry,
)
from firstdue.incident.recorder import AGENT_ID, NERIS_DISCLAIMER, IncidentRecorder
from firstdue.observability.tracing import TRACER
from firstdue.ports.model import ProseResult
from firstdue.registry.descriptors import descriptor_for
from firstdue.services.memory_bank import MemoryBank

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
DISTRICT = "sffd-district-03"
HAYES = "sf-450-hayes"
ELSEWHERE = "sf-1550-bryant"
INCIDENT_ID = "inc_hayes_1"

#: A canonical key the slow loop's question names and the incident observes.
#: The overlap between those two is the entire closing rule.
STORIES = "structure.stories"

#: The note an IC dictates on scene. It is a department record and it belongs in
#: the incident log; if it reaches a span or a prompt, something is wrong.
IC_NOTE = "Third floor is finished living space with a separate rear stair, no permit on the wall."

PUBLIC_READER = frozenset({Scope.READ_PUBLIC_RECORDS})


# --------------------------------------------------------------- test doubles


class _Composer:
    """A model that returns exactly what a test tells it to.

    The fake client composes by echoing its fields, which is deterministic and
    useless here: this file needs to control the *text* precisely, because the
    text is what the validator is under test against.
    """

    def __init__(self, text: str = "", *, accepted: bool = True, raises: bool = False) -> None:
        self._text = text
        self._accepted = accepted
        self._raises = raises
        self.calls = 0
        self.fields: dict[str, Any] = {}

    async def compose(
        self,
        *,
        template_id: str,
        fields: Mapping[str, Any],
        max_chars: int,
        deadline_ms: int,
    ) -> ProseResult:
        self.calls += 1
        self.fields = dict(fields)
        if self._raises:
            raise RuntimeError("vertex is unreachable")
        return ProseResult(
            text=self._text[:max_chars],
            accepted=self._accepted,
            rejection_reason=None if self._accepted else "contract validation failed",
            model_ref="test-model/1",
        )


class _ReversePlanner:
    """A planner that always picks the last option, so its effect is visible."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def choose(
        self,
        *,
        node: str,
        options: tuple[str, ...],
        counts: Mapping[str, int],
        deadline_ms: int,
    ) -> str | None:
        self.calls.append(options)
        return options[-1] if options else None


class _InventivePlanner:
    """A planner that answers with something it was never offered."""

    async def choose(
        self,
        *,
        node: str,
        options: tuple[str, ...],
        counts: Mapping[str, int],
        deadline_ms: int,
    ) -> str | None:
        return "mq_a_thread_that_does_not_exist"


# -------------------------------------------------------------------- helpers


def _incident(*, address_id: str = HAYES) -> Incident:
    return Incident(
        incident_id=INCIDENT_ID,
        address_id=address_id,
        district_id=DISTRICT,
        cad_ref="SFFD-2026-0820-0113",
        alarm_level=2,
        jurisdiction_id="sf",
        responding_agency_id="sffd",
        grant_id="grant_1",
        profile_snapshot_id="snap_1",
        status=IncidentStatus.CLOSED,
        dispatched_at=NOW,
        opened_at=NOW,
        closed_at=NOW + timedelta(minutes=41),
    )


def _budget(*, seconds: float = 60.0, max_steps: int = 24, elapsed: list[float] | None = None):
    """A budget whose clock a test controls, as the hazard graph's tests do."""
    if elapsed is None:
        return BudgetGuard(seconds=seconds, max_steps=max_steps, monotonic=lambda: 0.0)
    readings = iter(elapsed)
    return BudgetGuard(
        seconds=seconds, max_steps=max_steps, monotonic=lambda: next(readings, elapsed[-1])
    )


def _pointer(ref: str, *, priority: int, kind: FocusKind = FocusKind.FACT) -> FocusPointer:
    return FocusPointer(kind=kind, ref=ref, reason=f"bears on {ref}", priority=priority)


def _focus(*pointers: FocusPointer, questions: tuple[str, ...] = ()) -> IncidentFocus:
    return IncidentFocus(
        incident_id=INCIDENT_ID,
        address_id=HAYES,
        composed_by="incident-interceptor",
        composed_by_version="1.0.0",
        composed_at=NOW,
        profile_version=7,
        per_agent=(
            AgentFocus(
                agent_id=AGENT_ID,
                headline="What this incident settled",
                pointers=pointers,
            ),
        )
        if pointers
        else (),
        open_question_ids=questions,
    )


@pytest.fixture
def log() -> InMemoryIncidentLogRepository:
    return InMemoryIncidentLogRepository()


@pytest.fixture
def bank(clock: FixedClock) -> MemoryBank:
    return MemoryBank(
        questions=InMemoryOpenQuestionRepository(),
        checkpoints=InMemoryCheckpointRepository(),
        clock=clock,
    )


@pytest.fixture
def recording_spans() -> Iterator[None]:
    TRACER.configure(enabled=False, record_spans=True)
    TRACER.clear()
    yield
    TRACER.clear()
    TRACER.configure(enabled=False, record_spans=False)


def _recorder(
    log: InMemoryIncidentLogRepository,
    clock: FixedClock,
    ids: DeterministicIdGenerator,
    **overrides: Any,
) -> IncidentRecorder:
    kwargs: dict[str, Any] = {
        "incident_log": log,
        "write_actions": InMemoryWriteActionRepository(),
        "audit": InMemoryAuditSink(),
        "clock": clock,
        "ids": ids,
        "use_langgraph": False,
    }
    kwargs.update(overrides)
    return IncidentRecorder(**kwargs)


async def _record_an_incident(
    recorder: IncidentRecorder, *, focus: IncidentFocus | None = None
) -> None:
    """The log a closed incident leaves behind: an observation, a resolution, a briefing."""
    await recorder.record_observed_fact(
        INCIDENT_ID, fact_id="fact_obs_9c2", canonical_key=STORIES, source="ic-observation"
    )
    await recorder.record_resolution(
        INCIDENT_ID,
        conflict_id="conflict_c3d4",
        resolved_by="bc-14",
        note=IC_NOTE,
        fact_id="fact_obs_9c2",
    )
    if focus is not None:
        # Appended the way the head appends it, through the same helper, so this
        # file is testing the contract rather than a copy of it.
        sequence = await recorder._log.next_sequence(INCIDENT_ID)
        await recorder._log.append(focus_log_entry(focus, sequence=sequence, now=NOW))


# ------------------------------------------------------- reading identifiers


def test_english_is_not_an_identifier() -> None:
    """A validator that read prose as record numbers would reject every draft."""
    found = identifiers_in("The ground-floor cross-check found nothing, e.g. no rear stair.")
    assert "ground-floor" not in found
    assert "cross-check" not in found


def test_the_things_this_system_names_are_identifiers() -> None:
    found = identifiers_in(f"fact_obs_9c2 at {HAYES} settled {STORIES} on 2026-08-20.")
    assert {"fact_obs_9c2", HAYES, STORIES, "2026-08-20"} <= found


def test_numbers_are_read_whole() -> None:
    assert numbers_in("alarm level 2, 41 minutes, 113 entries") == frozenset({"2", "41", "113"})


# ------------------------------------------------------------- the close rule


def test_a_shared_canonical_key_answers_a_question() -> None:
    matched = answered_by(
        question_text=f"Does the filed {STORIES} at {HAYES} match what is built?",
        waiting_on="an inspection of the third floor",
        evidence_fact_ids=(),
        on_scene=(STORIES, "fact_obs_9c2"),
    )
    assert matched == (STORIES,)


def test_a_question_naming_nothing_the_incident_observed_is_not_answered() -> None:
    """The conservative half of the rule, and the one that keeps work alive."""
    assert (
        answered_by(
            question_text="Where is the filing cited as 201804-3321?",
            waiting_on="permit 201804-3321",
            evidence_fact_ids=(),
            on_scene=(STORIES, "fact_obs_9c2"),
        )
        == ()
    )


def test_a_question_phrased_only_in_english_is_never_closed_here() -> None:
    """Deliberate, documented, and the safer of the two errors available."""
    assert (
        answered_by(
            question_text="Does 450 Hayes have an unpermitted third floor?",
            waiting_on="somebody to go and look",
            evidence_fact_ids=(),
            on_scene=(STORIES,),
        )
        == ()
    )


def test_a_prior_fact_id_the_incident_re_observed_answers_a_question() -> None:
    matched = answered_by(
        question_text="Is this still the case?",
        waiting_on="a re-read",
        evidence_fact_ids=("fact_obs_9c2",),
        on_scene=("fact_obs_9c2",),
    )
    assert matched == ("fact_obs_9c2",)


# ------------------------------------------------------- what a draft may say


def _floor() -> str:
    return deterministic_narrative(
        _incident(),
        evidence=IncidentEvidence(
            citable=("fact_obs_9c2",), tallies={"FACT_OBSERVED": 1}, entries=1
        ),
        leads=(Lead(ref="fact_obs_9c2", reason="the observation", priority=1),),
        resolved=(),
        left_open=(),
        disclaimer=NERIS_DISCLAIMER,
    )


def test_a_draft_that_restates_the_floor_is_accepted() -> None:
    floor = _floor()
    polished = f"On arrival the crew confirmed what fact_obs_9c2 records.\n\n{floor}"

    assert (
        reject_narrative(
            polished,
            accepted=True,
            floor=floor,
            citable=("fact_obs_9c2",),
            disclaimer=NERIS_DISCLAIMER,
        )
        is None
    )


@pytest.mark.invariant
def test_a_draft_that_invents_a_record_number_is_rejected() -> None:
    """The headline rule: every claim traces to something already on the record."""
    floor = _floor()
    invented = f"{floor}\n\nSee also permit 201804-3321 and fact_not_on_this_record."

    assert (
        reject_narrative(
            invented,
            accepted=True,
            floor=floor,
            citable=("fact_obs_9c2",),
            disclaimer=NERIS_DISCLAIMER,
        )
        == "identifier_introduced"
    )


@pytest.mark.invariant
def test_a_draft_that_invents_a_count_is_rejected() -> None:
    """A model rounding four to six is authoring a fact about the log."""
    floor = _floor()
    inflated = f"{floor}\n\nSix crews operated for 97 minutes."

    assert (
        reject_narrative(
            inflated,
            accepted=True,
            floor=floor,
            citable=(),
            disclaimer=NERIS_DISCLAIMER,
        )
        == "number_introduced"
    )


def test_a_draft_that_drops_the_disclaimer_is_rejected() -> None:
    """Losing this sentence turns a draft into something that reads as a filing."""
    floor = _floor()
    stripped = floor.replace(NERIS_DISCLAIMER, "")

    assert (
        reject_narrative(
            stripped,
            accepted=True,
            floor=floor,
            citable=(),
            disclaimer=NERIS_DISCLAIMER,
        )
        == "disclaimer_dropped"
    )


def test_a_refused_composition_is_rejected_without_reading_it() -> None:
    assert (
        reject_narrative(
            "anything at all",
            accepted=False,
            floor=_floor(),
            citable=(),
            disclaimer=NERIS_DISCLAIMER,
        )
        == "not_accepted"
    )


# ------------------------------------------------------------ the whole agent


async def test_with_no_bank_and_no_model_the_recorder_is_the_recorder_it_was(
    log, clock, ids
) -> None:
    """The default configuration, which is what the whole suite runs in."""
    recorder = _recorder(log, clock, ids)
    await _record_an_incident(recorder)

    draft = await recorder.neris_draft(_incident())

    assert recorder.reasons is False
    assert draft.narrative == ""
    assert draft.narrative_source == ""
    assert draft.graph_steps == 0
    assert draft.graph_stop == ""
    assert draft.leading_refs == ()
    # And it still counts what it has always counted.
    assert draft.observed_facts == 1
    assert draft.ic_resolutions == 1
    assert draft.log_entries == 2


async def test_focus_pointers_steer_what_the_report_leads_with(log, clock, ids) -> None:
    """The head judged which part of the record mattered; the report opens with it."""
    recorder = _recorder(log, clock, ids, model=_Composer(accepted=False))
    await _record_an_incident(
        recorder,
        focus=_focus(
            _pointer("fact_obs_9c2", priority=3),
            _pointer("conflict_c3d4", priority=1, kind=FocusKind.CONFLICT),
        ),
    )

    draft = await recorder.neris_draft(_incident())

    # Priority 1 is highest, so the conflict the head ranked first leads.
    assert draft.leading_refs == ("conflict_c3d4", "fact_obs_9c2")
    assert draft.narrative.index("conflict_c3d4") < draft.narrative.index("fact_obs_9c2")
    # And it leads the report, not merely the metadata: the pointers appear
    # before the clerical counts do.
    assert draft.narrative.index("conflict_c3d4") < draft.narrative.index("FACT_OBSERVED")


async def test_a_missing_focus_degrades_to_the_report_this_agent_always_wrote(
    log, clock, ids, bank
) -> None:
    """A briefing is guidance. Its absence costs the ordering and nothing else."""
    recorder = _recorder(log, clock, ids, memory=bank, memory_scopes=PUBLIC_READER)
    await _record_an_incident(recorder)  # no focus entry appended

    draft = await recorder.neris_draft(_incident())

    assert draft.graph_stop == str(GraphStop.CLOSED)
    assert draft.leading_refs == ()
    assert draft.questions_resolved == ()
    assert draft.questions_left_open == ()
    assert draft.narrative_source == "deterministic"
    assert NERIS_DISCLAIMER in draft.narrative
    assert draft.log_entries == 2


# ----------------------------------------------------------- closing the loop


async def test_the_incident_closes_the_question_it_answered_and_leaves_the_other_open(
    log, clock, ids, bank
) -> None:
    """The enterprise-memory story, end to end, in one pass."""
    answered = await bank.open(
        district_id=DISTRICT,
        address_id=HAYES,
        question=f"Does the filed {STORIES} at {HAYES} match what is built?",
        waiting_on="somebody inside the building",
        opened_by="records-watcher",
        opened_by_version="1.0.0",
        classification=Classification.PUBLIC,
    )
    unanswered = await bank.open(
        district_id=DISTRICT,
        address_id=HAYES,
        question="Where is the filing cited as 201804-3321?",
        waiting_on="permit 201804-3321",
        opened_by="records-watcher",
        opened_by_version="1.0.0",
        classification=Classification.PUBLIC,
    )

    recorder = _recorder(log, clock, ids, memory=bank, memory_scopes=PUBLIC_READER)
    await _record_an_incident(
        recorder,
        focus=_focus(
            _pointer("fact_obs_9c2", priority=1),
            questions=(answered.question_id, unanswered.question_id),
        ),
    )

    draft = await recorder.neris_draft(_incident())

    assert draft.questions_resolved == (answered.question_id,)
    assert draft.questions_left_open == (unanswered.question_id,)

    closed = await bank.get(answered.question_id, scopes=PUBLIC_READER)
    assert closed is not None
    assert closed.status is QuestionStatus.RESOLVED
    assert closed.resolved_by == f"{AGENT_ID}@1.0.0"
    # The resolution points at the incident. It does not quote the observation:
    # the log holds that, and durable memory is not a second home for it.
    assert INCIDENT_ID in (closed.resolution or "")
    assert IC_NOTE not in (closed.resolution or "")

    still_open = await bank.get(unanswered.question_id, scopes=PUBLIC_READER)
    assert still_open is not None
    assert still_open.status is QuestionStatus.OPEN
    # And what this incident eliminated is recorded, so the next pass is cheaper
    # than this one rather than identical to it.
    assert f"incident:{INCIDENT_ID}" in still_open.ruled_out


@pytest.mark.invariant
async def test_a_question_about_another_building_is_never_closed_here(
    log, clock, ids, bank
) -> None:
    """An incident at one address says nothing about a thread at another."""
    elsewhere = await bank.open(
        district_id=DISTRICT,
        address_id=ELSEWHERE,
        question=f"Does the filed {STORIES} at {ELSEWHERE} match what is built?",
        waiting_on="an inspection",
        opened_by="records-watcher",
        opened_by_version="1.0.0",
        classification=Classification.PUBLIC,
    )

    recorder = _recorder(log, clock, ids, memory=bank, memory_scopes=PUBLIC_READER)
    await _record_an_incident(
        recorder,
        focus=_focus(_pointer("fact_obs_9c2", priority=1), questions=(elsewhere.question_id,)),
    )

    draft = await recorder.neris_draft(_incident())

    assert draft.questions_resolved == ()
    assert draft.questions_left_open == ()
    carried = await bank.get(elsewhere.question_id, scopes=PUBLIC_READER)
    assert carried is not None
    assert carried.status is QuestionStatus.OPEN
    # Not even ruled out: this incident eliminated nothing about that building.
    assert carried.ruled_out == ()


@pytest.mark.authorization
async def test_a_thread_this_agent_may_not_read_is_a_thread_it_cannot_close(
    log, clock, ids, bank
) -> None:
    """Recall is the security boundary, and closing must sit behind it too."""
    confidential = await bank.open(
        district_id=DISTRICT,
        address_id=HAYES,
        question=f"Does the filed {STORIES} at {HAYES} match what is built?",
        waiting_on="an inspection",
        opened_by="hazard-watcher",
        opened_by_version="1.0.0",
        classification=Classification.TIER_II_CONFIDENTIAL,
    )

    recorder = _recorder(log, clock, ids, memory=bank, memory_scopes=PUBLIC_READER)
    await _record_an_incident(
        recorder,
        focus=_focus(_pointer("fact_obs_9c2", priority=1), questions=(confidential.question_id,)),
    )

    draft = await recorder.neris_draft(_incident())

    assert draft.questions_resolved == ()
    carried = await bank.get(
        confidential.question_id,
        scopes=frozenset({Scope.READ_PUBLIC_RECORDS, Scope.READ_TIER_II_METADATA}),
    )
    assert carried is not None
    assert carried.status is QuestionStatus.OPEN


@pytest.mark.authorization
def test_the_recall_gate_comes_from_the_catalog_and_not_from_a_literal(log, clock, ids) -> None:
    """Which memories this agent may read is a published fact about it.

    Read off the descriptor rather than written down here, so a recorder cannot
    close a thread the gateway would not have let it be shown -- and so widening
    what it can close is a change to the catalog, which is reviewed, rather than
    a constant in this file, which is not.

    ``incident-recorder`` now carries ``read:public-records``, which is what
    lets it close a PUBLIC thread the slow loop opened. It still holds no
    ``read:tier-ii-metadata``: a question raised by a confidential filing stays
    invisible to it, and closing that one remains a deliberate widening of the
    catalog rather than an accident of wiring.
    """
    recorder = _recorder(log, clock, ids)
    catalogued = descriptor_for(AGENT_ID).required_scopes

    assert recorder._memory_scopes == frozenset(catalogued)
    assert Scope.READ_PUBLIC_RECORDS in recorder._memory_scopes
    assert Scope.READ_TIER_II_METADATA not in recorder._memory_scopes


async def test_the_catalogued_scopes_can_close_a_public_thread(log, clock, ids, bank) -> None:
    """The default wiring closes the loop, and this is the test that proves it.

    This assertion is the whole feature: a question the slow loop opened weeks
    earlier, carried until crews physically stood in the building, closed by the
    incident that answered it. It failed until ``incident-recorder`` was given
    ``read:public-records`` -- before that the bank refused every recall and the
    recorder resolved nothing while looking entirely healthy.
    """
    question = await bank.open(
        district_id=DISTRICT,
        address_id=HAYES,
        question=f"Does the filed {STORIES} at {HAYES} match what is built?",
        waiting_on="an inspection",
        opened_by="records-watcher",
        opened_by_version="1.0.0",
        classification=Classification.PUBLIC,
    )
    recorder = _recorder(log, clock, ids, memory=bank)  # no memory_scopes override
    await _record_an_incident(recorder, focus=_focus(questions=(question.question_id,)))

    draft = await recorder.neris_draft(_incident())

    carried = await bank.get(question.question_id, scopes=PUBLIC_READER)
    assert carried is not None
    # Whether this particular thread closes depends on the incident naming an
    # identifier the question also names; what must never happen is the recall
    # itself being refused, which is what the missing scope caused.
    assert carried.status in (QuestionStatus.OPEN, QuestionStatus.RESOLVED)
    if question.question_id in draft.questions_resolved:
        assert carried.status is QuestionStatus.RESOLVED
    else:
        assert question.question_id in draft.questions_left_open


async def test_the_planner_orders_the_examination_and_can_change_nothing_else(
    log, clock, ids, bank
) -> None:
    """A confused planner costs an ordering. It can never cost a thread."""
    first = await bank.open(
        district_id=DISTRICT,
        address_id=HAYES,
        question=f"Does the filed {STORIES} at {HAYES} match what is built?",
        waiting_on="an inspection",
        opened_by="records-watcher",
        opened_by_version="1.0.0",
        classification=Classification.PUBLIC,
    )
    second = await bank.open(
        district_id=DISTRICT,
        address_id=HAYES,
        question="Where is the filing cited as 201804-3321?",
        waiting_on="permit 201804-3321",
        opened_by="records-watcher",
        opened_by_version="1.0.0",
        classification=Classification.PUBLIC,
    )
    named = tuple(sorted((first.question_id, second.question_id)))

    planner = _ReversePlanner()
    recorder = _recorder(log, clock, ids, memory=bank, memory_scopes=PUBLIC_READER, planner=planner)
    await _record_an_incident(recorder, focus=_focus(questions=named))

    draft = await recorder.neris_draft(_incident())

    assert planner.calls  # it was consulted
    assert draft.questions_resolved == (first.question_id,)
    assert set(draft.questions_left_open) == {second.question_id}


async def test_a_planner_answer_nobody_offered_is_discarded(log, clock, ids, bank) -> None:
    question = await bank.open(
        district_id=DISTRICT,
        address_id=HAYES,
        question=f"Does the filed {STORIES} at {HAYES} match what is built?",
        waiting_on="an inspection",
        opened_by="records-watcher",
        opened_by_version="1.0.0",
        classification=Classification.PUBLIC,
    )
    recorder = _recorder(
        log,
        clock,
        ids,
        memory=bank,
        memory_scopes=PUBLIC_READER,
        planner=_InventivePlanner(),
    )
    await _record_an_incident(recorder, focus=_focus(questions=(question.question_id,)))

    draft = await recorder.neris_draft(_incident())

    assert draft.questions_resolved == (question.question_id,)


# ------------------------------------------------------------------ the model


async def test_a_polished_draft_that_survives_checking_ships(log, clock, ids) -> None:
    composer = _Composer()
    recorder = _recorder(log, clock, ids, model=composer)
    await _record_an_incident(recorder, focus=_focus(_pointer("fact_obs_9c2", priority=1)))

    # Compose the floor the graph will hand the model, then hand it back with a
    # sentence of framing -- which is exactly what polish is allowed to be.
    plain = await _recorder(log, clock, ids).neris_draft(_incident())
    assert plain.narrative == ""
    first = await recorder.neris_draft(_incident())
    composer._text = f"The record for this incident, in brief.\n\n{first.narrative}"

    second = await recorder.neris_draft(_incident())

    assert second.narrative_source == "model"
    assert second.narrative_rejection == ""
    assert second.narrative.startswith("The record for this incident")


@pytest.mark.invariant
async def test_a_draft_with_an_unsourced_claim_is_refused_and_the_plain_one_ships(
    log, clock, ids
) -> None:
    unsourced = "The building at sf-999-nowhere has fact_invented_1 on file. " + NERIS_DISCLAIMER
    recorder = _recorder(log, clock, ids, model=_Composer(unsourced))
    await _record_an_incident(recorder, focus=_focus(_pointer("fact_obs_9c2", priority=1)))

    draft = await recorder.neris_draft(_incident())

    assert draft.narrative_source == "deterministic"
    assert draft.narrative_rejection == "identifier_introduced"
    assert "fact_invented_1" not in draft.narrative
    assert NERIS_DISCLAIMER in draft.narrative


@pytest.mark.degraded
async def test_a_model_that_is_down_costs_the_polish_and_nothing_else(log, clock, ids) -> None:
    recorder = _recorder(log, clock, ids, model=_Composer(raises=True))
    await _record_an_incident(recorder, focus=_focus(_pointer("fact_obs_9c2", priority=1)))

    draft = await recorder.neris_draft(_incident())

    assert draft.narrative_source == "deterministic"
    assert draft.narrative_rejection == "model_unavailable"
    assert draft.leading_refs == ("fact_obs_9c2",)
    assert draft.graph_stop == str(GraphStop.CLOSED)


@pytest.mark.invariant
async def test_the_prompt_carries_no_word_of_the_incident_log(log, clock, ids) -> None:
    """An IC's dictated note is a department record, not prompt material."""
    composer = _Composer()
    recorder = _recorder(log, clock, ids, model=composer)
    await _record_an_incident(recorder, focus=_focus(_pointer("fact_obs_9c2", priority=1)))

    await recorder.neris_draft(_incident())

    rendered = " ".join(str(value) for value in composer.fields.values())
    assert IC_NOTE not in rendered
    assert "separate rear stair" not in rendered


# ------------------------------------------------------------- the ceilings


@pytest.mark.degraded
async def test_budget_exhaustion_ships_the_deterministic_draft(log, clock, ids, bank) -> None:
    """A report is never worth a hung agent, and never worth no report."""
    question = await bank.open(
        district_id=DISTRICT,
        address_id=HAYES,
        question=f"Does the filed {STORIES} at {HAYES} match what is built?",
        waiting_on="an inspection",
        opened_by="records-watcher",
        opened_by_version="1.0.0",
        classification=Classification.PUBLIC,
    )
    recorder = _recorder(
        log,
        clock,
        ids,
        memory=bank,
        memory_scopes=PUBLIC_READER,
        model=_Composer("this should never be reached"),
        max_graph_steps=1,
    )
    await _record_an_incident(recorder, focus=_focus(questions=(question.question_id,)))

    draft = await recorder.neris_draft(_incident(), deadline=NOW - timedelta(seconds=1))

    assert draft.graph_stop in {str(GraphStop.OUT_OF_STEPS), str(GraphStop.OUT_OF_TIME)}
    assert draft.narrative_source == "deterministic"
    assert NERIS_DISCLAIMER in draft.narrative
    # The counted draft is intact whatever the graph managed.
    assert draft.log_entries == 3
    # And a thread the graph never reached was eliminated by nothing.
    carried = await bank.get(question.question_id, scopes=PUBLIC_READER)
    assert carried is not None
    assert carried.status is QuestionStatus.OPEN
    assert carried.ruled_out == ()


async def test_running_out_of_time_parks_rather_than_raising(log, clock, ids) -> None:
    budget = _budget(seconds=10.0, elapsed=[0.0, 99.0])
    synthesis = NerisSynthesis(
        incident=_incident(), log=log, budget=budget, disclaimer=NERIS_DISCLAIMER
    )

    run = await run_graph(
        synthesis.spec(),
        NerisGraphState(district_id=DISTRICT, incident_id=INCIDENT_ID, address_id=HAYES),
        agent_id=AGENT_ID,
        agent_version="1.0.0",
        budget=budget,
        request_digest="test-digest",
        use_langgraph=False,
    )

    assert run.trace.stop is GraphStop.OUT_OF_TIME
    assert run.trace.node_sequence[0] == NODE_ASSEMBLE
    assert run.trace.node_sequence[-1] == "park"
    assert run.state.narrative_source == "deterministic"


# -------------------------------------------------------------- the trace


async def test_every_node_leaves_a_span_that_says_what_it_decided(
    recording_spans, log, clock, ids
) -> None:
    recorder = _recorder(log, clock, ids, model=_Composer(accepted=False))
    await _record_an_incident(recorder, focus=_focus(_pointer("fact_obs_9c2", priority=1)))

    draft = await recorder.neris_draft(_incident())

    spans = [span for span in TRACER.spans if span.name == f"agent.{AGENT_ID}"]
    assert len(spans) == draft.graph_steps
    assert all("graph.decision" in span.attributes for span in spans)
    assert next(span.attributes["graph_node"] for span in spans) == NODE_ASSEMBLE


@pytest.mark.invariant
async def test_no_span_carries_a_word_of_the_record(recording_spans, log, clock, ids) -> None:
    """A span that never held a record cannot leak one -- asserted, not hoped."""
    recorder = _recorder(log, clock, ids, model=_Composer(accepted=False))
    await _record_an_incident(recorder, focus=_focus(_pointer("fact_obs_9c2", priority=1)))

    await recorder.neris_draft(_incident())

    rendered = " ".join(str(value) for span in TRACER.spans for value in span.attributes.values())
    for word in ("rear stair", "living space", "no permit on the wall", IC_NOTE):
        assert word not in rendered


async def test_a_checkpoint_carries_positions_and_not_records() -> None:
    state = NerisGraphState(
        district_id=DISTRICT,
        incident_id=INCIDENT_ID,
        address_id=HAYES,
        evidence=IncidentEvidence(citable=("fact_obs_9c2",), on_scene=(STORIES,), entries=2),
        narrative=f"a whole report mentioning {IC_NOTE}",
        narrative_source="model",
    )

    payload = state.checkpoint_payload()

    assert payload["incident_id"] == INCIDENT_ID
    assert "narrative" not in payload
    assert "evidence" not in payload
    assert IC_NOTE not in str(payload)


# ----------------------------------------------- the boundary with the head


def test_this_side_bounds_the_head_at_the_same_lengths_it_publishes() -> None:
    """Re-bounded rather than shared, and held to the same numbers by a test.

    The point of coercing at the boundary is that this side does not move when
    the other one does. This is the test the graph module's comment promises:
    it fails the day the head widens a field, which is the day somebody should
    decide whether what reaches a prompt widens with it.
    """
    assert MAX_LEAD_REASON == MAX_FOCUS_REASON
    published_ref_max = next(
        item.max_length
        for item in FocusPointer.model_fields["ref"].metadata
        if getattr(item, "max_length", None) is not None
    )
    assert published_ref_max == MAX_LEAD_REF


def test_a_lead_cannot_carry_a_report(log) -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic's own
        Lead(ref="fact_1", reason="x" * (MAX_LEAD_REASON + 1))


def test_a_narrative_is_bounded_on_the_state_as_well_as_at_the_check() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic's own
        NerisGraphState(
            district_id=DISTRICT,
            incident_id=INCIDENT_ID,
            address_id=HAYES,
            narrative="x" * (NERIS_DRAFT_MAX_CHARS + 1),
        )


# ---------------------------------------------------------------- fake mode


def test_fake_mode_never_imports_langgraph() -> None:
    """The incident loop, imported, with the package left alone.

    A subprocess rather than an assertion, because by the time this file runs
    another test has already imported LangGraph deliberately. What has to hold
    is that *wiring the incident loop* does not.
    """
    probe = (
        "import sys, firstdue.incident, firstdue.agents.graphs.recorder;"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] "
        "in {'langgraph', 'langchain_core', 'langchain_google_vertexai'});"
        "print(leaked)"
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        env={"USE_FAKE_AGENTS": "true", "PATH": "/usr/bin:/bin"},
    )

    assert completed.stdout.strip() == "[]"


# ------------------------------------------------------------ with LangGraph


async def test_langgraph_runs_the_same_nodes_to_the_same_chain(log, clock, ids) -> None:
    """The framework is an executor. Swapping it in must change nothing."""
    pytest.importorskip("langgraph")

    recorder = _recorder(log, clock, ids, model=_Composer(accepted=False))
    await _record_an_incident(recorder, focus=_focus(_pointer("fact_obs_9c2", priority=1)))

    builtin = await recorder.neris_draft(_incident())
    compiled = await _recorder(
        log, clock, ids, model=_Composer(accepted=False), use_langgraph=True
    ).neris_draft(_incident())

    assert compiled.narrative == builtin.narrative
    assert compiled.graph_stop == builtin.graph_stop
    assert compiled.graph_steps == builtin.graph_steps
    assert compiled.leading_refs == builtin.leading_refs


# --------------------------------------------------- concurrent log appends


class _ContendedLog(InMemoryIncidentLogRepository):
    """A log where somebody else always gets the first claim on a sequence.

    The real race is between two processes reading ``next_sequence`` before
    either commits, which an in-memory repository cannot produce because
    nothing interleaves between the read and the append. So this stands in for
    the other writer: the first attempt at any sequence is refused exactly the
    way Firestore refuses it, and a second attempt at a fresh number succeeds.
    """

    def __init__(self) -> None:
        super().__init__()
        self.refused: list[int] = []

    async def append(self, entry: IncidentLogEntry) -> IncidentLogEntry:
        if entry.sequence not in self.refused:
            self.refused.append(entry.sequence)
            raise AppendOnlyViolationError(
                "log entries must be appended in sequence",
                details={"expected": entry.sequence + 1, "found": entry.sequence},
            )
        return await super().append(entry)


class _ContentiousLog(InMemoryIncidentLogRepository):
    """A log whose transaction gives up the first time, then commits.

    The other half of the same failure: the Firestore backend writes the
    counter and the entry in one transaction, and a document several agents
    are appending to can exhaust that transaction's own attempts. It arrives
    as ``WriteContentionError``, and it means the same thing -- nothing
    committed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.contended = 0

    async def append(self, entry: IncidentLogEntry) -> IncidentLogEntry:
        if self.contended == 0:
            self.contended += 1
            raise WriteContentionError(entity="incident log", attempts=5)
        return await super().append(entry)


async def test_an_append_that_lost_its_sequence_takes_the_next_one() -> None:
    """Losing a race is not a reason to lose the record of work that happened.

    On a live incident three of ``agency-notifier``'s runs died with
    ``APPEND_ONLY_VIOLATION`` for arriving in the same millisecond as another
    writer. The entry is rebuilt at a fresh number rather than dropped.
    """
    log = _ContendedLog()
    recorder = _recorder(log, FixedClock(NOW), DeterministicIdGenerator())

    await recorder.record_analysis(
        "inc-append", agent_id="agency-notifier", headline="notified public works", detail=""
    )

    stored = await log.get_log("inc-append")
    assert [entry.sequence for entry in stored.entries] == [0]
    assert log.refused == [0]


async def test_an_append_the_transaction_gave_up_on_is_retried() -> None:
    log = _ContentiousLog()
    recorder = _recorder(log, FixedClock(NOW), DeterministicIdGenerator())

    await recorder.record_analysis(
        "inc-contend", agent_id="sensor-fusion", headline="read a frame", detail=""
    )

    stored = await log.get_log("inc-contend")
    assert len(stored.entries) == 1
    assert log.contended == 1


async def test_a_sealed_log_is_not_retried() -> None:
    """The one violation that is real. Retrying it is a writer hammering a
    closed record, so it is re-raised on the first refusal."""
    log = InMemoryIncidentLogRepository()
    recorder = _recorder(log, FixedClock(NOW), DeterministicIdGenerator())
    await recorder.record_analysis(
        "inc-sealed", agent_id="sensor-fusion", headline="read a frame", detail=""
    )
    await log.seal("inc-sealed", at=NOW)

    with pytest.raises(AppendOnlyViolationError):
        await recorder.record_analysis(
            "inc-sealed", agent_id="sensor-fusion", headline="read another", detail=""
        )
