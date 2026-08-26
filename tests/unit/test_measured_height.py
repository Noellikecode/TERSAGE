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


# --------------------------------------------------- live-source geometry ---


def test_a_point_source_is_asked_about_one_address_not_a_district() -> None:
    """Solar and 3DEP answer per address; a district sweep never reaches them.

    This is why the massing model was a constant. The watcher fetched all three
    geometry sources with no address, which a fixture answers in bulk and a live
    point source refuses outright with ``address_required`` -- so the agent
    produced measured geometry in fake mode and none at all against Google
    Solar and USGS, while the console still captioned the result "measured
    height".
    """
    import inspect

    from firstdue.agents import geometry_watcher

    source = inspect.getsource(geometry_watcher.GeometryWatcher.poll)
    # The parcel sweep is district-wide; the point sources are per address.
    assert "await parcels.fetch()" in source
    assert "fetch(address_id=address_id)" in source


def test_targets_come_from_the_departments_own_profiles() -> None:
    """Not from whatever a source happened to attribute.

    The live parcel feed returns rows keyed by block-and-lot with no address id,
    so a target list built from the sweep is empty against real data and full
    against a fixture.
    """
    import inspect

    from firstdue.agents import geometry_watcher

    source = inspect.getsource(geometry_watcher.GeometryWatcher.poll)
    assert "list_by_district(district_id)" in source


def test_a_measured_roof_area_sizes_the_footprint() -> None:
    """Right size, no claim about shape."""
    from firstdue.agents.geometry_watcher import DEFAULT_FOOTPRINT, _footprint_of_area

    ring = _footprint_of_area(398.13, DEFAULT_FOOTPRINT)
    width = max(p[0] for p in ring) - min(p[0] for p in ring)
    depth = max(p[1] for p in ring) - min(p[1] for p in ring)
    assert round(width * depth) == 398
    # And it is no longer the constant every structure in the district shared.
    assert ring != DEFAULT_FOOTPRINT


def test_an_absent_or_unusable_area_falls_back_rather_than_guessing() -> None:
    from firstdue.agents.geometry_watcher import DEFAULT_FOOTPRINT, _footprint_of_area

    for bad in (None, "", "not-a-number", 0, -5):
        assert _footprint_of_area(bad, DEFAULT_FOOTPRINT) == DEFAULT_FOOTPRINT


def test_staleness_decides_before_a_metered_request_is_spent() -> None:
    """Filter first, fetch second.

    Fetching every candidate and filtering after is the same answer and a Solar
    request per structure in the district -- 135 calls to re-derive the handful
    whose records moved since the last flight, on every boot.
    """
    import inspect

    from firstdue.agents import geometry_watcher

    source = inspect.getsource(geometry_watcher.GeometryWatcher.poll)
    stale_at = source.index("geometry_is_stale(profile)")
    fetch_at = source.index("fetch(address_id=address_id)")
    assert stale_at < fetch_at, "point sources must be asked only about stale profiles"


def test_the_seeded_flight_predates_the_permit_that_disputes_it() -> None:
    """Otherwise `geometry-watcher` skips the district on every pass.

    The seed used to date its spec five days *after* the newest
    geometry-invalidating fact, so no seeded profile was ever stale and the
    agent that measures a building never ran in the demo at all. The model on
    screen was the seed's own literal under a caption reading "measured height".
    """
    from datetime import datetime

    from firstdue.agents.geometry_watcher import geometry_is_stale
    from firstdue.city.san_francisco import SanFranciscoAdapter
    from firstdue.demo.seed import build_seed, profiles_from_seed
    from firstdue.settings import get_settings

    # Built here, not loaded from `.demo-state`: a unit test that reads a file
    # somebody has to run `firstdue seed` to produce passes on the machine that
    # just ran it and fails everywhere else.
    settings = get_settings()
    city = SanFranciscoAdapter(settings.fixtures_dir)
    document = build_seed(
        addresses=list(city.list_addresses()),
        epoch=datetime.fromisoformat(settings.demo_epoch),
        seed=settings.demo_seed,
    )
    stale = [p.address_id for p in profiles_from_seed(document) if geometry_is_stale(p)]
    assert stale, "no seeded profile is stale, so the watcher will skip every one"


# ------------------------------------------ a height is not a storey count ---


