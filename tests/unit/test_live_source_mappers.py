"""Every live mapper, run against a row the real endpoint actually returned.

The rows in ``tests/fixtures/live_rows`` were captured from the public feeds
named in the catalog. They are here because a mapper written from a dataset's
published column names and never executed is a mapper that is wrong about at
least one column -- which is exactly how phase 3's notes described the risk.

These tests need no network: the capture is checked in. Refreshing it is a
deliberate act, and a schema change upstream shows up as a failure here rather
than as an empty district on a fireground.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from firstdue.domain.enums import Classification
from firstdue.ports.sources import SourceMode
from firstdue.sources.catalog import (
    LIVE_SOURCES,
    UNCONFIGURED_REASONS,
    LiveCredentials,
    _epa_frs_mapper,
    _nrel_mapper,
    _parcel_footprint,
    _sf_assessor_mapper,
    _sf_inspection_mapper,
    _sf_parcel_mapper,
    _sf_permit_mapper,
    _sf_violation_mapper,
    _solar_mapper,
    _usgs_elevation_mapper,
    build_fetcher,
)

LIVE_ROWS = Path(__file__).resolve().parents[1] / "fixtures" / "live_rows"

ROW_MAPPERS = [
    ("sf_permits", _sf_permit_mapper),
    ("sf_assessor", _sf_assessor_mapper),
    ("sf_inspections", _sf_inspection_mapper),
    ("sf_violations", _sf_violation_mapper),
    ("sf_parcels", _sf_parcel_mapper),
    ("epa_frs", _epa_frs_mapper),
]


def _rows(name: str) -> list[dict[str, Any]]:
    return json.loads((LIVE_ROWS / f"{name}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(("name", "mapper"), ROW_MAPPERS)
def test_every_captured_row_maps(name: str, mapper: Any) -> None:
    """No captured row raises, and none maps to nothing."""
    rows = _rows(name)
    assert rows, f"{name} capture is empty"
    for row in rows:
        record = mapper(row)
        assert record is not None, f"{name} dropped a real row"
        assert record.record_ref
        assert record.classification is Classification.PUBLIC


@pytest.mark.parametrize(("name", "mapper"), ROW_MAPPERS)
def test_observation_times_are_timezone_aware(name: str, mapper: Any) -> None:
    """A naive timestamp cannot become a fact -- the fact model refuses it.

    DataSF publishes local time with no offset. Reading it as UTC would shift
    every municipal filing by up to eight hours, which is enough to reorder a
    merge against a same-day survey.
    """
    for row in _rows(name):
        record = mapper(row)
        assert record is not None
        observed = record.observed_at
        assert isinstance(observed, datetime)
        assert observed.tzinfo is not None, f"{name} produced a naive observation time"
        assert observed.tzinfo.utcoffset(observed) is not None


def test_permit_row_carries_the_fields_the_watcher_reads() -> None:
    record = _sf_permit_mapper(_rows("sf_permits")[0])
    assert record is not None
    assert record.fields["permit_number"]
    assert record.fields["street_address"]


def test_assessor_address_survives_the_fixed_width_export() -> None:
    """``0000 1380 GREENWICH           ST0207`` is 1380 Greenwich St.

    The leading block is a unit number and the trailing digits are a suffix
    code. Leaving either in place makes the address unresolvable, and an
    unresolvable address is a filing nobody ever sees.
    """
    record = _sf_assessor_mapper(_rows("sf_assessor")[0])
    assert record is not None
    address = record.fields["street_address"]
    assert address
    assert not address.split()[0].startswith("0000")
    assert not address.split()[-1][-1:].isdigit()


def test_assessor_carries_the_storey_count_the_conflict_rule_needs() -> None:
    """The permit-vs-lidar rule needs a filed storey count to disagree with."""
    for row in _rows("sf_assessor"):
        record = _sf_assessor_mapper(row)
        assert record is not None
        assert "stories_filed" in record.fields
        assert "construction_type" in record.fields


def test_violation_narrative_joins_finding_and_correction() -> None:
    """A corrective action read alone inverts the record it corrects."""
    rows = _rows("sf_violations")
    narrated = [r for r in (_sf_violation_mapper(row) for row in rows) if r and r.document_text]
    assert narrated, "no captured violation carried a narrative"


def test_parcel_footprint_flattens_to_one_ring() -> None:
    for row in _rows("sf_parcels"):
        record = _sf_parcel_mapper(row)
        assert record is not None
        footprint = record.fields["footprint"]
        assert isinstance(footprint, list)
        if footprint:
            assert all(len(point) == 2 for point in footprint)
            assert all(isinstance(value, float) for point in footprint for value in point)


def test_parcel_footprint_handles_both_polygon_nestings() -> None:
    polygon = {"type": "Polygon", "coordinates": [[[1.0, 2.0], [3.0, 4.0]]]}
    multi = {"type": "MultiPolygon", "coordinates": [[[[1.0, 2.0], [3.0, 4.0]]]]}
    assert _parcel_footprint(polygon) == [[1.0, 2.0], [3.0, 4.0]]
    assert _parcel_footprint(multi) == [[1.0, 2.0], [3.0, 4.0]]
    assert _parcel_footprint(None) == []
    assert _parcel_footprint({"coordinates": []}) == []


def test_usgs_maps_a_real_elevation_response() -> None:
    payload = json.loads((LIVE_ROWS / "usgs_3dep.json").read_text(encoding="utf-8"))
    record = _usgs_elevation_mapper(payload, "sf-0450-hayes", 37.7768, -122.4247)
    assert record is not None
    assert isinstance(record.fields["ground_elevation_m"], float)
    assert record.observed_at.tzinfo is not None
    # The observation time is when the lidar was flown, not when we asked.
    assert record.observed_at.year < 2026


def test_usgs_returns_nothing_rather_than_zero_for_an_empty_answer() -> None:
    """A missing elevation must not become a ground plane at sea level."""
    assert _usgs_elevation_mapper({"value": None}, "sf-0450-hayes", 0.0, 0.0) is None
    assert _usgs_elevation_mapper({}, "sf-0450-hayes", 0.0, 0.0) is None


def test_solar_maps_roof_segments_without_inventing_a_height() -> None:
    """Height above ground is a subtraction the watcher does, not the mapper.

    The Solar API reports each roof plane's height in the same datum as the
    elevation service. Turning that into a building height requires the ground
    reading, and a mapper that guessed would be inventing the number the whole
    product turns on.
    """
    payload = {
        "name": "buildings/abc",
        "imageryDate": {"year": 2024, "month": 6, "day": 1},
        "solarPotential": {
            "maxArrayPanelsCount": 24,
            "wholeRoofStats": {"groundAreaMeters2": 180.0},
            "roofSegmentStats": [
                {
                    "pitchDegrees": 18.0,
                    "azimuthDegrees": 210.0,
                    "planeHeightAtCenterMeters": 31.4,
                    "stats": {"groundAreaMeters2": 90.0},
                }
            ],
        },
    }
    record = _solar_mapper(payload, "sf-0450-hayes", 37.7768, -122.4247)
    assert record is not None
    assert record.fields["max_plane_height_m"] == 31.4
    assert record.fields["solar_array_present"] is True
    assert record.fields["segment_count"] == 1
    assert "height_m" not in record.fields
    assert record.observed_at.year == 2024


def test_solar_without_segments_maps_to_nothing() -> None:
    assert _solar_mapper({"solarPotential": {}}, "sf-0450-hayes", 0.0, 0.0) is None


def test_nrel_collapses_stations_into_one_presence_record() -> None:
    payload = {
        "fuel_stations": [
            {"distance": 0.31, "station_name": "Garage A", "ev_connector_types": ["J1772"]},
            {"distance": 0.12, "station_name": "Garage B", "ev_connector_types": ["TESLA"]},
        ]
    }
    record = _nrel_mapper(payload, "sf-0450-hayes", 37.7768, -122.4247)
    assert record is not None
    assert record.fields["station_count"] == 2
    assert record.fields["nearest_distance_miles"] == 0.12
    assert record.fields["connector_types"] == ["J1772", "TESLA"]


def test_nrel_with_no_stations_reports_absence_not_a_dropped_record() -> None:
    """ "No chargers found" is an answer. It must reach the profile as one."""
    record = _nrel_mapper({"fuel_stations": []}, "sf-0450-hayes", 0.0, 0.0)
    assert record is not None
    assert record.fields["ev_present"] is False
    assert record.fields["station_count"] == 0


# ------------------------------------------------------------ configuration --


def test_live_mode_without_a_key_is_unconfigured_never_a_fixture(tmp_path: Path) -> None:
    """A missing key degrades to UNCONFIGURED, not to synthetic records."""
    fetcher = build_fetcher(
        "google-solar",
        fixtures_dir=tmp_path,
        live=True,
        resolver=lambda _: (37.0, -122.0),
        credentials=LiveCredentials(),
    )
    assert fetcher.mode is SourceMode.UNCONFIGURED


def test_live_mode_with_a_key_builds_a_live_fetcher(tmp_path: Path) -> None:
    fetcher = build_fetcher(
        "google-solar",
        fixtures_dir=tmp_path,
        live=True,
        resolver=lambda _: (37.0, -122.0),
        credentials=LiveCredentials(maps_api_key="test-key"),
    )
    assert fetcher.mode is SourceMode.LIVE


def test_point_sources_without_a_resolver_are_unconfigured(tmp_path: Path) -> None:
    """A point source cannot invent the coordinate it is asked about."""
    for source_id in ("usgs-3dep", "nws"):
        fetcher = build_fetcher(source_id, fixtures_dir=tmp_path, live=True, resolver=None)
        assert fetcher.mode is SourceMode.UNCONFIGURED


def test_sources_with_no_public_feed_say_why(tmp_path: Path) -> None:
    """ "Unavailable" and "withheld by statute" are different statements."""
    for source_id, reason in UNCONFIGURED_REASONS.items():
        assert source_id not in LIVE_SOURCES
        fetcher = build_fetcher(source_id, fixtures_dir=tmp_path, live=True)
        assert fetcher.mode is SourceMode.UNCONFIGURED
        assert reason in getattr(fetcher, "note", "")


def test_fake_mode_never_builds_a_live_fetcher(tmp_path: Path) -> None:
    from firstdue.sources.catalog import CATALOG

    for config in CATALOG:
        fetcher = build_fetcher(config.source_id, fixtures_dir=tmp_path, live=False)
        assert fetcher.mode is SourceMode.FIXTURE


def test_the_city_declares_exactly_the_sources_the_catalog_builds() -> None:
    """The declared source list and the built catalog cannot drift apart.

    ``sf-hydrants`` and ``nws`` were named by the city adapter for five phases
    while no adapter existed for either, so a caller asking the registry for
    them got a ``KeyError`` rather than an honest ``UNCONFIGURED``. This is the
    test that would have caught it.
    """
    from firstdue.city.san_francisco import SanFranciscoAdapter
    from firstdue.sources.catalog import CATALOG

    city = SanFranciscoAdapter(Path("fixtures"))
    declared = set(city.source_ids())
    built = {config.source_id for config in CATALOG}
    assert (
        declared == built
    ), f"declared-but-unbuilt: {declared - built}; built-but-undeclared: {built - declared}"


def test_every_catalogued_source_has_a_fixture_for_fake_mode() -> None:
    from firstdue.sources.catalog import CATALOG, FIXTURE_FILES

    for config in CATALOG:
        assert config.source_id in FIXTURE_FILES
        path = Path("fixtures") / "san-francisco" / "sources" / FIXTURE_FILES[config.source_id]
        assert path.is_file(), f"{config.source_id} has no fixture at {path}"


def test_every_catalogued_source_is_either_live_or_explains_itself() -> None:
    """No source may be silently unreachable: it is live, or it says why not."""
    from firstdue.sources.catalog import CATALOG

    for config in CATALOG:
        assert (
            config.source_id in LIVE_SOURCES or config.source_id in UNCONFIGURED_REASONS
        ), f"{config.source_id} is neither live nor explained"
