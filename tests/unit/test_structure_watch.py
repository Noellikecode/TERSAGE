"""Structure Watch: one reading, two rankings, and no model anywhere near either.

These tests hold the properties the merge was made for. The first group is the
old Delta Ranker's contract, carried over unchanged because merging two agents
is not a licence to re-score a department's queue. The second group is what is
new: that severity and rank provably describe the *same* reading of the corpus,
and that conflicts are now ranked against each other by importance.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.clock import DeterministicIdGenerator, FixedClock
from firstdue.adapters.memory.bus import InMemoryEventBus
from firstdue.adapters.memory.repositories import (
    InMemoryConflictRepository,
    InMemoryProfileRepository,
    InMemoryQueueRepository,
)
from firstdue.agents.structure_watch import (
    RULE_ATTRIBUTE_DECAY,
    RULE_CONFLICT,
    RULE_LIFE_SAFETY,
    RULE_NEVER_SURVEYED,
    RULE_SEVERITY,
    RULE_UNRESOLVED_AGE,
    WEIGHT_ATTRIBUTE_DECAY,
    WEIGHT_CONFLICT,
    WEIGHT_LIFE_SAFETY,
    WEIGHT_SEVERITY,
    WEIGHT_UNRESOLVED_AGE,
    DistrictReading,
    ProfileReading,
    StructureWatch,
    rank_conflicts,
    read_district,
    read_profile,
    score_conflict,
    score_reading,
)
from firstdue.domain.conflicts import Conflict
from firstdue.domain.enums import Classification, SourceType
from firstdue.domain.events import Topic
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import Keys
from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType
from firstdue.domain.values import IntegerValue
from firstdue.errors import StaleVersionError, ValidationError

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
DISTRICT = "sffd-district-03"
ADDRESS = "sf-0450-hayes"


# ------------------------------------------------------------------ builders


def _fact(
    fact_id: str,
    *,
    stories: int,
    source_type: SourceType,
    days_ago: int,
    address_id: str = ADDRESS,
) -> StructuralFact:
    return StructuralFact(
        fact_id=fact_id,
        address_id=address_id,
        canonical_key=Keys.STORIES,
        value=IntegerValue(integer=stories),
        source_type=source_type,
        source_ref=f"{source_type.value.lower()}/{fact_id}",
        source_snapshot_id=f"snap-{fact_id}",
        observed_at=NOW - timedelta(days=days_ago),
        ingested_at=NOW - timedelta(days=days_ago - 1),
        confidence=0.92,
        classification=Classification.PUBLIC,
    )


def _profile(
    *,
    address_id: str = ADDRESS,
    disputed: bool = False,
    surveyed_days_ago: int | None = None,
) -> BuildingProfile:
    """A profile carrying facts, not pre-baked conclusions.

    The conflict is built the way the product builds it -- a permit and a lidar
    measurement that disagree -- so these tests exercise the engine rather than
    a hand-written ``Conflict`` that no rule ever produced.
    """
    profile = BuildingProfile(address_id=address_id, district_id=DISTRICT)
    facts = [
        _fact(
            "fact-permit",
            stories=2,
            source_type=SourceType.PERMIT,
            days_ago=2000,
            address_id=address_id,
        )
    ]
    if disputed:
        facts.append(
            _fact(
                "fact-lidar",
                stories=3,
                source_type=SourceType.LIDAR_DSM,
                days_ago=10,
                address_id=address_id,
            )
        )
    for index, fact in enumerate(facts):
        profile = profile.with_fact(
            fact,
            event=ProfileEvent(
                event_id=f"evt-{index}",
                sequence=profile.next_sequence,
                occurred_at=fact.ingested_at,
                type=ProfileEventType.FACT_WRITTEN,
                actor="records-watcher",
                summary="filed",
                canonical_keys=(fact.canonical_key,),
                fact_ids=(fact.fact_id,),
            ),
        )
    if surveyed_days_ago is not None:
        profile = profile.model_copy(
            update={"last_human_survey": NOW - timedelta(days=surveyed_days_ago)}
        )
    return profile


def _reading(*, disputed: bool = False, surveyed_days_ago: int | None = None) -> ProfileReading:
    return read_profile(_profile(disputed=disputed, surveyed_days_ago=surveyed_days_ago), now=NOW)


def _conflict(
    *, key: str, severity: int, days_open: int = 0, conflict_id: str = "conflict_a"
) -> Conflict:
    return Conflict(
        conflict_id=conflict_id,
        address_id=ADDRESS,
        canonical_key=key,
        rule_id="authoritative-source-disagreement",
        severity=severity,
        fact_ids=("fact-a", "fact-b"),
        summary=f"Filed records disagree on {key}.",
        detected_at=NOW - timedelta(days=days_open),
    )


def _watch(
    profiles: InMemoryProfileRepository,
    *,
    queue: InMemoryQueueRepository | None = None,
    conflicts: InMemoryConflictRepository | None = None,
    bus: InMemoryEventBus | None = None,
) -> StructureWatch:
    return StructureWatch(
        profiles=profiles,
        conflicts=conflicts or InMemoryConflictRepository(),
        queue=queue or InMemoryQueueRepository(),
        clock=FixedClock(NOW),
        ids=DeterministicIdGenerator("test"),
        bus=bus,
    )


async def _seeded(*profiles: BuildingProfile) -> InMemoryProfileRepository:
    repository = InMemoryProfileRepository()
    for profile in profiles:
        await repository.create(profile)
    return repository


# ------------------------------------------------------- structure ranking


def test_a_conflict_outweighs_everything_else() -> None:
    """The signal the queue exists for must dominate the other three."""
    with_conflict, reasons = score_reading(_reading(disputed=True))
    without, _ = score_reading(_reading())

    # A severity-4 conflict contributes 0.8 of the conflict weight.
    assert with_conflict - without >= WEIGHT_CONFLICT * 0.8 - 1e-6
    assert any(r.rule_id == RULE_CONFLICT for r in reasons)


def test_every_score_carries_at_least_one_reason() -> None:
    """A row with no reason is not allowed to exist.

    ``SurveyQueueEntry`` refuses to be constructed without one, so a scoring
    path that returned none would take the whole structure out of the queue
    rather than putting an unexplained row in front of a chief.
    """
    _score, reasons = score_reading(_reading())
    assert reasons
    assert all(r.detail for r in reasons)
    assert all(0.0 <= r.weight <= 1.0 for r in reasons)


def test_a_never_surveyed_structure_says_so() -> None:
    _score, reasons = score_reading(_reading())
    assert any(r.rule_id == RULE_NEVER_SURVEYED for r in reasons)


def test_a_recent_survey_scores_lower_than_an_old_one() -> None:
    recent, _ = score_reading(_reading(surveyed_days_ago=10))
    old, _ = score_reading(_reading(surveyed_days_ago=900))
    assert old > recent


def test_scoring_is_deterministic() -> None:
    """Same profiles, same order, every run -- or a chief cannot be shown why."""
    assert score_reading(_reading(disputed=True)) == score_reading(_reading(disputed=True))


def test_the_score_stays_within_range() -> None:
    score, _ = score_reading(_reading(disputed=True))
    assert 0.0 <= score <= 1.0


def test_the_cited_conflict_names_its_rule_and_both_facts() -> None:
    """An officer disagreeing with row one must be able to open the documents."""
    _score, reasons = score_reading(_reading(disputed=True))
    cited = next(r for r in reasons if r.rule_id == RULE_CONFLICT)
    assert cited.conflict_id
    assert set(cited.fact_ids) == {"fact-permit", "fact-lidar"}


# ------------------------------------------------------------ one reading


@pytest.mark.invariant
def test_a_reading_whose_decay_is_not_its_profiles_cannot_be_built() -> None:
    """The queue must never quote a decay number the profile does not hold.

    This is the merge's whole point made structural: if the map handed to the
    ranker can differ from the one materialization stored, the queue can say an
    attribute is 40% stale beside a profile page that says 90%, and both are
    "the system's" answer.
    """
    honest = _reading(disputed=True)
    with pytest.raises(ValidationError):
        ProfileReading(
            profile=honest.profile,
            base_version=honest.base_version,
            read_at=honest.read_at,
            new_conflicts=honest.new_conflicts,
            decay={Keys.STORIES: 0.01},
        )


@pytest.mark.invariant
def test_a_district_reading_refuses_readings_from_different_instants() -> None:
    """Two structures on one rank scale must be as of one moment.

    Decay carries 0.25 of the score. Mixing an hour-old reading with a fresh one
    would make that quarter of the ranking a measurement of the clock rather
    than of the building.
    """
    fresh = _reading(disputed=True)
    stale = read_profile(_profile(address_id="sf-0500-fell"), now=NOW - timedelta(days=1))
    with pytest.raises(ValidationError):
        DistrictReading(district_id=DISTRICT, read_at=NOW, readings=(fresh, stale))


@pytest.mark.invariant
async def test_one_pass_reads_the_district_exactly_once() -> None:
    """Severity and rank cannot disagree about a corpus only read once.

    ``conflict-detector`` read the profiles and ``survey-ranker`` read them
    again. Anything written between the two reads made the conflict an officer
    saw and the score that surfaced it answers about different corpora.
    """
    reads: list[str] = []

    class CountingProfiles(InMemoryProfileRepository):
        async def list_by_district(self, district_id: str) -> Sequence[BuildingProfile]:
            reads.append(f"list:{district_id}")
            return await super().list_by_district(district_id)

        async def get(self, address_id: str) -> BuildingProfile | None:
            reads.append(f"get:{address_id}")
            return await super().get(address_id)

    profiles = CountingProfiles()
    await profiles.create(_profile(disputed=True))
    await profiles.create(_profile(address_id="sf-0500-fell"))

    result = await _watch(profiles).watch(DISTRICT)

    assert reads == [f"list:{DISTRICT}"]
    assert result.entries
    assert result.conflicts


@pytest.mark.concurrency
async def test_losing_the_version_check_does_not_trigger_a_second_read() -> None:
    """A contended write is not a reason to go back to the store.

    The other writer ran the same deterministic engine over the same facts, so
    its conflicts and decay are ours. Re-reading to "get the winner's copy"
    would reintroduce the second reading this agent exists to remove -- and the
    row would then be scored on a corpus its cited severity never saw.
    """
    reads: list[str] = []

    class ContendedProfiles(InMemoryProfileRepository):
        async def list_by_district(self, district_id: str) -> Sequence[BuildingProfile]:
            reads.append("list")
            return await super().list_by_district(district_id)

        async def get(self, address_id: str) -> BuildingProfile | None:
            reads.append("get")
            return await super().get(address_id)

        async def save(self, profile: BuildingProfile, *, expected_version: int) -> BuildingProfile:
            raise StaleVersionError(expected=expected_version, actual=99, entity="profile")

    profiles = ContendedProfiles()
    await profiles.create(_profile(disputed=True))

    result = await _watch(profiles).watch(DISTRICT)

    assert reads == ["list"]
    assert result.contended == (ADDRESS,)
    # The ranking still happened, from the reading already in hand.
    assert result.entries and result.entries[0].address_id == ADDRESS


async def test_the_row_and_the_ranked_conflict_name_the_same_finding() -> None:
    """One reading means the queue and the conflict list cannot diverge."""
    profiles = await _seeded(_profile(disputed=True))
    result = await _watch(profiles).watch(DISTRICT)

    cited = next(r for r in result.entries[0].reasons if r.rule_id == RULE_CONFLICT)
    assert cited.conflict_id in {c.conflict_id for c in result.conflicts}


# ------------------------------------------------------- conflict ranking


def test_a_life_safety_conflict_outranks_a_paperwork_one_of_equal_severity() -> None:
    """Severity alone cannot separate them; what a crew does about it can.

    A disagreement about the stairwell count is settled at 03:00 by someone who
    has to find the second one. A disagreement about the year of construction is
    settled by a clerk.
    """
    reading = _reading()
    life_safety, _ = score_conflict(_conflict(key=Keys.STAIRWELL_COUNT, severity=3), reading)
    paperwork, _ = score_conflict(_conflict(key=Keys.YEAR_BUILT, severity=3), reading)
    assert life_safety > paperwork


def test_engine_severity_is_the_heaviest_signal_in_the_conflict_ranking() -> None:
    """Only severity is derived from what the sources actually said.

    The other three describe the disagreement's situation rather than its
    content. Together they can lift an old life-safety disagreement above a
    fresh minor one, which is the intent; individually none of them may move a
    conflict as far as the engine's own finding does, or the list starts
    reporting on the backlog instead of on the buildings.
    """
    reading = _reading()
    high, _ = score_conflict(_conflict(key=Keys.YEAR_BUILT, severity=5), reading)
    low, _ = score_conflict(_conflict(key=Keys.YEAR_BUILT, severity=1), reading)
    assert high - low == pytest.approx(WEIGHT_SEVERITY * 0.8)
    assert max(WEIGHT_LIFE_SAFETY, WEIGHT_UNRESOLVED_AGE, WEIGHT_ATTRIBUTE_DECAY) < WEIGHT_SEVERITY


def test_an_older_unresolved_conflict_outranks_an_identical_fresh_one() -> None:
    """Only a human closes a conflict, so age is a survey that keeps not happening."""
    reading = _reading()
    old, _ = score_conflict(_conflict(key=Keys.YEAR_BUILT, severity=3, days_open=200), reading)
    fresh, _ = score_conflict(_conflict(key=Keys.YEAR_BUILT, severity=3), reading)
    assert old > fresh


def test_a_conflict_on_a_decayed_attribute_outranks_one_on_a_fresh_attribute() -> None:
    """Once the file cannot settle it, only a person in the building can."""
    reading = _reading()
    assert reading.decay[Keys.STORIES] < 1.0
    decayed, reasons = score_conflict(_conflict(key=Keys.STORIES, severity=3), reading)
    fresh, _ = score_conflict(_conflict(key=Keys.YEAR_BUILT, severity=3), reading)
    assert decayed > fresh
    assert any(r.rule_id == RULE_ATTRIBUTE_DECAY for r in reasons)


@pytest.mark.invariant
def test_every_ranked_conflict_cites_the_rule_and_the_facts_it_rests_on() -> None:
    """ "Because the model said so" must not be expressible for a conflict either.

    The queue has carried this guarantee since the ranker existed. Ranking
    conflicts introduced a second ordering a captain reads, and it inherits the
    same rule: every row names the deterministic rule and the fact ids.
    """
    district = read_district(DISTRICT, [_profile(disputed=True)], now=NOW)
    ranked = rank_conflicts(district)
    assert ranked
    for row in ranked:
        assert row.reasons
        severity_reason = next(r for r in row.reasons if r.rule_id == RULE_SEVERITY)
        # The rule id and both documents, so an officer can re-derive the row.
        assert set(severity_reason.fact_ids) == {"fact-permit", "fact-lidar"}
        assert severity_reason.conflict_id == row.conflict_id
        assert row.rule_id == "permit-vs-lidar-story-count"
        assert any(r.rule_id == RULE_ATTRIBUTE_DECAY for r in row.reasons)


def test_conflict_importance_is_deterministic_and_ties_break_on_the_derived_id() -> None:
    """Two workers ranking one district produce the same order, not just scores.

    The twins sit at *different addresses*. One rule and one attribute produce
    exactly one live finding per structure -- a second finding about the same
    pairing is a superseded one, and `current_conflicts` is what drops it -- so
    a tie a captain can actually see is a tie across two buildings.
    """
    reading = _reading()
    twins = {
        cid: _conflict(key=Keys.YEAR_BUILT, severity=3, conflict_id=cid)
        for cid in ("conflict_zzz", "conflict_aaa")
    }
    scores = {cid: score_conflict(c, reading)[0] for cid, c in twins.items()}
    assert scores["conflict_zzz"] == scores["conflict_aaa"]

    profiles = [
        _profile(address_id=f"{ADDRESS}-{cid}").model_copy(
            update={"conflicts": (conflict.model_copy(update={"address_id": f"{ADDRESS}-{cid}"}),)}
        )
        for cid, conflict in twins.items()
    ]
    district = read_district(DISTRICT, profiles, now=NOW)
    ordered = [row.conflict_id for row in rank_conflicts(district)]
    assert ordered == ["conflict_aaa", "conflict_zzz"]
    assert ordered == [row.conflict_id for row in rank_conflicts(district)]


def test_one_disagreement_is_ranked_once_however_many_times_it_was_redetected() -> None:
    """A rule re-firing does not give a captain three rows for one problem.

    Real data: three OPEN findings at one address, same rule, same attribute,
    same sentence -- each citing a different pair of the facts retained for that
    key, because a conflict's id is derived from the facts it cited and an
    amended permit mints a new one. Only a human may resolve a conflict, so the
    superseded findings stay OPEN in the record. The queue shows the live one.
    """
    redetections = tuple(
        _conflict(key=Keys.YEAR_BUILT, severity=3, days_open=age, conflict_id=cid)
        for age, cid in ((2, "conflict_oldest"), (1, "conflict_middle"), (0, "conflict_newest"))
    )
    profile = _profile().model_copy(update={"conflicts": redetections})

    assert len(profile.open_conflicts) == 3, "the record keeps every finding"
    assert [c.conflict_id for c in profile.current_conflicts] == ["conflict_newest"]

    district = read_district(DISTRICT, [profile], now=NOW)
    assert [row.conflict_id for row in rank_conflicts(district)] == ["conflict_newest"]


@pytest.mark.invariant
def test_a_models_narration_cannot_change_a_conflicts_importance() -> None:
    """A model that could re-order the list could bury the one that matters.

    Narration is the only field a model authors. It must move nothing: not the
    severity the engine computed, and not the importance derived from it.
    """
    reading = _reading()
    plain = _conflict(key=Keys.STAIRWELL_COUNT, severity=4, days_open=30)
    narrated = plain.narrate("The crew should expect only one usable stairwell.")

    assert score_conflict(narrated, reading) == score_conflict(plain, reading)
    assert any(r.rule_id == RULE_LIFE_SAFETY for r in score_conflict(plain, reading)[1])


def test_the_worst_severity_sets_the_structure_score_whatever_the_importance() -> None:
    """Merging the two rankings must not let the new one re-weight the old one.

    The queue's conflict signal is *severity*, at 0.40, as it has always been.
    Importance only decides which conflict the row cites, so a structure with a
    severity-5 disagreement keeps its place even when an older, life-safety
    severity-3 conflict is the more important thing to act on.
    """
    profile = _profile().model_copy(
        update={
            "conflicts": (
                _conflict(key=Keys.YEAR_BUILT, severity=5, conflict_id="conflict_severe"),
                _conflict(
                    key=Keys.STAIRWELL_COUNT,
                    severity=3,
                    days_open=900,
                    conflict_id="conflict_important",
                ),
            )
        }
    )
    reading = read_profile(profile, now=NOW)
    _score, reasons = score_reading(reading)
    cited = next(r for r in reasons if r.rule_id == RULE_CONFLICT)

    assert cited.conflict_id == "conflict_severe"
    assert cited.weight == pytest.approx(1.0)
    # And the more important one is still the one at the top of the other list.
    ranked = rank_conflicts(DistrictReading(district_id=DISTRICT, read_at=NOW, readings=(reading,)))
    assert ranked[0].conflict_id == "conflict_important"
    assert any(r.rule_id == RULE_UNRESOLVED_AGE for r in ranked[0].reasons)


# --------------------------------------------------------------- the pass


@pytest.mark.idempotency
async def test_a_second_pass_over_unchanged_profiles_writes_nothing_new() -> None:
    """Ids are derived, so re-running re-derives what exists instead of doubling it."""
    profiles = await _seeded(_profile(disputed=True))
    queue = InMemoryQueueRepository()
    conflicts = InMemoryConflictRepository()
    agent = _watch(profiles, queue=queue, conflicts=conflicts)

    first = await agent.watch(DISTRICT)
    second = await agent.watch(DISTRICT)

    assert first.new_conflict_ids
    assert second.new_conflict_ids == ()
    assert len(await conflicts.list_open()) == len(first.new_conflict_ids)
    assert [e.entry_id for e in first.entries] == [e.entry_id for e in second.entries]
    assert [e.rank for e in first.entries] == [e.rank for e in second.entries]


async def test_the_queue_is_ordered_by_score_and_ties_break_on_address() -> None:
    """The order is the product; two workers must not produce two orders."""
    profiles = await _seeded(
        _profile(address_id="sf-0500-fell"),
        _profile(disputed=True),
        _profile(address_id="sf-0100-oak"),
    )
    result = await _watch(profiles).watch(DISTRICT)

    scores = [e.score for e in result.entries]
    assert scores == sorted(scores, reverse=True)
    assert result.entries[0].address_id == ADDRESS
    tied = [e.address_id for e in result.entries if e.score == result.entries[-1].score]
    assert tied == sorted(tied)


@pytest.mark.idempotency
async def test_a_re_ranking_that_changes_nothing_does_not_notify_anybody_twice() -> None:
    """``queue.ranked`` wakes the referral clerk; an unchanged order is not news.

    The topic was declared and subscribed to and nothing had ever published it.
    Publishing it now only helps if a pass that re-derives the same order is
    recognisable as the same ranking rather than as a second one.
    """
    profiles = await _seeded(_profile(disputed=True))
    bus = InMemoryEventBus()
    agent = _watch(profiles, bus=bus)

    first = await agent.watch(DISTRICT, correlation_id="corr_1")
    second = await agent.watch(DISTRICT, correlation_id="corr_2")

    published = [e for e in bus.published if e.topic is Topic.QUEUE_RANKED]
    assert len(published) == 2
    assert published[0].idempotency_key == published[1].idempotency_key
    assert first.published_event_ids and second.published_event_ids


def test_the_agent_takes_no_model_and_no_way_to_reach_one() -> None:
    """A model that could invent a conflict could invent its absence.

    Detection and ranking are deterministic Python. Gemini narrates a conflict
    the engine found and explains why a row surfaced; it is not a collaborator
    in either decision, and the constructor is where that would first leak in.
    """
    import inspect

    parameters = inspect.signature(StructureWatch.__init__).parameters
    assert not any("model" in name or "gemini" in name for name in parameters)
