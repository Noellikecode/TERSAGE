"""The focus contract: a pointer carries a reference, and never a value.

Two other agents code against this module, so the tests here are about the
promises they are relying on rather than about how the focus gets composed:

* a pointer that carries a value is refused, by the type and by the closed list;
* a focus is bound to exactly one ``profile_version``;
* ``read_focus`` returns the latest one and nothing else;
* the log entry carries ids, keys and counts -- never a transcript.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from firstdue.adapters.memory.repositories import InMemoryIncidentLogRepository
from firstdue.domain.conflicts import Conflict
from firstdue.domain.enums import Classification, LogEntryType, SourceType
from firstdue.domain.facts import StructuralFact
from firstdue.domain.geometry import GeometrySpec
from firstdue.domain.keys import Keys
from firstdue.domain.memory import OpenQuestion, derive_question_id
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.domain.values import IntegerValue
from firstdue.errors import ValidationError
from firstdue.incident.focus import (
    COMPOSER_ID,
    MAX_FOCUS_REASON,
    AgentFocus,
    FocusKind,
    FocusPointer,
    IncidentFocus,
    compose_focus,
    focus_log_entry,
    focus_scope,
    geometry_ref,
    read_focus,
    survey_ref,
)
from firstdue.incident.interceptor import AGENT_ID

NOW = datetime(2026, 8, 21, 3, 14, tzinfo=UTC)
MARCH = datetime(2026, 3, 4, 9, 0, tzinfo=UTC)
ADDRESS = "sf-0450-hayes"
DISTRICT = "sffd-district-03"
INCIDENT = "inc-1"

#: Both refusals count. A bound declared on the field is enforced by pydantic
#: and a rule about shape is enforced by a validator; a caller that gets either
#: one has been refused, and which one fired is an implementation detail.
REFUSED = (ValidationError, PydanticValidationError)

#: What a 911 caller said. Nothing derived from it may reach a focus, a log
#: entry, or a span -- this string is the tripwire the assertions grep for.
TRANSCRIPT = "Caller says there are people on the third floor and the building has three storeys."


# ------------------------------------------------------------------ helpers


def _fact(key: str, *, fact_id: str, source_type: SourceType = SourceType.PERMIT) -> StructuralFact:
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


def _conflict(*fact_ids: str, conflict_id: str = "conflict_c3d4", severity: int = 4) -> Conflict:
    return Conflict(
        conflict_id=conflict_id,
        address_id=ADDRESS,
        canonical_key=Keys.STORIES,
        rule_id="rule_storey_disagreement",
        severity=severity,
        fact_ids=tuple(fact_ids),
        summary="the permit and the lidar measurement disagree",
        detected_at=MARCH,
    )


def _question(*, evidence: tuple[str, ...] = (), question: str = "Which storey count is filed?"):
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
        waiting_on="a human survey of the building",
        evidence_fact_ids=evidence,
        classification=Classification.PUBLIC,
        confidence=0.5,
    )


def _snapshot(*, geometry: bool = False, surveyed: bool = False, version: int = 7):
    facts = {
        Keys.STORIES: _fact(Keys.STORIES, fact_id="fact_a1b2"),
        Keys.HAZARD_TIER_II_PRESENT: _fact(
            Keys.HAZARD_TIER_II_PRESENT, fact_id="fact_h9z8", source_type=SourceType.TIER_II
        ),
    }
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
        facts=facts,
        conflicts=(_conflict("fact_a1b2", "fact_l4m5"),),
        geometry=spec,
        open_referral_ids=("referral_r7t8",),
        last_human_survey=MARCH if surveyed else None,
    )


def _pointer(kind: FocusKind = FocusKind.CONFLICT, ref: str = "conflict_c3d4") -> FocusPointer:
    return FocusPointer(kind=kind, ref=ref, reason=f"{ref} is open on {Keys.STORIES}.", priority=1)


def _focus(*pointers: FocusPointer, version: int = 7, agent_id: str = "incident-recorder"):
    return IncidentFocus(
        incident_id=INCIDENT,
        address_id=ADDRESS,
        composed_by=COMPOSER_ID,
        composed_by_version="1.0.0",
        composed_at=NOW,
        profile_version=version,
        per_agent=(
            AgentFocus(agent_id=agent_id, headline="2 references.", pointers=tuple(pointers)),
        ),
    )


# --------------------------------------------- a pointer never carries a value


@pytest.mark.invariant
@pytest.mark.parametrize(
    "value",
    [
        "the building has three storeys",
        "three storeys",
        "3",
        "wood frame",
        TRANSCRIPT,
        "  ",
        "people on the third floor",
    ],
)
def test_a_pointer_carrying_a_value_is_refused(value: str) -> None:
    """The head points. It does not assert, and the type is what stops it.

    Every string here is something a model handed a snapshot and a transcript
    would plausibly produce, and every one of them would arrive on a commander's
    screen with no source type, no confidence, no span and nothing to check it
    against.
    """
    with pytest.raises(REFUSED):
        FocusPointer(kind=FocusKind.FACT, ref=value, reason="it matters", priority=1)


@pytest.mark.parametrize(
    "ref",
    [
        "fact_a1b2",
        "conflict_c3d4",
        "mq_9f1c4d2e",
        "referral_r7t8",
        Keys.STORIES,
        Keys.HAZARD_TIER_II_PRESENT,
        geometry_ref(ADDRESS),
        survey_ref(ADDRESS),
    ],
)
def test_a_reference_is_accepted(ref: str) -> None:
    """Every shape the rest of the system actually mints has to get through."""
    assert FocusPointer(kind=FocusKind.FACT, ref=ref, reason="x", priority=3).ref == ref


@pytest.mark.invariant
def test_a_reason_may_not_grow_into_a_quotation() -> None:
    """Bounded, and refusing the punctuation a pasted transcript line arrives in."""
    with pytest.raises(REFUSED):
        FocusPointer(
            kind=FocusKind.FACT, ref="fact_a1b2", reason="x" * (MAX_FOCUS_REASON + 1), priority=1
        )
    with pytest.raises(REFUSED):
        FocusPointer(
            kind=FocusKind.FACT,
            ref="fact_a1b2",
            reason=f'the caller said "{TRANSCRIPT}"',
            priority=1,
        )


def test_a_headline_is_one_line() -> None:
    with pytest.raises(ValidationError):
        AgentFocus(agent_id="incident-recorder", headline="two\nlines", pointers=())


# ---------------------------------------------------- bound to one snapshot


@pytest.mark.invariant
def test_the_closed_list_drops_a_reference_nothing_on_file_supports() -> None:
    """The shape check is coarse; this is the one that is not.

    ``three-storeys`` looks enough like a reference to survive the regex. It
    does not survive the scope, because nothing filed against this snapshot is
    called that -- which is the property the module actually rests on.
    """
    scope = focus_scope(_snapshot())
    invented = FocusPointer(
        kind=FocusKind.FACT, ref="three-storeys", reason="the caller implied it", priority=1
    )
    composed = compose_focus(
        incident_id=INCIDENT,
        scope=scope,
        per_agent=(
            AgentFocus(
                agent_id="sensor-fusion",
                headline="2 references.",
                pointers=(_pointer(), invented),
            ),
        ),
        composed_by_version="1.0.0",
        composed_at=NOW,
    )
    assert composed.refs == ("conflict_c3d4",)


@pytest.mark.invariant
def test_a_reference_filed_under_the_wrong_kind_is_dropped() -> None:
    """A real fact id labelled CONFLICT sends an agent to the wrong store."""
    scope = focus_scope(_snapshot())
    mislabelled = FocusPointer(
        kind=FocusKind.CONFLICT, ref="fact_a1b2", reason="filed under the wrong kind", priority=1
    )
    composed = compose_focus(
        incident_id=INCIDENT,
        scope=scope,
        per_agent=(
            AgentFocus(agent_id="sensor-fusion", headline="1 reference.", pointers=(mislabelled,)),
        ),
        composed_by_version="1.0.0",
        composed_at=NOW,
    )
    assert composed.per_agent == ()


@pytest.mark.invariant
def test_a_focus_is_bound_to_one_profile_version() -> None:
    """Every pointer resolves against one version, or none of them do.

    Two agents acting on focus computed against different profile versions is
    the failure ``profile_version`` exists to make visible: the conflict one of
    them was pointed at may already be closed in the version the other read.
    """
    scope = focus_scope(_snapshot(version=7))
    composed = compose_focus(
        incident_id=INCIDENT,
        scope=scope,
        per_agent=(
            AgentFocus(agent_id="sensor-fusion", headline="1 reference.", pointers=(_pointer(),)),
        ),
        composed_by_version="1.0.0",
        composed_at=NOW,
    )
    assert composed.profile_version == 7
    assert composed.unresolved_against(scope) == ()

    moved_on = focus_scope(_snapshot(version=8))
    assert composed.unresolved_against(moved_on) == composed.pointers


def test_the_scope_carries_only_what_the_snapshot_supports() -> None:
    scope = focus_scope(
        _snapshot(geometry=True, surveyed=True),
        questions=(_question(),),
        unknown_keys=(Keys.SUPPRESSION_SPRINKLERED,),
    )
    assert scope.of(FocusKind.CONFLICT) == ("conflict_c3d4",)
    assert scope.of(FocusKind.GEOMETRY) == (geometry_ref(ADDRESS),)
    assert scope.of(FocusKind.SURVEY) == (survey_ref(ADDRESS),)
    assert scope.of(FocusKind.REFERRAL) == ("referral_r7t8",)
    assert scope.of(FocusKind.UNKNOWN_KEY) == (Keys.SUPPRESSION_SPRINKLERED,)
    # A hazard reference is either the filed fact or the attribute it is filed
    # under: "no Tier II filing on record" has a key and no fact id.
    assert set(scope.of(FocusKind.HAZARD)) == {"fact_h9z8", Keys.HAZARD_TIER_II_PRESENT}

    bare = focus_scope(_snapshot())
    assert bare.of(FocusKind.GEOMETRY) == ()
    assert bare.of(FocusKind.SURVEY) == ()


def test_an_agent_pointed_nowhere_is_absent_rather_than_empty() -> None:
    """``None`` is a real answer; a pointer invented to fill the gap is not."""
    composed = compose_focus(
        incident_id=INCIDENT,
        scope=focus_scope(_snapshot()),
        per_agent=(
            AgentFocus(agent_id="sensor-fusion", headline="1 reference.", pointers=(_pointer(),)),
        ),
        composed_by_version="1.0.0",
        composed_at=NOW,
    )
    assert composed.for_agent("sensor-fusion") is not None
    assert composed.for_agent("agency-notifier") is None


def test_a_focus_names_each_agent_once() -> None:
    entry = AgentFocus(agent_id="sensor-fusion", headline="1 reference.", pointers=(_pointer(),))
    with pytest.raises(ValidationError):
        IncidentFocus(
            incident_id=INCIDENT,
            address_id=ADDRESS,
            composed_by=COMPOSER_ID,
            composed_by_version="1.0.0",
            composed_at=NOW,
            profile_version=7,
            per_agent=(entry, entry),
        )


def test_composed_at_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        IncidentFocus(
            incident_id=INCIDENT,
            address_id=ADDRESS,
            composed_by=COMPOSER_ID,
            composed_by_version="1.0.0",
            composed_at=datetime(2026, 8, 21, 3, 14),  # noqa: DTZ001 - the point of the test
            profile_version=7,
        )


def test_open_question_ids_are_bound_to_the_same_scope() -> None:
    question = _question()
    scope = focus_scope(_snapshot(), questions=(question,))
    composed = compose_focus(
        incident_id=INCIDENT,
        scope=scope,
        per_agent=(
            AgentFocus(agent_id="incident-recorder", headline="1 ref.", pointers=(_pointer(),)),
        ),
        open_question_ids=(question.question_id, "mq_notonthisprofile"),
        composed_by_version="1.0.0",
        composed_at=NOW,
    )
    assert composed.open_question_ids == (question.question_id,)


# ------------------------------------------------------------- persistence


def test_the_log_entry_carries_ids_and_keys_and_no_transcript() -> None:
    focus = _focus(_pointer())
    entry = focus_log_entry(focus, sequence=3, now=NOW)

    assert entry.entry_type is LogEntryType.FOCUS_COMPOSED
    assert entry.sequence == 3
    assert entry.agent_versions == {COMPOSER_ID: "1.0.0"}
    assert entry.content["profile_version"] == 7
    assert entry.content["pointer_count"] == 1
    assert entry.content["agent_ids"] == ["incident-recorder"]
    assert TRANSCRIPT not in str(entry.content)
    # Sealable like every other entry, which is what makes it replayable.
    assert entry.sealed().content_hash


def test_the_entry_id_is_derived_so_a_replay_reproduces_it() -> None:
    focus = _focus(_pointer())
    assert (
        focus_log_entry(focus, sequence=3, now=NOW).entry_id
        == focus_log_entry(focus, sequence=3, now=NOW).entry_id
    )
    assert (
        focus_log_entry(focus, sequence=3, now=NOW).entry_id
        != focus_log_entry(focus, sequence=4, now=NOW).entry_id
    )


async def test_read_focus_returns_the_latest_one() -> None:
    """A later composition supersedes an earlier one. Both stay in the log."""
    log = InMemoryIncidentLogRepository()
    assert await read_focus(log, INCIDENT) is None

    first = _focus(_pointer(), version=7)
    second = _focus(
        _pointer(FocusKind.OPEN_QUESTION, "mq_9f1c4d2e"),
        version=9,
        agent_id="agency-notifier",
    )
    for sequence, focus in enumerate((first, second)):
        await log.append(focus_log_entry(focus, sequence=sequence, now=NOW))

    latest = await read_focus(log, INCIDENT)
    assert latest is not None
    assert latest.profile_version == 9
    assert latest.agent_ids == ("agency-notifier",)
    assert latest.refs == ("mq_9f1c4d2e",)
    # Nothing was replaced: the superseded focus is still on the record.
    assert len((await log.get_log(INCIDENT)).entries) == 2


async def test_read_focus_ignores_every_other_entry_type() -> None:
    from firstdue.domain.logentries import IncidentLogEntry

    log = InMemoryIncidentLogRepository()
    await log.append(
        IncidentLogEntry(
            entry_id="entry_intake",
            incident_id=INCIDENT,
            sequence=0,
            entry_type=LogEntryType.INTAKE_READ,
            occurred_at=NOW,
            profile_snapshot_id="snap-7",
            content={"accepted": True},
        )
    )
    assert await read_focus(log, INCIDENT) is None


def test_the_composer_is_the_interceptor() -> None:
    """Spelled in two modules to avoid a cycle; held together here."""
    assert COMPOSER_ID == AGENT_ID
