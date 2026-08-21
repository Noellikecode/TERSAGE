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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from firstdue.domain.enums import Classification, SourceType
from firstdue.ports.clock import Clock
from firstdue.ports.sources import SourceAdapter, SourceRecord
from firstdue.sources.fetchers import FixtureFetcher, HttpFetcher
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
}


def _parse_observed(raw: Any) -> datetime:
    return datetime.fromisoformat(str(raw))


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
        observed_at=_parse_observed(row.get("filed_date") or row.get("issued_date")),
    )


#: Live endpoints for the sources that have a reachable public feed.
LIVE_ENDPOINTS: Final[dict[str, dict[str, Any]]] = {
    PERMITS: {
        "url": f"{SF_OPEN_DATA}/i98e-djp9.json",
        "mapper": _sf_permit_mapper,
        "since_param": "$where",
    },
}


def build_fetcher(source_id: str, *, fixtures_dir: Path, live: bool) -> PageFetcher:
    """Choose the fetcher for one source.

    Live mode uses a real feed where one exists and reports ``UNCONFIGURED``
    where one does not. It never falls back to the fixture: a live-mode process
    serving synthetic records would be lying about where its data came from.
    """
    if not live:
        return FixtureFetcher(fixtures_dir / "san-francisco" / "sources" / FIXTURE_FILES[source_id])

    endpoint = LIVE_ENDPOINTS.get(source_id)
    if endpoint is None:
        return UnconfiguredFetcher(
            note=f"{source_id} has no reachable public endpoint in this build"
        )
    return HttpFetcher(**endpoint)


def build_sources(
    *, fixtures_dir: Path, clock: Clock, live: bool = False
) -> tuple[SourceAdapter, ...]:
    """Every configured source for San Francisco, in catalog order."""
    return tuple(
        ManagedSource(
            config,
            build_fetcher(config.source_id, fixtures_dir=fixtures_dir, live=live),
            clock=clock,
        )
        for config in CATALOG
    )


def sources_for(adapters: Sequence[SourceAdapter], *source_ids: str) -> tuple[SourceAdapter, ...]:
    """Select adapters by id, preserving the order asked for."""
    by_id = {adapter.source_id: adapter for adapter in adapters}
    return tuple(by_id[source_id] for source_id in source_ids if source_id in by_id)
