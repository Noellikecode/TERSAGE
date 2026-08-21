"""The San Francisco source catalog.

Eleven sources, each one config plus one fetcher. The live URLs are the real
public endpoints; the fixtures are synthetic records attached to real public
street addresses.

Which fetcher a source gets is decided here and nowhere else:

* fake mode -- every source is fixture-backed, and says so;
* live mode -- sources with a reachable public feed get :class:`HttpFetcher`,
  and the rest get :class:`UnconfiguredFetcher`, which reports ``UNCONFIGURED``
  and raises on fetch so the resulting fact is ``UNAVAILABLE``.

That last part is the honest bit. Tier II filings are confidential and have no
public API; the county emergency-management feed needs an agreement we do not
have. Those sources exist in the catalog, are visibly unconfigured, and never
quietly return an empty list that reads as "no hazardous materials present".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

from firstdue.domain.enums import Classification, SourceType
from firstdue.ports.city import CityAdapter
from firstdue.ports.clock import Clock
from firstdue.ports.sources import SourceAdapter, SourceRecord
from firstdue.sources.fetchers import (
    FixtureFetcher,
    HttpFetcher,
    NwsPointFetcher,
    PointFetcher,
    PointResolver,
)
from firstdue.sources.framework import ManagedSource, PageFetcher, SourceConfig, UnconfiguredFetcher

#: SF's open-data platform. Real endpoints; the dataset ids are the public ones.
SF_OPEN_DATA: Final[str] = "https://data.sfgov.org/resource"

PERMITS = "sf-permits"
ASSESSOR = "sf-assessor"
INSPECTIONS = "sf-fire-inspections"
VIOLATIONS = "sf-violations"
PARCELS = "sf-parcels"
SOLAR = "google-solar"
LIDAR = "usgs-3dep"
EPA = "epa-frs"
PHMSA = "phmsa-pipelines"
NREL = "nrel-ev"
TIER_II = "tier-ii-confidential"
HYDRANTS = "sf-hydrants"
WEATHER = "nws"


def _config(
    source_id: str,
    source_type: SourceType,
    classification: Classification = Classification.PUBLIC,
    *,
    rate_per_second: float = 5.0,
    cache_minutes: int = 15,
) -> SourceConfig:
    return SourceConfig(
        source_id=source_id,
        source_type=source_type,
        classification=classification,
        rate_per_second=rate_per_second,
        cache_ttl=timedelta(minutes=cache_minutes),
    )


#: Every source this build knows, with the type and classification it returns.
CATALOG: Final[tuple[SourceConfig, ...]] = (
    # Municipal filings. Rate limits are the conservative published ones.
    _config(PERMITS, SourceType.PERMIT, rate_per_second=4.0, cache_minutes=30),
    _config(ASSESSOR, SourceType.ASSESSOR, rate_per_second=4.0, cache_minutes=720),
    _config(INSPECTIONS, SourceType.FIRE_INSPECTION, rate_per_second=4.0, cache_minutes=60),
    _config(VIOLATIONS, SourceType.VIOLATION, rate_per_second=4.0, cache_minutes=60),
    _config(PARCELS, SourceType.PARCEL, rate_per_second=4.0, cache_minutes=1440),
    # Remote measurement. Solar API is metered, so it is cached hard.
    _config(SOLAR, SourceType.SOLAR_API, rate_per_second=1.0, cache_minutes=1440),
    _config(LIDAR, SourceType.LIDAR_DSM, rate_per_second=1.0, cache_minutes=1440),
    # Federal hazard registries.
    _config(EPA, SourceType.EPA_FRS, rate_per_second=2.0, cache_minutes=720),
    _config(PHMSA, SourceType.PHMSA_PIPELINE, rate_per_second=2.0, cache_minutes=1440),
    _config(NREL, SourceType.NREL_EV, rate_per_second=2.0, cache_minutes=720),
    # Department water supply. Catalogued because the city adapter names it;
    # San Francisco publishes no open hydrant feed, so live mode says so.
    _config(HYDRANTS, SourceType.HYDRANT, rate_per_second=4.0, cache_minutes=1440),
    # Live observation. Short cache: fireground weather is a current question.
    _config(WEATHER, SourceType.NWS_OBSERVATION, rate_per_second=2.0, cache_minutes=15),
    # Confidential. No public endpoint exists, by statute.
    _config(TIER_II, SourceType.TIER_II, Classification.TIER_II_CONFIDENTIAL, cache_minutes=720),
)

#: Fixture file per source, relative to ``fixtures/san-francisco/sources``.
FIXTURE_FILES: Final[dict[str, str]] = {
    PERMITS: "permits.json",
    ASSESSOR: "assessor.json",
    INSPECTIONS: "inspections.json",
    VIOLATIONS: "violations.json",
    PARCELS: "parcels.json",
    SOLAR: "solar.json",
    LIDAR: "lidar.json",
    EPA: "epa.json",
    PHMSA: "phmsa.json",
    NREL: "nrel-ev.json",
    TIER_II: "tier-ii.json",
    HYDRANTS: "hydrants.json",
    WEATHER: "weather.json",
}


#: San Francisco's open-data platform publishes local timestamps with no
#: offset. Reading them as UTC would shift every municipal filing by seven or
#: eight hours, which is enough to reorder a merge against a same-day survey.
SF_TZ: Final[ZoneInfo] = ZoneInfo("America/Los_Angeles")


def _parse_observed(raw: Any, tz: tzinfo = UTC) -> datetime:
    """Parse an upstream timestamp, attaching the source's zone if it has none.

    A naive timestamp on a fact is an ambiguity nobody can resolve two years
    later, and the fact model refuses one outright -- so the zone is decided
    here, per source, by whoever knows what the publisher meant.
    """
    parsed = datetime.fromisoformat(str(raw))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=tz)


def _clean(raw: Any) -> str:
    """Collapse the whitespace municipal exports are padded with."""
    return " ".join(str(raw or "").split())


def _observed_or(raw: Any, *fallbacks: Any, tz: tzinfo = UTC) -> datetime:
    """First parseable timestamp among the candidates.

    Municipal exports disagree about which date field is populated on any given
    row, so a mapper names the ones it will accept in preference order and this
    picks the first that parses. A row with none of them is unmappable and the
    fetcher skips it -- a record with no observation time cannot be merged,
    because merge order is defined by observation time.
    """
    for candidate in (raw, *fallbacks):
        if candidate in (None, ""):
            continue
        try:
            return _parse_observed(candidate, tz)
        except ValueError:
            continue
    raise ValueError("no parseable observation time on this row")


# ------------------------------------------------------------------ mappers --
#
# One mapper per live feed. Each is a pure function from the upstream row to a
# SourceRecord, and each is exercised by a test against a captured real row --
# a mapper written from published column names and never run is a mapper that
# is wrong about at least one column.


def _sf_permit_mapper(row: dict[str, Any]) -> SourceRecord | None:
    """Map one row of SF's building-permits dataset.

    The address resolves later, in the watcher, through the city adapter -- the
    fetcher does not get to guess which building a row belongs to.
    """
    number = row.get("permit_number")
    if not number:
        return None
    description = str(row.get("description") or "")
    return SourceRecord(
        record_ref=f"permit/{number}",
        address_id=None,
        classification=Classification.PUBLIC,
        fields={
            "permit_number": number,
            "status": row.get("status"),
            "permit_type": row.get("permit_type_definition"),
            "street_address": " ".join(
                str(row.get(part, "")).strip()
                for part in ("street_number", "street_name", "street_suffix")
            ).strip(),
            "stories_filed": row.get("number_of_existing_stories"),
        },
        document_text=description or None,
        observed_at=_parse_observed(row.get("filed_date") or row.get("issued_date"), SF_TZ),
    )


def _sf_assessor_mapper(row: dict[str, Any]) -> SourceRecord | None:
    """Map one row of the assessor's secured property roll.

    ``property_location`` is a fixed-width export field: a zero-padded unit
    prefix, the street address, then a suffix code. Only the middle is an
    address, and the rest is dropped rather than guessed at.
    """
    parcel = row.get("parcel_number")
    if not parcel:
        return None
    roll_year = row.get("closed_roll_year")
    return SourceRecord(
        record_ref=f"assessor/{parcel}/{roll_year}",
        address_id=None,
        classification=Classification.PUBLIC,
        fields={
            "parcel_number": parcel,
            "block": row.get("block"),
            "lot": row.get("lot"),
            "street_address": _assessor_address(row.get("property_location")),
            "stories_filed": row.get("number_of_stories"),
            "construction_type": row.get("construction_type"),
            "year_built": row.get("year_property_built"),
            "property_area": row.get("property_area"),
            "units": row.get("number_of_units"),
            "use": row.get("use_definition"),
        },
        # The roll carries no narrative. Its values are filed columns, so no
        # extraction span is required and none is invented.
        document_text=None,
        observed_at=_observed_or(row.get("data_as_of"), row.get("data_loaded_at"), tz=SF_TZ),
    )


def _assessor_address(raw: Any) -> str:
    """Pull the street address out of a fixed-width roll location field.

    ``0000 1380 GREENWICH           ST0207`` is unit ``0000``, address
    ``1380 GREENWICH ST``, suffix code ``0207``. The leading unit block and the
    trailing code are export scaffolding, not part of any address.
    """
    text = _clean(raw)
    if not text:
        return ""
    parts = text.split()
    if parts and parts[0].isdigit() and len(parts[0]) == 4 and parts[0].startswith("0"):
        parts = parts[1:]
    if parts:
        # The final token fuses the street type with a numeric suffix code.
        tail = parts[-1]
        letters = "".join(ch for ch in tail if ch.isalpha())
        if letters and letters != tail:
            parts[-1] = letters
    return " ".join(parts)


def _sf_inspection_mapper(row: dict[str, Any]) -> SourceRecord | None:
    """Map one SFFD fire-inspection row."""
    number = row.get("inspection_number")
    if not number:
        return None
    description = _clean(row.get("inspection_type_description"))
    return SourceRecord(
        record_ref=f"inspection/{number}",
        address_id=None,
        classification=Classification.PUBLIC,
        fields={
            "inspection_number": number,
            "street_address": _clean(row.get("address")),
            "inspection_type": row.get("inspection_type"),
            "status": row.get("inspection_status"),
            "station": row.get("station"),
            "battalion": row.get("battalion"),
        },
        document_text=description or None,
        observed_at=_observed_or(
            row.get("inspection_start_date"),
            row.get("inspection_end_date"),
            row.get("data_as_of"),
            tz=SF_TZ,
        ),
    )


def _sf_violation_mapper(row: dict[str, Any]) -> SourceRecord | None:
    """Map one SFFD fire-violation row.

    This is the narrative source the whole extraction path exists for. The
    violation item and its corrective action are joined into one document,
    because a corrective action read without the finding it corrects inverts
    the meaning of the record.
    """
    violation_id = row.get("violation_id") or row.get("violation_number")
    if not violation_id:
        return None
    item = _clean(row.get("violation_item_description"))
    corrective = _clean(row.get("corrective_action"))
    narrative = ". ".join(part for part in (item, corrective) if part)
    return SourceRecord(
        record_ref=f"violation/{violation_id}",
        address_id=None,
        classification=Classification.PUBLIC,
        fields={
            "violation_id": violation_id,
            "street_address": _clean(row.get("address")),
            "violation_item": row.get("violation_item"),
            "status": row.get("status"),
            "close_date": row.get("close_date"),
            "inspection_number": row.get("inspection_number"),
        },
        document_text=narrative or None,
        observed_at=_observed_or(row.get("violation_date"), row.get("data_as_of"), tz=SF_TZ),
    )


def _sf_parcel_mapper(row: dict[str, Any]) -> SourceRecord | None:
    """Map one parcel row: the footprint the geometry watcher extrudes."""
    blklot = row.get("blklot") or row.get("mapblklot")
    if not blklot:
        return None
    street = " ".join(
        part for part in (_clean(row.get("street_name")), _clean(row.get("street_type"))) if part
    )
    number = _clean(row.get("from_address_num"))
    return SourceRecord(
        record_ref=f"parcel/{blklot}",
        address_id=None,
        classification=Classification.PUBLIC,
        fields={
            "parcel_ref": blklot,
            "block": row.get("block_num"),
            "lot": row.get("lot_num"),
            "street_address": f"{number} {street}".strip(),
            "from_address_num": row.get("from_address_num"),
            "to_address_num": row.get("to_address_num"),
            "latitude": row.get("centroid_latitude"),
            "longitude": row.get("centroid_longitude"),
            "footprint": _parcel_footprint(row.get("shape")),
            "zoning": row.get("zoning_code"),
            "active": row.get("active"),
        },
        document_text=None,
        observed_at=_observed_or(row.get("data_as_of"), row.get("date_map_add"), tz=SF_TZ),
    )


def _parcel_footprint(shape: Any) -> list[list[float]]:
    """Flatten a parcel MultiPolygon into one outer ring.

    The renderer extrudes a single ring, and a parcel's outer boundary is the
    footprint. Interior rings and additional polygons are dropped here rather
    than half-rendered downstream.
    """
    if not isinstance(shape, dict):
        return []
    coordinates = shape.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        return []
    node: Any = coordinates
    # Descend to the first coordinate ring: MultiPolygon nests one level
    # deeper than Polygon, and the parcel export uses both.
    while (
        isinstance(node, list)
        and node
        and isinstance(node[0], list)
        and node[0]
        and isinstance(node[0][0], list)
    ):
        node = node[0]
    if not isinstance(node, list):
        return []
    ring: list[list[float]] = []
    for point in node:
        if isinstance(point, list) and len(point) >= 2:
            try:
                ring.append([float(point[0]), float(point[1])])
            except (TypeError, ValueError):
                continue
    return ring


def _epa_frs_mapper(row: dict[str, Any]) -> SourceRecord | None:
    """Map one EPA Facility Registry Service row."""
    registry_id = row.get("registry_id")
    if not registry_id:
        return None
    program = _clean(row.get("pgm_sys_acrnm"))
    return SourceRecord(
        record_ref=f"epa-frs/{registry_id}/{program or 'unknown'}",
        address_id=None,
        classification=Classification.PUBLIC,
        fields={
            "registry_id": registry_id,
            "facility_name": _clean(row.get("primary_name")),
            "street_address": _clean(row.get("location_address")),
            "postal_code": row.get("postal_code"),
            "program": program,
            "program_id": row.get("pgm_sys_id"),
            "site_type": row.get("site_type_name"),
        },
        document_text=None,
        observed_at=_observed_or(
            row.get("update_date"),
            row.get("last_reported_date"),
            row.get("create_date"),
            EPA_FALLBACK_OBSERVED,
        ),
    )


#: EPA rows frequently carry no populated date at all. Rather than drop every
#: such row -- which would render a real hazard registry as empty -- they are
#: stamped with the epoch the catalog treats as "on file, date not published".
#: A filed hazard with an unknown filing date is still a filed hazard.
EPA_FALLBACK_OBSERVED: Final[str] = "1970-01-01T00:00:00+00:00"


def _usgs_elevation_mapper(
    payload: dict[str, Any], address_id: str, latitude: float, longitude: float
) -> SourceRecord | None:
    """Map a 3DEP point-elevation response.

    EPQS answers with the bare-earth elevation at a coordinate. That is the
    ground datum a roof height is measured *from*; it is not a building height
    and this mapper does not pretend it is one.
    """
    value = payload.get("value")
    if value in (None, ""):
        return None
    attributes = payload.get("attributes") or {}
    acquired = _clean(attributes.get("AcquisitionDate"))
    return SourceRecord(
        record_ref=f"usgs-3dep/{address_id}/{acquired or 'undated'}",
        address_id=address_id,
        classification=Classification.PUBLIC,
        fields={
            "ground_elevation_m": float(str(value)),
            "resolution_m": payload.get("resolution"),
            "raster_id": payload.get("rasterId"),
            "acquisition_date": acquired,
            "latitude": latitude,
            "longitude": longitude,
        },
        document_text=None,
        observed_at=_usgs_observed(acquired),
    )


def _usgs_observed(acquired: str) -> datetime:
    """Parse 3DEP's ``M/D/YYYY`` acquisition date.

    The observation time of an elevation reading is when the lidar was flown,
    not when we asked. Getting this wrong would let a 2023 survey lose a merge
    to a stale filing on recency.
    """
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(acquired, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError("unparseable 3DEP acquisition date")


def _solar_mapper(
    payload: dict[str, Any], address_id: str, latitude: float, longitude: float
) -> SourceRecord | None:
    """Map a Google Solar ``buildingInsights`` response.

    The roof segments carry pitch, azimuth, and the height of each plane's
    centre. Height above ground is *not* in this response -- it is the plane
    height minus the 3DEP ground elevation, and that subtraction happens in the
    geometry watcher where both facts are in hand. A mapper that guessed at it
    would be inventing the number the whole product turns on.
    """
    potential = payload.get("solarPotential") or {}
    segments = potential.get("roofSegmentStats") or []
    if not segments:
        return None

    mapped_segments = [
        {
            "pitch_deg": segment.get("pitchDegrees"),
            "azimuth_deg": segment.get("azimuthDegrees"),
            "plane_height_m": (segment.get("planeHeightAtCenterMeters")),
            "area_m2": (segment.get("stats") or {}).get("groundAreaMeters2"),
        }
        for segment in segments
        if isinstance(segment, dict)
    ]
    heights = [
        float(str(segment["plane_height_m"]))
        for segment in mapped_segments
        if segment.get("plane_height_m") is not None
    ]
    return SourceRecord(
        record_ref=f"google-solar/{payload.get('name') or address_id}",
        address_id=address_id,
        classification=Classification.PUBLIC,
        fields={
            "roof_segments": mapped_segments,
            "segment_count": len(mapped_segments),
            "max_plane_height_m": max(heights) if heights else None,
            "panel_count": potential.get("maxArrayPanelsCount"),
            "roof_area_m2": potential.get("wholeRoofStats", {}).get("groundAreaMeters2"),
            "solar_array_present": bool(potential.get("maxArrayPanelsCount")),
            "latitude": latitude,
            "longitude": longitude,
        },
        document_text=None,
        observed_at=_solar_observed(payload.get("imageryDate")),
    )


def _solar_observed(imagery_date: Any) -> datetime:
    """Turn Solar's ``{year, month, day}`` imagery date into an instant."""
    if not isinstance(imagery_date, dict):
        raise ValueError("solar response carries no imagery date")
    return datetime(
        int(imagery_date["year"]),
        int(imagery_date.get("month") or 1),
        int(imagery_date.get("day") or 1),
        tzinfo=UTC,
    )


