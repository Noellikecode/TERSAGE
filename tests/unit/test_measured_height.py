"""The subtraction the flagship conflict rests on.

In fake mode a surface model reports height above ground directly. Live, no
such feed exists for San Francisco: Google Solar reports roof planes and USGS
3DEP reports the ground, both in the same vertical datum, and the height is the
difference. These tests pin that arithmetic, because ``permit says 2, lidar
measures 3`` is only a finding if the 3 is right.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from firstdue.agents.geometry_watcher import (
    MIN_STRUCTURE_HEIGHT_M,
    measured_height,
    stories_from_height,
)
from firstdue.domain.enums import Classification
from firstdue.ports.sources import SourceRecord

OBSERVED = datetime(2026, 6, 1, tzinfo=UTC)


def _record(ref: str, **fields: object) -> SourceRecord:
    return SourceRecord(
        record_ref=ref,
        address_id="sf-0450-hayes",
        classification=Classification.PUBLIC,
        fields=dict(fields),
        observed_at=OBSERVED,
    )


def test_surface_model_answers_directly() -> None:
    lidar = _record("usgs/dsm", dsm_height_m=9.5, dtm_height_m=0.0)
    result = measured_height(lidar, None)
    assert result is not None
    assert result.height_m == 9.5
    assert result.method == "dsm-minus-dtm"
    assert result.citations == ("usgs/dsm",)


def test_live_pairing_subtracts_ground_from_roof_plane() -> None:
    """A roof plane at 31.4 m over ground at 21.9 m is a 9.5 m building."""
    solar = _record("google-solar/b1", max_plane_height_m=31.4)
    lidar = _record("usgs-3dep/p1", ground_elevation_m=21.9)
    result = measured_height(lidar, solar)
    assert result is not None
    assert result.height_m == pytest.approx(9.5)
    assert result.method == "solar-plane-minus-3dep-ground"


def test_the_derived_height_cites_both_readings() -> None:
    """A subtraction that cites one operand is a number nobody can check."""
    solar = _record("google-solar/b1", max_plane_height_m=31.4)
    lidar = _record("usgs-3dep/p1", ground_elevation_m=21.9)
    result = measured_height(lidar, solar)
    assert result is not None
    assert result.citations == ("google-solar/b1", "usgs-3dep/p1")
    assert "google-solar/b1" in result.source_ref
    assert "usgs-3dep/p1" in result.source_ref


def test_the_derived_height_is_attributed_to_the_roof_measurement() -> None:
    solar = _record("google-solar/b1", max_plane_height_m=31.4)
    lidar = _record("usgs-3dep/p1", ground_elevation_m=21.9)
    result = measured_height(lidar, solar)
    assert result is not None
    assert result.primary.record_ref == "google-solar/b1"


def test_the_live_pairing_reproduces_the_flagship_conflict() -> None:
    """Permit files two storeys; the measurement makes three."""
    solar = _record("google-solar/b1", max_plane_height_m=31.4)
    lidar = _record("usgs-3dep/p1", ground_elevation_m=21.9)
    result = measured_height(lidar, solar)
    assert result is not None
    assert stories_from_height(result.height_m) == 3


@pytest.mark.degraded
def test_only_one_of_the_two_readings_measures_nothing() -> None:
    """Half a subtraction is not a height. It must render as UNKNOWN."""
    solar = _record("google-solar/b1", max_plane_height_m=31.4)
    lidar = _record("usgs-3dep/p1", ground_elevation_m=21.9)
    assert measured_height(None, solar) is None
    assert measured_height(lidar, None) is None
    assert measured_height(None, None) is None


@pytest.mark.degraded
def test_a_missing_field_measures_nothing_rather_than_zero() -> None:
    """Absent readings must not subtract to a zero-height building.

    A building of height zero is one storey with a collapse zone computed from
    nothing, which is worse than admitting the height is unknown.
    """
    solar = _record("google-solar/b1")
    lidar = _record("usgs-3dep/p1", ground_elevation_m=21.9)
    assert measured_height(lidar, solar) is None


@pytest.mark.degraded
def test_an_implausible_difference_is_refused() -> None:
    """A roof below its own ground means the readings disagree about datum."""
    solar = _record("google-solar/b1", max_plane_height_m=10.0)
    lidar = _record("usgs-3dep/p1", ground_elevation_m=21.9)
    assert measured_height(lidar, solar) is None


@pytest.mark.degraded
def test_a_difference_under_one_storey_is_refused() -> None:
    solar = _record("google-solar/b1", max_plane_height_m=23.0)
    lidar = _record("usgs-3dep/p1", ground_elevation_m=21.9)
    assert 0 < 23.0 - 21.9 < MIN_STRUCTURE_HEIGHT_M
    assert measured_height(lidar, solar) is None


def test_the_surface_model_wins_when_both_shapes_are_present() -> None:
    """A direct measurement is not improved by being recomputed."""
    lidar = _record("usgs/dsm", dsm_height_m=9.5, dtm_height_m=0.0, ground_elevation_m=21.9)
    solar = _record("google-solar/b1", max_plane_height_m=99.0)
    result = measured_height(lidar, solar)
    assert result is not None
    assert result.height_m == 9.5
