"""Confidence decay, source authority weighting, merge precedence, and override.

These four are one story: how the system decides what it currently believes, and
how sure it is. Each is deterministic and each is checked here against the case
that matters operationally rather than against a convenient one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.domain.decay import (
    AUTHORITY_WEIGHT,
    EVENT_PENALTY_FLOOR,
    HALF_LIFE_DAYS,
    decayed_confidence,
    staleness,
)
from firstdue.domain.enums import Classification, SourceTier, SourceType
from firstdue.domain.facts import StructuralFact
from firstdue.domain.keys import Keys
from firstdue.domain.merge import order_facts, resolve_facts
from firstdue.domain.values import BooleanValue, IntegerValue, UnscannedValue

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
ADDRESS = "sf-0450-hayes"


def _fact(
    fact_id: str,
    *,
    source_type: SourceType,
    days_ago: float = 0.0,
    confidence: float = 1.0,
    value: object | None = None,
    key: str = Keys.STORIES,
    survey_id: str | None = None,
) -> StructuralFact:
    return StructuralFact(
        fact_id=fact_id,
        address_id=ADDRESS,
        canonical_key=key,
        value=value if value is not None else IntegerValue(integer=2),  # type: ignore[arg-type]
        source_type=source_type,
        source_ref="ref",
        source_snapshot_id="snapshot-1",
        observed_at=NOW - timedelta(days=days_ago),
        ingested_at=NOW,
        confidence=confidence,
        classification=Classification.PUBLIC,
        human_verified=survey_id is not None,
        survey_id=survey_id,
    )


# ---------------------------------------------------------------------- decay


def test_a_fresh_fact_keeps_its_authority_weight() -> None:
    fact = _fact("f1", source_type=SourceType.PERMIT)
    expected = AUTHORITY_WEIGHT[SourceTier.AUTHORITATIVE_RECORD]
    assert decayed_confidence(fact, now=NOW) == pytest.approx(expected)


def test_confidence_halves_over_one_half_life() -> None:
    half_life = HALF_LIFE_DAYS[SourceTier.AUTHORITATIVE_RECORD]
    fact = _fact("f1", source_type=SourceType.PERMIT, days_ago=half_life)
    fresh = decayed_confidence(_fact("f2", source_type=SourceType.PERMIT), now=NOW)
    assert decayed_confidence(fact, now=NOW) == pytest.approx(fresh / 2)


def test_a_live_observation_decays_far_faster_than_a_filed_record() -> None:
    """A thermal reading describes a moment; a permit describes a filing."""
    assert (
        HALF_LIFE_DAYS[SourceTier.LIVE_OBSERVATION]
        < HALF_LIFE_DAYS[SourceTier.HUMAN_VERIFIED]
        < HALF_LIFE_DAYS[SourceTier.AUTHORITATIVE_RECORD]
    )
    thermal = _fact("f1", source_type=SourceType.THERMAL_SENSOR, days_ago=1)
    permit = _fact("f2", source_type=SourceType.PERMIT, days_ago=1)
    assert decayed_confidence(thermal, now=NOW) < decayed_confidence(permit, now=NOW)


def test_authority_weighting_orders_the_tiers() -> None:
    """A remote measurement is never as authoritative as a human observation."""
    assert (
        AUTHORITY_WEIGHT[SourceTier.HUMAN_VERIFIED]
        > AUTHORITY_WEIGHT[SourceTier.AUTHORITATIVE_RECORD]
        > AUTHORITY_WEIGHT[SourceTier.REMOTE_MEASUREMENT]
        > AUTHORITY_WEIGHT[SourceTier.DERIVED_INFERENCE]
    )
    same_age = {
        tier: decayed_confidence(_fact("f", source_type=source, days_ago=1), now=NOW)
        for tier, source in (
            (SourceTier.HUMAN_VERIFIED, SourceType.HUMAN_SURVEY),
            (SourceTier.AUTHORITATIVE_RECORD, SourceType.PERMIT),
            (SourceTier.REMOTE_MEASUREMENT, SourceType.LIDAR_DSM),
            (SourceTier.DERIVED_INFERENCE, SourceType.DERIVED),
        )
    }
    assert same_age[SourceTier.AUTHORITATIVE_RECORD] > same_age[SourceTier.REMOTE_MEASUREMENT]
    assert same_age[SourceTier.REMOTE_MEASUREMENT] > same_age[SourceTier.DERIVED_INFERENCE]


def test_intervening_events_cost_confidence_but_never_all_of_it() -> None:
    """Two permits pulled since the survey means the survey knows less than it did."""
    fact = _fact("f1", source_type=SourceType.HUMAN_SURVEY, days_ago=30, survey_id="survey-1")
    quiet = decayed_confidence(fact, now=NOW, events_since_observation=0)
    busy = decayed_confidence(fact, now=NOW, events_since_observation=2)
    floored = decayed_confidence(fact, now=NOW, events_since_observation=50)

    assert busy < quiet
    assert floored == pytest.approx(quiet * EVENT_PENALTY_FLOOR)
    assert floored > 0.0


def test_decay_is_reproducible_and_bounded() -> None:
    fact = _fact("f1", source_type=SourceType.PERMIT, days_ago=900, confidence=0.92)
    first = decayed_confidence(fact, now=NOW, events_since_observation=3)
    second = decayed_confidence(fact, now=NOW, events_since_observation=3)
    assert first == second
    assert 0.0 <= first <= 1.0
    assert staleness(fact, now=NOW, events_since_observation=3) == pytest.approx(1.0 - first)


def test_negative_event_counts_are_refused() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        decayed_confidence(
            _fact("f1", source_type=SourceType.PERMIT), now=NOW, events_since_observation=-1
        )


# ----------------------------------------------------------- merge precedence


@pytest.mark.invariant
def test_memory_never_outranks_live_observation() -> None:
    """The rule the fire service cares about, stated once and checked here."""
    permit = _fact("f-permit", source_type=SourceType.PERMIT, days_ago=1, confidence=1.0)
    thermal = _fact(
        "f-thermal",
        source_type=SourceType.THERMAL_SENSOR,
        days_ago=0,
        confidence=0.4,
        value=IntegerValue(integer=3),
    )
    winner, trace = resolve_facts([permit, thermal])
    assert winner is not None and trace is not None
    assert winner.fact_id == "f-thermal"
    assert trace.rule == "live-observation-outranks-memory"
    # The loser is still there to be shown; resolution is display, not deletion.
    assert "f-permit" in trace.losing_fact_ids


@pytest.mark.invariant
def test_lapsed_sensor_coverage_shows_as_unscanned_not_as_the_old_reading() -> None:
    hot = _fact(
        "f-hot",
        source_type=SourceType.THERMAL_SENSOR,
        days_ago=1,
        key=Keys.THERMAL_FACE_C,
        value=IntegerValue(integer=340),
    )
    lapsed = _fact(
        "f-lapsed",
        source_type=SourceType.THERMAL_SENSOR,
        days_ago=0,
        key=Keys.THERMAL_FACE_C,
        value=UnscannedValue(surface="CHARLIE"),
    )
    winner, _ = resolve_facts([hot, lapsed])
    assert winner is not None
    assert winner.fact_id == "f-lapsed"


@pytest.mark.invariant
def test_a_human_survey_overrides_a_filed_record() -> None:
    """A crew that stood in the building outranks the file, and the file stays."""
    filed = _fact(
        "f-inspection",
        source_type=SourceType.FIRE_INSPECTION,
        days_ago=1,
        key=Keys.LIGHTWEIGHT_TRUSS,
        value=BooleanValue(boolean=False),
        confidence=1.0,
    )
    survey = _fact(
        "f-survey",
        source_type=SourceType.HUMAN_SURVEY,
        days_ago=400,
        key=Keys.LIGHTWEIGHT_TRUSS,
        value=BooleanValue(boolean=True),
        confidence=0.6,
        survey_id="survey-1",
    )
    winner, trace = resolve_facts([filed, survey])
    assert winner is not None and trace is not None
    # Older and less confident, and it still wins: tier is compared first.
    assert winner.fact_id == "f-survey"
    assert set(trace.considered_fact_ids) == {"f-survey", "f-inspection"}


def test_a_known_value_beats_a_source_outage() -> None:
    from firstdue.domain.values import UnavailableValue

    filed = _fact("f-permit", source_type=SourceType.PERMIT, days_ago=500)
    outage = _fact(
        "f-outage",
        source_type=SourceType.ASSESSOR,
        days_ago=0,
        value=UnavailableValue(source_id="sf-assessor", reason="circuit open"),
    )
    winner, trace = resolve_facts([filed, outage])
    assert winner is not None and trace is not None
    assert winner.fact_id == "f-permit"
    assert trace.rule == "known-beats-absent"


def test_merge_order_is_stable_across_input_order() -> None:
    facts = [
        _fact("f-derived", source_type=SourceType.DERIVED, days_ago=0),
        _fact("f-permit", source_type=SourceType.PERMIT, days_ago=10),
        _fact("f-lidar", source_type=SourceType.LIDAR_DSM, days_ago=5),
        _fact("f-survey", source_type=SourceType.HUMAN_SURVEY, days_ago=20, survey_id="s1"),
    ]
    forward = [f.fact_id for f in order_facts(facts)]
    backward = [f.fact_id for f in order_facts(list(reversed(facts)))]
    assert forward == backward
    assert forward == ["f-survey", "f-permit", "f-lidar", "f-derived"]