def _nrel_mapper(
    payload: dict[str, Any], address_id: str, latitude: float, longitude: float
) -> SourceRecord | None:
    """Map an NREL alternative-fuel-station response to one EV-presence record.

    The hazard is the presence and density of high-capacity batteries near the
    structure, not any individual charger, so the stations collapse into one
    record per address rather than one fact per charger.
    """
    stations = payload.get("fuel_stations") or []
    if not isinstance(stations, list):
        return None
    nearest = min(
        (s for s in stations if isinstance(s, dict) and s.get("distance") is not None),
        key=lambda s: float(s["distance"]),
        default=None,
    )
    return SourceRecord(
        record_ref=f"nrel-ev/{address_id}",
        address_id=address_id,
        classification=Classification.PUBLIC,
        fields={
            "station_count": len(stations),
            "nearest_distance_miles": (float(nearest["distance"]) if nearest is not None else None),
            "nearest_station": _clean(nearest.get("station_name")) if nearest else None,
            "connector_types": sorted(
                {
                    str(connector)
                    for station in stations
                    if isinstance(station, dict)
                    for connector in (station.get("ev_connector_types") or [])
                }
            ),
            "ev_present": bool(stations),
            "latitude": latitude,
            "longitude": longitude,
        },
        document_text=None,
        observed_at=_observed_or(payload.get("last_updated"), NREL_FALLBACK_OBSERVED),
    )


