"""Sensor fusion, the material time window, and the resource split."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.domain.enums import AssertionStatus, FaceLabel, Scope, SourceType
from firstdue.domain.geometry import Face, GeometrySpec
from firstdue.domain.values import QuantityValue, UnavailableValue, UnscannedValue
from firstdue.incident.fusion import (
    THERMAL_CAVEAT,
    VOID_DELTA_C,
    SensorFusion,
    ThermalFrame,
    unscanned_faces,
)
from firstdue.incident.resources import (
    ALL_KINDS,
    COMMITMENTS,
    NOTIFICATIONS,
    commitment_kinds,
    notification_kinds,
)
from firstdue.incident.timer import (
    DISCLAIMER,
    TRUSS_WINDOW_MAX,
    TRUSS_WINDOW_MIN,
    truss_time_window,
)

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
INCIDENT = "inc-1"


def _frame(face: FaceLabel, temps: tuple[float, ...], *, at: datetime = NOW) -> ThermalFrame:
    return ThermalFrame(
        frame_id=f"frame-{face}",
        incident_id=INCIDENT,
        face=face,
        observed_at=at,
        region_temps_c=temps,
    )


# --------------------------------------------------------------- fusion


@pytest.mark.invariant
def test_a_face_with_no_frame_is_unscanned_not_cool() -> None:
    """There is no default temperature anywhere in this module."""
    fusion = SensorFusion()
    fusion.register(_frame(FaceLabel.ALPHA, (20.0,)))

    coverage = fusion.coverage(INCIDENT, now=NOW)
    assert len(coverage) == 4
    assert unscanned_faces(coverage) == (
        FaceLabel.BRAVO,
        FaceLabel.CHARLIE,
        FaceLabel.DELTA,
    )
    for report in coverage:
        if report.scanned:
            continue
        assert "UNSCANNED" in report.render
        assert report.peak_c is None


@pytest.mark.invariant
def test_every_reading_carries_the_caveat() -> None:
    """Thermal measures surface temperature and cannot see through walls."""
    fusion = SensorFusion()
    fusion.register(_frame(FaceLabel.ALPHA, (300.0,)))
    for report in fusion.coverage(INCIDENT, now=NOW):
        assert THERMAL_CAVEAT in report.render


def test_coverage_lapses_rather_than_holding_a_stale_reading() -> None:
    fusion = SensorFusion(coverage_window=timedelta(minutes=5))
    fusion.register(_frame(FaceLabel.ALPHA, (300.0,)))

    later = NOW + timedelta(minutes=6)
    alpha = next(c for c in fusion.coverage(INCIDENT, now=later) if c.face is FaceLabel.ALPHA)
    assert not alpha.scanned
    assert "lapsed" in alpha.render


def test_the_newest_frame_per_face_wins() -> None:
    fusion = SensorFusion()
    fusion.register(_frame(FaceLabel.ALPHA, (20.0,)))
    fusion.register(_frame(FaceLabel.ALPHA, (120.0,), at=NOW + timedelta(minutes=1)))

    alpha = next(
        c
        for c in fusion.coverage(INCIDENT, now=NOW + timedelta(minutes=2))
        if c.face is FaceLabel.ALPHA
    )
    assert alpha.peak_c == 120.0


def test_an_older_frame_does_not_displace_a_newer_one() -> None:
    fusion = SensorFusion()
    fusion.register(_frame(FaceLabel.ALPHA, (120.0,), at=NOW + timedelta(minutes=1)))
    fusion.register(_frame(FaceLabel.ALPHA, (20.0,), at=NOW))
    assert fusion.frame_for(INCIDENT, FaceLabel.ALPHA).peak_c == 120.0  # type: ignore[union-attr]


def test_a_frame_with_no_regions_is_refused() -> None:
    """A frame measuring nothing is refused by the model, before fusion sees it."""
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        ThermalFrame(
            frame_id="f",
            incident_id=INCIDENT,
            face=FaceLabel.ALPHA,
            observed_at=NOW,
            region_temps_c=(),
        )


def test_void_detection_is_deterministic_and_states_its_threshold() -> None:
    fusion = SensorFusion()
    fusion.register(_frame(FaceLabel.ALPHA, (20.0, 22.0, 90.0, 92.0)))

    first = fusion.voids(INCIDENT, now=NOW)
    second = fusion.voids(INCIDENT, now=NOW)
    assert first == second
    assert len(first) == 1
    assert first[0].region_index == 2
    assert first[0].threshold_c == VOID_DELTA_C
    # An observation about the surface, with the caveat attached.
    assert THERMAL_CAVEAT in first[0].render


def test_a_small_delta_is_not_a_void() -> None:
    fusion = SensorFusion()
    fusion.register(_frame(FaceLabel.ALPHA, (20.0, 30.0, 35.0)))
    assert fusion.voids(INCIDENT, now=NOW) == ()


def test_a_warm_but_uniform_face_is_not_a_void() -> None:
    fusion = SensorFusion()
    fusion.register(_frame(FaceLabel.ALPHA, (85.0, 86.0, 87.0)))
    assert fusion.voids(INCIDENT, now=NOW) == ()


def _spec() -> GeometrySpec:
    return GeometrySpec(
        address_id="sf-0450-hayes",
        generated_at=NOW,
        footprint=((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)),
        faces=tuple(
            Face(label=label)
            for label in (FaceLabel.ALPHA, FaceLabel.BRAVO, FaceLabel.CHARLIE, FaceLabel.DELTA)
        ),
        collapse_zone_radius_m=9.0,
    )


@pytest.mark.invariant
def test_applying_thermal_to_geometry_leaves_unflown_faces_unscanned() -> None:
    fusion = SensorFusion()
    fusion.register(_frame(FaceLabel.ALPHA, (200.0,)))

    applied = fusion.apply_to_geometry(_spec(), INCIDENT, now=NOW)
    by_label = {face.label: face for face in applied.faces}

    assert isinstance(by_label[FaceLabel.ALPHA].thermal, QuantityValue)
    assert by_label[FaceLabel.ALPHA].observed_at == NOW
    for label in (FaceLabel.BRAVO, FaceLabel.CHARLIE, FaceLabel.DELTA):
        assert isinstance(by_label[label].thermal, UnscannedValue)
    assert set(applied.unscanned_faces) == {
        FaceLabel.BRAVO,
        FaceLabel.CHARLIE,
        FaceLabel.DELTA,
    }


@pytest.mark.degraded
def test_a_downed_sensor_feed_is_unavailable_not_unscanned() -> None:
    """ "The drone is offline" and "nobody flew that side" are different facts."""
    fusion = SensorFusion()
    degraded = fusion.unavailable(_spec(), source_id="thermal-drone", reason="feed offline")
    for face in degraded.faces:
        assert isinstance(face.thermal, UnavailableValue)
        assert "UNAVAILABLE" in face.thermal.render()


# ----------------------------------------------------------- the timer


@pytest.mark.invariant
def test_the_truss_window_is_a_published_range_and_the_clock() -> None:
    window = truss_time_window(
        dispatched_at=NOW, now=NOW + timedelta(minutes=9), fact_id="fact-truss"
    )

    assert window.window_min_seconds == int(TRUSS_WINDOW_MIN.total_seconds())
    assert window.window_max_seconds == int(TRUSS_WINDOW_MAX.total_seconds())
    assert window.elapsed_seconds == pytest.approx(540.0)
    assert window.elapsed_exceeds_window_start is True
    assert window.fact_id == "fact-truss"


@pytest.mark.invariant
def test_the_window_cannot_be_rendered_without_its_disclaimer() -> None:
    """The property builds the string, so a template cannot show numbers alone."""
    rendered = truss_time_window(dispatched_at=NOW, now=NOW + timedelta(minutes=4)).render

    assert DISCLAIMER in rendered
    assert "not a prediction" in rendered.lower()
    assert "published test window" in rendered
    # No prediction language anywhere.
    for phrase in ("will collapse", "is going to fail", "expect failure"):
        assert phrase not in rendered.lower()


def test_the_window_is_the_same_whatever_the_clock_says() -> None:
    """The published range does not move; only the elapsed time does."""
    early = truss_time_window(dispatched_at=NOW, now=NOW)
    late = truss_time_window(dispatched_at=NOW, now=NOW + timedelta(hours=1))
    assert early.window_min_seconds == late.window_min_seconds
    assert early.elapsed_exceeds_window_start is False
    assert late.elapsed_exceeds_window_start is True


# --------------------------------------------------------- the resource split


@pytest.mark.invariant
def test_notifications_inform_and_commitments_spend() -> None:
    """The categorisation is a convenience; the gateway is the control."""
    assert set(notification_kinds()) == {
        "water-supply",
        "public-works",
        "exposure",
        "building-department",
    }
    assert set(commitment_kinds()) == {
        "gas-shutoff",
        "electric-shutoff",
        "road-closure",
        "hazmat-team",
        "collapse-rescue",
    }

    for kind in NOTIFICATIONS:
        assert kind.scope is Scope.NOTIFY_AGENCY
        assert kind.is_notification
    for kind in COMMITMENTS:
        assert kind.scope is not Scope.NOTIFY_AGENCY
        assert not kind.is_notification


def test_every_request_kind_names_its_undo() -> None:
    for kind in ALL_KINDS.values():
        assert kind.compensating_action
        assert kind.intent_template
        assert "{address_id}" in kind.intent_template


def test_source_tier_puts_an_ic_resolution_above_a_filed_record() -> None:
    """What the commander saw outranks what the file says."""
    from firstdue.domain.enums import TIER_RANK, tier_for

    assert TIER_RANK[tier_for(SourceType.IC_RESOLUTION)] < TIER_RANK[tier_for(SourceType.PERMIT)]
    assert AssertionStatus.CONFIRMED  # sanity: the enum the brief renders with
