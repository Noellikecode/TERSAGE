"""Readiness: six criteria, and not ready as a first-class answer.

Each test drives one criterion to failure and asserts that the verdict says so,
names it, and cites what it looked at. The last group asserts the property the
whole module exists for: a negative assessment is a complete, renderable
document rather than an absence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.domain.conflicts import Conflict
from firstdue.domain.enums import AssertionStatus, FaceLabel, SourceType
from firstdue.domain.geometry import Face, GeometrySpec, Level, collapse_zone_radius
from firstdue.domain.keys import IntakeKeys, Keys
from firstdue.domain.profiles import ProfileSnapshot
from firstdue.domain.values import BooleanValue
from firstdue.incident.fusion import THERMAL_CAVEAT, FaceCoverage
from firstdue.incident.readiness import HAZARD_KEYS, MAX_SNAPSHOT_AGE, assess

NOW = datetime(2026, 8, 20, 8, 0, 0, tzinfo=UTC)
GROUND = (FaceLabel.ALPHA, FaceLabel.BRAVO, FaceLabel.CHARLIE, FaceLabel.DELTA)
ADDRESS = "sf-0450-hayes"


def geometry() -> GeometrySpec:
    return GeometrySpec(
        address_id=ADDRESS,
        generated_at=NOW,
        footprint=((0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)),
        levels=(
            Level(height_m=3.5, provenance=SourceType.PERMIT, status=AssertionStatus.CONFIRMED),
        ),
        faces=tuple(Face(label=label) for label in GROUND),
        collapse_zone_radius_m=collapse_zone_radius(3.5),
    )


def scanned() -> tuple[FaceCoverage, ...]:
    return tuple(
        FaceCoverage(
            face=label,
            scanned=True,
            observed_at=NOW,
            peak_c=42.0,
            coverage=1.0,
            render=f"42 C peak surface temperature. {THERMAL_CAVEAT}",
        )
        for label in GROUND
    )


def snapshot(
    make_fact,
    *,
    with_geometry: bool = True,
    hazards: bool = True,
    conflicts: tuple[Conflict, ...] = (),
    read_at: datetime = NOW,
) -> ProfileSnapshot:
    facts = (
        {
            key: make_fact(address_id=ADDRESS, key=key, value=BooleanValue(boolean=False))
            for key in HAZARD_KEYS
        }
        if hazards
        else {}
    )
    return ProfileSnapshot(
        address_id=ADDRESS,
        district_id="sffd-district-03",
        profile_version=4,
        snapshot_id="snap-1",
        read_at=read_at,
        facts=facts,
        conflicts=conflicts,
        geometry=geometry() if with_geometry else None,
    )


def verdict(snap: ProfileSnapshot, **kwargs):
    return assess(
        incident_id="inc-1",
        snapshot=snap,
        coverage=kwargs.pop("coverage", scanned()),
        now=kwargs.pop("now", NOW),
        reported_keys=kwargs.pop("reported_keys", (IntakeKeys.REPORTED_OCCUPANCY,)),
        narratives_read=kwargs.pop("narratives_read", 1),
        assessed_by="incident-interceptor",
    )


def criterion(assessment, criterion_id):
    return next(c for c in assessment.criteria if c.criterion_id == criterion_id)


# ------------------------------------------------------------------ the pass


@pytest.mark.invariant
def test_a_complete_record_is_ready_and_every_criterion_says_what_it_checked(make_fact) -> None:
    assessment = verdict(snapshot(make_fact))
    assert assessment.ready
    assert assessment.failed_ids == ()
    assert assessment.summary.startswith("READY")
    assert len(assessment.criteria) == 6
    for entry in assessment.criteria:
        assert entry.passed
        assert entry.reason
        assert entry.title


# ------------------------------------------------------------ each criterion


@pytest.mark.invariant
def test_a_cold_address_fails_the_geometry_criterion(make_fact) -> None:
    assessment = verdict(snapshot(make_fact, with_geometry=False))
    assert not assessment.ready
    entry = criterion(assessment, "geometry.present")
    assert not entry.passed
    assert "never measured" in entry.reason
    assert ADDRESS in entry.refs


@pytest.mark.invariant
def test_an_unscanned_face_fails_the_thermal_criterion_and_names_it(make_fact) -> None:
    """UNSCANNED is unknown, never safe -- and the criterion says exactly that."""
    coverage = (
        FaceCoverage(face=FaceLabel.ALPHA, scanned=False, render="UNSCANNED - no coverage."),
        *scanned()[1:],
    )
    entry = criterion(verdict(snapshot(make_fact), coverage=coverage), "thermal.coverage")
    assert not entry.passed
    assert "UNSCANNED is unknown, never safe" in entry.reason
    assert entry.refs == ("ALPHA",)


def test_no_coverage_report_at_all_fails_rather_than_abstaining(make_fact) -> None:
    entry = criterion(verdict(snapshot(make_fact), coverage=()), "thermal.coverage")
    assert not entry.passed


@pytest.mark.invariant
def test_an_unresolved_hazard_attribute_is_outstanding_rather_than_absent(make_fact) -> None:
    snap = snapshot(make_fact, hazards=False)
    entry = criterion(verdict(snap), "hazard.resolved")
    assert not entry.passed
    assert "outstanding rather than absent" in entry.reason
    assert set(entry.refs) == set(HAZARD_KEYS)


def test_a_hazard_attribute_checked_and_found_absent_counts_as_resolved(make_fact) -> None:
    """A checked ``no`` resolves the attribute. Only ``nobody checked`` does not."""
    assert criterion(verdict(snapshot(make_fact)), "hazard.resolved").passed


@pytest.mark.invariant
def test_an_open_conflict_on_a_load_bearing_key_fails_the_conflict_criterion(make_fact) -> None:
    conflict = Conflict(
        conflict_id="conflict-1",
        address_id=ADDRESS,
        canonical_key=Keys.STORIES,
        rule_id="rule.storey-mismatch",
        severity=4,
        fact_ids=("a", "b"),
        summary="the permit says two and the lidar measured three",
        detected_at=NOW,
    )
    entry = criterion(verdict(snapshot(make_fact, conflicts=(conflict,))), "conflicts.load-bearing")
    assert not entry.passed
    assert "conflict-1" in entry.refs
    assert Keys.STORIES in entry.refs


def test_a_conflict_on_an_attribute_that_does_not_move_a_crew_does_not_fail_it(
    make_fact,
) -> None:
    """A disagreement about the year built is real and changes no entry."""
    conflict = Conflict(
        conflict_id="conflict-2",
        address_id=ADDRESS,
        canonical_key=Keys.YEAR_BUILT,
        rule_id="rule.year-mismatch",
        severity=1,
        fact_ids=("a", "b"),
        summary="the assessor and the permit disagree on the year",
        detected_at=NOW,
    )
    assert criterion(
        verdict(snapshot(make_fact, conflicts=(conflict,))), "conflicts.load-bearing"
    ).passed


@pytest.mark.invariant
def test_a_snapshot_older_than_the_window_fails_the_freshness_criterion(make_fact) -> None:
    later = NOW + MAX_SNAPSHOT_AGE + timedelta(seconds=1)
    entry = criterion(verdict(snapshot(make_fact), now=later), "snapshot.fresh")
    assert not entry.passed
    assert "snap-1" in entry.refs
    assert "not that the building changed" in entry.reason


def test_a_snapshot_inside_the_window_passes(make_fact) -> None:
    entry = criterion(
        verdict(snapshot(make_fact), now=NOW + MAX_SNAPSHOT_AGE - timedelta(seconds=1)),
        "snapshot.fresh",
    )
    assert entry.passed


@pytest.mark.invariant
def test_no_narrative_read_fails_the_intake_criterion(make_fact) -> None:
    entry = criterion(
        verdict(snapshot(make_fact), reported_keys=(), narratives_read=0), "intake.access-bound"
    )
    assert not entry.passed
    assert "no 911 or CAD narrative" in entry.reason


def test_a_narrative_that_bound_nothing_about_access_fails_it_too(make_fact) -> None:
    entry = criterion(
        verdict(
            snapshot(make_fact),
            reported_keys=(IntakeKeys.REPORTED_ALARM_LEVEL,),
            narratives_read=2,
        ),
        "intake.access-bound",
    )
    assert not entry.passed
    assert IntakeKeys.REPORTED_ALARM_LEVEL in entry.refs


def test_a_bound_access_note_passes_and_is_marked_reported_never_observed(make_fact) -> None:
    entry = criterion(
        verdict(snapshot(make_fact), reported_keys=(IntakeKeys.ACCESS_NOTE,)),
        "intake.access-bound",
    )
    assert entry.passed
    assert "Reported, never observed" in entry.reason


# ------------------------------------------------- not ready is a real answer


@pytest.mark.invariant
def test_a_not_ready_verdict_is_a_complete_renderable_document(make_fact) -> None:
    """No silent failure anywhere: every criterion is present either way.

    A partial assessment would be indistinguishable from one that had not run,
    which is the failure this whole module is shaped to avoid.
    """
    assessment = verdict(
        snapshot(make_fact, with_geometry=False, hazards=False),
        coverage=(),
        reported_keys=(),
        narratives_read=0,
    )
    assert not assessment.ready
    assert len(assessment.criteria) == 6
    assert assessment.summary.startswith("NOT READY")
    for failed in assessment.failed_ids:
        assert failed in assessment.summary
    assert set(assessment.failed_ids) == {
        "geometry.present",
        "thermal.coverage",
        "hazard.resolved",
        "intake.access-bound",
    }


def test_the_criteria_are_evaluated_in_a_fixed_order(make_fact) -> None:
    assert [c.criterion_id for c in verdict(snapshot(make_fact)).criteria] == [
        "geometry.present",
        "thermal.coverage",
        "hazard.resolved",
        "conflicts.load-bearing",
        "snapshot.fresh",
        "intake.access-bound",
    ]


def test_the_same_record_produces_a_byte_identical_assessment(make_fact) -> None:
    snap = snapshot(make_fact, hazards=False)
    first = verdict(snap).model_dump(mode="json")
    assert all(verdict(snap).model_dump(mode="json") == first for _ in range(5))


def test_an_assessment_survives_a_round_trip_through_the_log(make_fact) -> None:
    """It is stored as a document and read back; the derived fields recompute."""
    from firstdue.incident.readiness import ReadinessAssessment

    original = verdict(snapshot(make_fact, hazards=False))
    restored = ReadinessAssessment.model_validate(original.model_dump(mode="json"))
    assert restored == original
    assert restored.ready is False
    assert restored.summary == original.summary