#: NREL does not stamp the aggregate response with a time. The station list is
#: a current-state answer, so an undated response is treated as of the epoch
#: and superseded by the next poll that does carry a date.
NREL_FALLBACK_OBSERVED: Final[str] = "1970-01-01T00:00:00+00:00"


# ---------------------------------------------------------------- endpoints --


@dataclass(frozen=True, slots=True)
class LiveSourceSpec:
    """How one source reaches its live feed.

    ``kind`` decides which fetcher is built. ``requires`` names the setting an
    operator has to supply; a spec whose requirement is unmet degrades to
    ``UNCONFIGURED`` rather than to a fixture, because a live-mode process
    serving synthetic records would be lying about where its data came from.
    """

    kind: Literal["rows", "point", "weather"]
    url: str = ""
    mapper: Any = None
    params: dict[str, str] = field(default_factory=dict)
    rows_path: tuple[str, ...] = ()
    since_param: str | None = None
    offset_param: str | None = "$offset"
    limit_param: str | None = "$limit"
    lat_param: str = "y"
    lon_param: str = "x"
    requires: Literal["none", "maps_key", "nrel_key"] = "none"


#: Why a catalogued source has no live endpoint. Rendered by the console, so an
#: officer reading ``UNAVAILABLE`` can see whether it is an outage or a policy.
UNCONFIGURED_REASONS: Final[dict[str, str]] = {
    PHMSA: (
        "PHMSA's National Pipeline Mapping System restricts programmatic access "
        "to pipeline centrelines; there is no public feed to poll"
    ),
    TIER_II: (
        "Tier II facility filings are confidential under EPCRA and are held by "
        "county emergency management; no public endpoint exists by statute"
    ),
    HYDRANTS: (
        "San Francisco publishes no open hydrant dataset; hydrant identifiers "
        "reach the profile from the department's own records"
    ),
}

