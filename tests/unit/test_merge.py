"""Merge order: live observation always outranks memory."""

from __future__ import annotations

from datetime import timedelta

import pytest

from firstdue.domain.enums import SourceType
from firstdue.domain.factsets import FactSet
from firstdue.domain.merge import resolve_facts
from firstdue.domain.values import IntegerValue, QuantityValue, UnscannedValue
from firstdue.errors import AppendOnlyViolationError, ValidationError

pytestmark = pytest.mark.invariant


def test_live_observation_beats_a_newer_filed_record(make_fact, epoch) -> None:
    """Even a permit filed *today* loses to a thermal reading from last night."""
    remembered = make_fact(
        key="thermal.face_temperature_c",
        value=QuantityValue(magnitude=20.0, unit="C"),
        source_type=SourceType.PERMIT,
        observed_at=epoch,
        confidence=1.0,
    )
    observed = make_fact(
        key="thermal.face_temperature_c",
        value=QuantityValue(magnitude=340.0, unit="C"),
        source_type=SourceType.THERMAL_SENSOR,
        observed_at=epoch - timedelta(hours=1),
        confidence=0.5,
    )
    winner, trace = resolve_facts([remembered, observed])
    assert winner is not None
    assert winner.fact_id == observed.fact_id
    assert trace is not None
    assert trace.rule == "live-observation-outranks-memory"


def test_human_survey_beats_a_filed_record(make_fact, epoch) -> None:
    permit = make_fact(source_type=SourceType.PERMIT, observed_at=epoch, confidence=1.0)
    survey = make_fact(
        source_type=SourceType.HUMAN_SURVEY,
        survey_id="sv-1",
        human_verified=True,
        value=IntegerValue(integer=3),
        observed_at=epoch - timedelta(days=200),
        confidence=0.7,
    )
    winner, _ = resolve_facts([permit, survey])
    assert winner is not None and winner.fact_id == survey.fact_id


def test_a_source_outage_does_not_erase_a_filed_record(make_fact, epoch) -> None:
    """An UNAVAILABLE reading must not displace a known filed value."""
    permit = make_fact(source_type=SourceType.PERMIT, observed_at=epoch - timedelta(days=100))
    absent = make_fact(
        source_type=SourceType.LIDAR_DSM,
        value=UnscannedValue(),
        observed_at=epoch,
    )
    winner, trace = resolve_facts([permit, absent])
    assert winner is not None and winner.fact_id == permit.fact_id
    assert trace is not None and trace.rule == "known-beats-absent"


def test_lapsed_live_coverage_wins_over_a_stale_live_reading(make_fact, epoch) -> None:
    """UNSCANNED now beats a hot reading from an hour ago: coverage that
    lapsed must render as lapsed, never as a stale temperature."""
    old_reading = make_fact(
        key="thermal.face_temperature_c",
        value=QuantityValue(magnitude=340.0, unit="C"),
        source_type=SourceType.THERMAL_SENSOR,
        observed_at=epoch - timedelta(hours=1),
    )
    lapsed = make_fact(
        key="thermal.face_temperature_c",
        value=UnscannedValue(surface="BRAVO"),
        source_type=SourceType.THERMAL_SENSOR,
        observed_at=epoch,
    )
    winner, _ = resolve_facts([old_reading, lapsed])
    assert winner is not None and winner.fact_id == lapsed.fact_id


def test_superseded_facts_are_excluded_but_retained(make_fact) -> None:
    first = make_fact()
    second = make_fact(value=IntegerValue(integer=3))
    corrected = first.supersede(by_fact_id=second.fact_id)
    winner, trace = resolve_facts([corrected, second])
    assert winner is not None and winner.fact_id == second.fact_id
    assert trace is not None
    assert corrected.fact_id not in trace.considered_fact_ids


def test_resolution_is_deterministic_regardless_of_input_order(make_fact, epoch) -> None:
    a = make_fact(source_type=SourceType.PERMIT, observed_at=epoch - timedelta(days=1))
    b = make_fact(source_type=SourceType.ASSESSOR, observed_at=epoch - timedelta(days=1))
    forward, _ = resolve_facts([a, b])
    backward, _ = resolve_facts([b, a])
    assert forward is not None and backward is not None
    assert forward.fact_id == backward.fact_id


def test_empty_set_resolves_to_nothing() -> None:
    winner, trace = resolve_facts([])
    assert winner is None and trace is None


def test_conflicting_facts_are_both_retained(make_fact) -> None:
    permit = make_fact(value=IntegerValue(integer=2), source_type=SourceType.PERMIT)
    lidar = make_fact(value=IntegerValue(integer=3), source_type=SourceType.LIDAR_DSM)
    fact_set = FactSet.of(permit).append(lidar)
    assert len(fact_set.facts) == 2
    assert {f.fact_id for f in fact_set.facts} == {permit.fact_id, lidar.fact_id}


def test_fact_set_is_append_only(make_fact) -> None:
    permit = make_fact()
    fact_set = FactSet.of(permit)
    with pytest.raises(AppendOnlyViolationError):
        fact_set.append(permit)


def test_fact_set_rejects_a_foreign_fact(make_fact) -> None:
    fact_set = FactSet.of(make_fact())
    with pytest.raises(ValidationError):
        fact_set.append(make_fact(address_id="sf-1215-fell"))


def test_disagreement_shows_as_disputed(make_fact) -> None:
    from firstdue.domain.enums import AssertionStatus

    fact_set = FactSet.of(make_fact(value=IntegerValue(integer=2))).append(
        make_fact(value=IntegerValue(integer=3), source_type=SourceType.LIDAR_DSM)
    )
    assert fact_set.local_status is AssertionStatus.DISPUTED