def test_a_tower_and_its_permit_are_not_a_disagreement() -> None:
    """415 Mission is Salesforce Tower. Both records are right.

    325 m is the real height and 62 storeys is the real filing; together they
    imply 5.25 m floors, which is what an office tower is built at. Dividing the
    height by a flat 3.2 m residential ceiling reported 102 storeys and raised a
    severity-5 conflict against a building nobody had mismeasured -- and put it
    at the top of the standby screen, where it read as the system being broken.
    """
    from firstdue.agents.geometry_watcher import (
        records_agree_on_stories,
        storey_height_implied_by,
    )

    assert round(storey_height_implied_by(325.75, 62) or 0, 2) == 5.25
    assert records_agree_on_stories(325.75, 62)


def test_a_height_no_ceiling_can_explain_is_still_a_disagreement() -> None:
    """The rule must not simply agree with everything.

    450 Hayes measures 16.29 m against a filed 2 storeys: 8.1 m ceilings, which
    nobody builds. That is the finding the whole product is about, and it has to
    survive the fix for the tower.
    """
    from firstdue.agents.geometry_watcher import (
        records_agree_on_stories,
        storey_height_implied_by,
    )

    assert round(storey_height_implied_by(16.29, 2) or 0, 1) == 8.1
    assert not records_agree_on_stories(16.29, 2)
    # And a squashed one: 3 m over two storeys is 1.5 m of headroom.
    assert not records_agree_on_stories(3.03, 2)


def test_an_unusable_pair_does_not_clear_a_conflict() -> None:
    """Absent or nonsensical inputs report the finding, never suppress it."""
    from firstdue.agents.geometry_watcher import (
        records_agree_on_stories,
        storey_height_implied_by,
    )

    assert storey_height_implied_by(16.29, 0) is None
    assert not records_agree_on_stories(16.29, 0)
    assert not records_agree_on_stories(0.5, 1)


# ---------------------------------------------- the model uses its records ---


def test_a_roof_does_not_size_a_building_that_tapers() -> None:
    """Salesforce Tower's roof is 684 m2 and its floor plate is 3,200 m2.

    Sizing the massing model from the roof drew it seventeen times taller than
    it was wide. Preferring the filing outright is the wrong correction -- at
    450 Hayes the assessor's number is *smaller* than what Solar measured, and
    nothing else in this system lets a filing overwrite a measurement. A roof
    cannot overhang the whole floor plate, so the larger number is the one the
    footprint has to clear.
    """
    from firstdue.agents.geometry_watcher import DEFAULT_FOOTPRINT, _footprint_of_area

    def area_of(ring: tuple[tuple[float, float], ...]) -> float:
        return (max(p[0] for p in ring) - min(p[0] for p in ring)) * (
            max(p[1] for p in ring) - min(p[1] for p in ring)
        )

    tower = _footprint_of_area(max(684.0, 3200.0), DEFAULT_FOOTPRINT)
    assert round(area_of(tower)) == 3200
    hayes = _footprint_of_area(max(398.13, 240.0), DEFAULT_FOOTPRINT)
    assert round(area_of(hayes)) == 398


def test_a_filed_area_keeps_its_unit() -> None:
    """The assessor files `3200 m2`, Solar reports `398.13`. Both are areas."""
    from firstdue.agents.geometry_watcher import _area_of

    assert _area_of("3200 m2") == 3200.0
    assert _area_of(398.13) == 398.13
    for bad in (None, "", "nonsense", 0, -1):
        assert _area_of(bad) is None


def test_the_model_draws_the_filed_storeys_when_the_height_allows_them() -> None:
    """And the measured ones when it does not.

    The renderer used to divide height by an assumed 3.2 m ceiling regardless,
    so a 62-storey tower was drawn with 102 levels -- the same guess the
    conflict rule had already declined to make.
    """
    from firstdue.agents.geometry_watcher import records_agree_on_stories

    # 325 m over 62 storeys is 5.25 m a floor: draw the 62 that were filed.
    assert records_agree_on_stories(325.75, 62)
    # 16.29 m over 2 is 8.1 m a floor: the filing cannot stand, so the measured
    # count is drawn and everything above the filing stays DISPUTED.
    assert not records_agree_on_stories(16.29, 2)