#: Live endpoints, one per source that has a reachable feed.
LIVE_SOURCES: Final[dict[str, LiveSourceSpec]] = {
    PERMITS: LiveSourceSpec(
        kind="rows",
        url=f"{SF_OPEN_DATA}/i98e-djp9.json",
        mapper=_sf_permit_mapper,
        since_param="$where",
    ),
    ASSESSOR: LiveSourceSpec(
        kind="rows",
        url=f"{SF_OPEN_DATA}/wv5m-vpq2.json",
        mapper=_sf_assessor_mapper,
        # The roll is published per closed year; the most recent is the one a
        # current structural question is asked against.
        params={"$order": "closed_roll_year DESC"},
    ),
    INSPECTIONS: LiveSourceSpec(
        kind="rows",
        url=f"{SF_OPEN_DATA}/wb4c-6hwj.json",
        mapper=_sf_inspection_mapper,
        params={"$order": "inspection_start_date DESC"},
    ),
    VIOLATIONS: LiveSourceSpec(
        kind="rows",
        url=f"{SF_OPEN_DATA}/4zuq-2cbe.json",
        mapper=_sf_violation_mapper,
        params={"$order": "violation_date DESC"},
    ),
    PARCELS: LiveSourceSpec(
        kind="rows",
        url=f"{SF_OPEN_DATA}/acdm-wktn.json",
        mapper=_sf_parcel_mapper,
        params={"active": "true"},
    ),
    EPA: LiveSourceSpec(
        kind="rows",
        url=(
            "https://data.epa.gov/efservice/frs_program_facility"
            "/city_name/SAN FRANCISCO/rows/0:500/JSON"
        ),
        mapper=_epa_frs_mapper,
        # The Envirofacts REST service takes its window in the path, not in
        # query parameters, so there is nothing to page with.
        offset_param=None,
        limit_param=None,
    ),
    LIDAR: LiveSourceSpec(
        kind="point",
        url="https://epqs.nationalmap.gov/v1/json",
        mapper=_usgs_elevation_mapper,
        params={"units": "Meters", "wkid": "4326", "includeDate": "true"},
        lat_param="y",
        lon_param="x",
    ),
    SOLAR: LiveSourceSpec(
        kind="point",
        url="https://solar.googleapis.com/v1/buildingInsights:findClosest",
        mapper=_solar_mapper,
        params={"requiredQuality": "HIGH"},
        lat_param="location.latitude",
        lon_param="location.longitude",
        requires="maps_key",
    ),
    NREL: LiveSourceSpec(
        kind="point",
        url="https://developer.nrel.gov/api/alt-fuel-stations/v1/nearest.json",
        mapper=_nrel_mapper,
        params={"fuel_type": "ELEC", "radius": "0.5", "limit": "20"},
        lat_param="latitude",
        lon_param="longitude",
        requires="nrel_key",
    ),
    WEATHER: LiveSourceSpec(kind="weather"),
}


@dataclass(frozen=True, slots=True)
class LiveCredentials:
    """The keys the live feeds need, and the identity NWS asks callers for."""

    maps_api_key: str | None = None
    nrel_api_key: str | None = None
    socrata_app_token: str | None = None
    contact_email: str = "firstdue@example.org"

    def has(self, requirement: str) -> bool:
        if requirement == "maps_key":
            return bool(self.maps_api_key)
        if requirement == "nrel_key":
            return bool(self.nrel_api_key)
        return True

    @property
    def user_agent(self) -> str:
        """NWS rate-limits anonymous callers, and asks for a contact address."""
        return f"firstdue/1.0 ({self.contact_email})"


def build_fetcher(
    source_id: str,
    *,
    fixtures_dir: Path,
    live: bool,
    resolver: PointResolver | None = None,
    credentials: LiveCredentials | None = None,
) -> PageFetcher:
    """Choose the fetcher for one source.

    Live mode uses a real feed where one exists and reports ``UNCONFIGURED``
    where one does not. It never falls back to the fixture: a live-mode process
    serving synthetic records would be lying about where its data came from.
    """
    if not live:
        return FixtureFetcher(fixtures_dir / "san-francisco" / "sources" / FIXTURE_FILES[source_id])

    spec = LIVE_SOURCES.get(source_id)
    if spec is None:
        return UnconfiguredFetcher(
            note=UNCONFIGURED_REASONS.get(
                source_id, f"{source_id} has no reachable public endpoint in this build"
            )
        )

    creds = credentials or LiveCredentials()
    if not creds.has(spec.requires):
        return UnconfiguredFetcher(
            note=f"{source_id} needs a credential this process was not given ({spec.requires})"
        )

    if spec.kind == "weather":
        if resolver is None:
            return UnconfiguredFetcher(note=f"{source_id} needs an address resolver")
        return NwsPointFetcher(resolver=resolver, user_agent=creds.user_agent)

    if spec.kind == "point":
        if resolver is None:
            return UnconfiguredFetcher(note=f"{source_id} needs an address resolver")
        params = dict(spec.params)
        if spec.requires == "maps_key" and creds.maps_api_key:
            params["key"] = creds.maps_api_key
        if spec.requires == "nrel_key" and creds.nrel_api_key:
            params["api_key"] = creds.nrel_api_key
        return PointFetcher(
            url=spec.url,
            mapper=spec.mapper,
            resolver=resolver,
            params=params,
            lat_param=spec.lat_param,
            lon_param=spec.lon_param,
        )

    params = dict(spec.params)
    if creds.socrata_app_token and spec.url.startswith(SF_OPEN_DATA):
        # An app token lifts DataSF's anonymous throttle. It is not a secret in
        # the authorization sense -- it identifies the caller, it does not
        # authorize one -- but it still comes from configuration, never a file.
        params["$$app_token"] = creds.socrata_app_token
    return HttpFetcher(
        url=spec.url,
        mapper=spec.mapper,
        params=params,
        rows_path=spec.rows_path,
        since_param=spec.since_param,
        offset_param=spec.offset_param,
        limit_param=spec.limit_param,
    )


def build_sources(
    *,
    fixtures_dir: Path,
    clock: Clock,
    live: bool = False,
    city: CityAdapter | None = None,
    credentials: LiveCredentials | None = None,
) -> tuple[SourceAdapter, ...]:
    """Every configured source for San Francisco, in catalog order."""
    resolver = _city_resolver(city) if city is not None else None
    return tuple(
        ManagedSource(
            config,
            build_fetcher(
                config.source_id,
                fixtures_dir=fixtures_dir,
                live=live,
                resolver=resolver,
                credentials=credentials,
            ),
            clock=clock,
        )
        for config in CATALOG
    )


def _city_resolver(city: CityAdapter) -> PointResolver:
    """Address ids become coordinates only through the city adapter."""

    def resolve(address_id: str) -> tuple[float, float] | None:
        address = city.get_address(address_id)
        if address is None:
            return None
        return (address.latitude, address.longitude)

    return resolve


def sources_for(adapters: Sequence[SourceAdapter], *source_ids: str) -> tuple[SourceAdapter, ...]:
    """Select adapters by id, preserving the order asked for."""
    by_id = {adapter.source_id: adapter for adapter in adapters}
    return tuple(by_id[source_id] for source_id in source_ids if source_id in by_id)
