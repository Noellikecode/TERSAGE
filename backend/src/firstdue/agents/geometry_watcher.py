"""Geometry Watcher -- what the building measures, not what it filed.

Three sources, three different kinds of evidence:

* the **parcel** gives the footprint the building sits on;
* the **Solar API** gives roof segments, pitch, and whether there is an array a
  crew cannot cut through;
* **USGS 3DEP** gives a digital surface model -- the actual measured height.

Height becomes a storey count by division, and that is where the product's
central disagreement comes from: the permit says two, the lidar measures 9.5 m,
and 9.5 m is three storeys. Both facts are written. Neither is corrected. The
conflict engine does the rest.

**Permit-driven invalidation.** Geometry derived before a permit was filed
describes a building that may no longer exist, so a fact on any
geometry-invalidating key that post-dates the spec marks it stale and forces a
re-derivation.

Everything emitted is deterministic and provenanced, including the SVG fallback
data -- the disputed storey arrives marked ``DISPUTED``, so even a static
renderer shows it as disputed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import AssertionStatus, Classification, FaceLabel, SourceType
from firstdue.domain.facts import StructuralFact, natural_fact_id
from firstdue.domain.geometry import (
    Face,
    GeometrySpec,
    Level,
    Obstruction,
    ObstructionType,
    Point2D,
    RoofSegment,
    collapse_zone_radius,
)
from firstdue.domain.keys import GEOMETRY_INVALIDATING_KEYS, Keys
from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType
from firstdue.domain.values import BooleanValue, IntegerValue, QuantityValue
from firstdue.errors import AppendOnlyViolationError, SourceUnavailableError, StaleVersionError
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock
from firstdue.ports.repositories import FactRepository, ProfileRepository
from firstdue.ports.sources import SourceAdapter, SourceRecord
from firstdue.services.materialization import ProfileMaterializer
from firstdue.sources.catalog import LIDAR, PARCELS, SOLAR

logger = get_logger(__name__)

AGENT_ID: Final[str] = "geometry-watcher"

#: Storey height used to turn a measured height into a storey count. A
#: published residential convention, applied to a measurement -- it predicts
#: nothing and it is stated so an officer can check the arithmetic.
TYPICAL_STOREY_M: Final[float] = 3.2
#: Below this, a measurement is noise rather than a storey.
MIN_STRUCTURE_HEIGHT_M: Final[float] = 2.0

DEFAULT_FOOTPRINT: Final[tuple[Point2D, ...]] = (
    (0.0, 0.0),
    (11.5, 0.0),
    (11.5, 22.0),
    (0.0, 22.0),
)

#: The default's proportions, kept when only an area is known.
_DEFAULT_ASPECT: Final[float] = 22.0 / 11.5


def _footprint_of_area(area_m2: object, fallback: tuple[Point2D, ...]) -> tuple[Point2D, ...]:
    """A rectangle of a measured ground area, in the default's proportions.

    Used only where no parcel ring is available. It carries the building's
    *size* and makes no claim about its shape: a roof measured at 398 m2 renders
    as 398 m2 rather than as the constant every structure used to share.

    An unusable or absent area returns the fallback rather than a guess.
    """
    try:
        area = float(str(area_m2))
    except (TypeError, ValueError):
        return fallback
    if not area > 0:
        return fallback
    width = math.sqrt(area / _DEFAULT_ASPECT)
    depth = area / width
    return ((0.0, 0.0), (width, 0.0), (width, depth), (0.0, depth))


@dataclass(frozen=True, slots=True)
class MeasuredHeight:
    """A height above ground, and every reading it rests on.

    Two shapes produce one of these. A digital surface model reports height
    above ground directly. The live pairing does not: Google Solar reports each
    roof plane in the same vertical datum USGS reports the ground in, so the
    height is a *subtraction* -- and a subtraction that cites only one of its
    two operands is a number nobody can check.
    """

    height_m: float
    #: The record the fact is attributed to: the one that measured the roof.
    primary: SourceRecord
    #: Every record the number rests on, in citation order.
    citations: tuple[str, ...]
    method: str

    @property
    def source_ref(self) -> str:
        return " + ".join(self.citations)


def measured_height(
    lidar: SourceRecord | None, solar: SourceRecord | None
) -> MeasuredHeight | None:
    """Height above ground from whichever measurement pair is available.

    Returns ``None`` when neither shape is present. That becomes an absent
    height, which renders as ``UNKNOWN`` -- never as a building of height zero,
    which would be a one-storey structure the collapse zone was computed from.
    """
    if lidar is not None:
        # A digital surface model already answers "how tall above the ground".
        direct = lidar.fields.get("dsm_height_m")
        if direct is not None:
            datum = float(lidar.fields.get("dtm_height_m") or 0.0)
            return MeasuredHeight(
                height_m=float(direct) - datum,
                primary=lidar,
                citations=(lidar.record_ref,),
                method="dsm-minus-dtm",
            )

    if solar is None or lidar is None:
        return None
    plane = solar.fields.get("max_plane_height_m")
    ground = lidar.fields.get("ground_elevation_m")
    if plane is None or ground is None:
        return None

    height = float(plane) - float(ground)
    if height < MIN_STRUCTURE_HEIGHT_M:
        # A roof plane below the ground it sits on means the two readings are
        # not in the same datum, or the API answered about a different
        # building. Either way the honest output is no height at all.
        logger.warning(
            "geometry_height_rejected",
            extra={"reason": "implausible_difference", "method": "solar-minus-3dep"},
        )
        return None
    return MeasuredHeight(
        height_m=height,
        primary=solar,
        citations=(solar.record_ref, lidar.record_ref),
        method="solar-plane-minus-3dep-ground",
    )


def stories_from_height(height_m: float, *, storey_m: float = TYPICAL_STOREY_M) -> int:
    """Deterministic storey count from a measured height.

    Rounds to nearest, floors at one. Same input, same answer, forever -- which
    is what lets the resulting disagreement with a permit be a finding rather
    than an artefact of how the number was computed today.
    """
    if height_m < MIN_STRUCTURE_HEIGHT_M:
        return 1
    return max(1, round(height_m / storey_m))


class GeometryWatchResult(BaseModel):
    """What one geometry pass produced for one district."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    district_id: str
    addresses_updated: tuple[str, ...] = ()
    facts_written: int = Field(default=0, ge=0)
    #: The ids of the facts this pass appended, for the run record.
    written_fact_ids: tuple[str, ...] = ()
    #: Addresses whose stored geometry was invalidated by a newer permit.
    invalidated: tuple[str, ...] = ()
    conflicts_detected: tuple[str, ...] = ()
    unavailable_sources: tuple[str, ...] = ()


def geometry_is_stale(profile: BuildingProfile) -> bool:
    """True when a geometry-invalidating fact post-dates the stored spec.

    A permit pulled after the last flight describes work the flight could not
    have seen. Rather than trusting old geometry, the spec is re-derived.
    """
    if profile.geometry is None:
        return True
    generated_at = profile.geometry.generated_at
    for key, fact_set in profile.fact_sets.items():
        if key not in GEOMETRY_INVALIDATING_KEYS:
            continue
        if any(fact.ingested_at > generated_at for fact in fact_set.facts):
            return True
    return False


class GeometryWatcher:
    """Derives measured geometry and the facts that follow from it."""

    def __init__(
        self,
        *,
        profiles: ProfileRepository,
        facts: FactRepository,
        materializer: ProfileMaterializer,
        clock: Clock,
        agent_version: str = "1.0.0",
    ) -> None:
        self._profiles = profiles
        self._facts = facts
        self._materializer = materializer
        self._clock = clock
        self._agent_version = agent_version

    async def poll(
        self,
        *,
        district_id: str,
        sources: Sequence[SourceAdapter],
        correlation_id: str,
        address_ids: Sequence[str] | None = None,
    ) -> GeometryWatchResult:
        by_source = {s.source_id: s for s in sources}
        unavailable: list[str] = []
        records: dict[str, dict[str, SourceRecord]] = {}

        # The parcel sweep first, and only the parcel sweep. It is the source
        # that answers about a whole district at once, so it is what decides
        # which addresses there are geometry to derive for.
        parcels = by_source.get(PARCELS)
        if parcels is not None:
            try:
                snapshot = await parcels.fetch()
            except SourceUnavailableError as exc:
                logger.warning(
                    "geometry_source_unavailable",
                    extra={"source_id": PARCELS, "error_code": str(exc.code)},
                )
                unavailable.append(PARCELS)
            else:
                for record in snapshot.records:
                    if record.address_id is None:
                        continue
                    records.setdefault(record.address_id, {})[PARCELS] = record

        # The department's own profiles are the list of structures, not whatever
        # a source happened to attribute. The live parcel feed returns rows
        # keyed by block-and-lot with no address id at all, so a sweep-derived
        # target list is empty against real data and full against a fixture --
        # which is the second half of why this agent measured nothing live.
        if address_ids:
            targets = sorted(address_ids)
        else:
            targets = sorted(
                p.address_id for p in await self._profiles.list_by_district(district_id)
            )

        # Solar and 3DEP answer about **one address**, and asking them about a
        # district raises `address_required` before a request is even made.
        # Sweeping all three together worked only because a fixture returns
        # every record whatever it is asked, so this agent produced measured
        # geometry in fake mode and none at all against the live feeds -- a
        # default footprint and a "measured height" nobody measured.
        for address_id in targets:
            for source_id in (SOLAR, LIDAR):
                source = by_source.get(source_id)
                if source is None:
                    continue
                try:
                    snapshot = await source.fetch(address_id=address_id)
                except SourceUnavailableError as exc:
                    # Per address, because one building outside coverage must
                    # not mark the source down for the rest of the district.
                    logger.warning(
                        "geometry_source_unavailable",
                        extra={
                            "source_id": source_id,
                            "address_id": address_id,
                            "error_code": str(exc.code),
                        },
                    )
                    if source_id not in unavailable:
                        unavailable.append(source_id)
                    continue
                for record in snapshot.records:
                    # A point source answers about the address it was asked
                    # about; the mapper has no address to attribute it to.
                    records.setdefault(address_id, {})[source_id] = record
        updated: list[str] = []
        invalidated: list[str] = []
        conflicts: list[str] = []
        written = 0

        for address_id in targets:
            profile = await self._profiles.get(address_id)
            if profile is None or profile.district_id != district_id:
                continue
            was_stale = geometry_is_stale(profile)
            if not was_stale:
                continue
            if profile.geometry is not None:
                invalidated.append(address_id)

            count = await self._derive(profile, records[address_id])
            if count == 0:
                continue
            written += count
            updated.append(address_id)
            outcome = await self._materializer.run(
                address_id,
                owner=f"{AGENT_ID}:{district_id}",
                correlation_id=correlation_id,
            )
            conflicts.extend(outcome.new_conflict_ids)

        logger.info(
            "geometry_watcher_pass",
            extra={
                "district_id": district_id,
                "updated": len(updated),
                "invalidated": len(invalidated),
                "unavailable": len(unavailable),
            },
        )
        return GeometryWatchResult(
            district_id=district_id,
            addresses_updated=tuple(updated),
            facts_written=written,
            invalidated=tuple(invalidated),
            conflicts_detected=tuple(conflicts),
            unavailable_sources=tuple(unavailable),
        )

    # ------------------------------------------------------------ internals

    async def _derive(self, profile: BuildingProfile, by_source: dict[str, SourceRecord]) -> int:
        """Write measurement facts and the spec they support."""
        now = self._clock.now()
        lidar = by_source.get(LIDAR)
        solar = by_source.get(SOLAR)
        parcel = by_source.get(PARCELS)

        measured_facts: list[StructuralFact] = []
        height_m: float | None = None

        measurement = measured_height(lidar, solar)
        if measurement is not None:
            height_m = measurement.height_m
            # A height that two instruments were subtracted to produce is worth
            # marginally less than one a surface model measured outright, and
            # the storey count derived from it is worth less again. The
            # confidence says so rather than presenting both as equal.
            derived = len(measurement.citations) > 1
            measured_facts.append(
                self._fact(
                    profile.address_id,
                    Keys.HEIGHT_M,
                    QuantityValue(magnitude=round(height_m, 2), unit="m"),
                    record=measurement.primary,
                    source_type=SourceType.LIDAR_DSM,
                    confidence=0.80 if derived else 0.85,
                    now=now,
                    source_ref=measurement.source_ref,
                )
            )
            measured_facts.append(
                self._fact(
                    profile.address_id,
                    Keys.STORIES,
                    IntegerValue(integer=stories_from_height(height_m)),
                    record=measurement.primary,
                    source_type=SourceType.LIDAR_DSM,
                    confidence=0.76 if derived else 0.81,
                    now=now,
                    source_ref=measurement.source_ref,
                )
            )

        if solar is not None:
            measured_facts.append(
                self._fact(
                    profile.address_id,
                    Keys.HAZARD_SOLAR_ARRAY,
                    BooleanValue(boolean=bool(solar.fields.get("solar_array_present"))),
                    record=solar,
                    source_type=SourceType.SOLAR_API,
                    confidence=0.88,
                    now=now,
                )
            )

        stored = 0
        updated = profile
        for fact in measured_facts:
            try:
                await self._facts.append(fact)
            except AppendOnlyViolationError:
                continue
            try:
                updated = updated.with_fact(
                    fact,
                    event=ProfileEvent(
                        event_id=f"pevt_{fact.fact_id.removeprefix('fact_')}",
                        sequence=updated.next_sequence,
                        occurred_at=now,
                        type=ProfileEventType.FACT_WRITTEN,
                        actor=AGENT_ID,
                        actor_version=self._agent_version,
                        summary=f"Measured {fact.canonical_key} from {fact.source_type}",
                        canonical_keys=(fact.canonical_key,),
                        fact_ids=(fact.fact_id,),
                    ),
                )
            except AppendOnlyViolationError:
                continue
            stored += 1

        spec = self._spec(updated, parcel=parcel, solar=solar, height_m=height_m, now=now)
        updated = updated.with_geometry(
            spec,
            event=ProfileEvent(
                event_id=f"pevt_geom_{spec.address_id}_{updated.next_sequence}",
                sequence=updated.next_sequence,
                occurred_at=now,
                type=ProfileEventType.GEOMETRY_UPDATED,
                actor=AGENT_ID,
                actor_version=self._agent_version,
                summary="Geometry derived from parcel footprint, roof segments, and lidar DSM",
            ),
        )

        try:
            await self._profiles.save(updated, expected_version=profile.profile_version)
        except StaleVersionError:
            logger.info("geometry_write_contended", extra={"address_id": profile.address_id})
            return 0
        return stored

    def _spec(
        self,
        profile: BuildingProfile,
        *,
        parcel: SourceRecord | None,
        solar: SourceRecord | None,
        height_m: float | None,
        now: datetime,
    ) -> GeometrySpec:
        """Build the renderable spec, marking disputed mass as disputed."""
        footprint = DEFAULT_FOOTPRINT
        if parcel is not None:
            raw = parcel.fields.get("footprint")
            if isinstance(raw, list) and len(raw) >= 3:
                footprint = tuple((float(p[0]), float(p[1])) for p in raw)
        elif solar is not None:
            # No parcel ring for this address, but Solar measured the roof's
            # ground area. A rectangle of that area is not the building's
            # *shape* and is not offered as one -- it is the right size, which
            # is what the collapse zone and the massing render read. The
            # alternative is `DEFAULT_FOOTPRINT`, a constant 11.5 x 22 m that
            # every structure in the district shared and that no source ever
            # measured.
            footprint = _footprint_of_area(solar.fields.get("roof_area_m2"), footprint)

        segments: list[RoofSegment] = []
        obstructions: list[Obstruction] = []
        if solar is not None:
            for entry in solar.fields.get("roof_segments", []) or []:
                segments.append(
                    RoofSegment(
                        pitch_deg=float(entry["pitch_deg"]),
                        azimuth_deg=float(entry["azimuth_deg"]),
                        area_m2=float(entry["area_m2"]) if entry.get("area_m2") else None,
                    )
                )
            if solar.fields.get("solar_array_present") and segments:
                obstructions.append(
                    Obstruction(
                        type=ObstructionType.SOLAR_ARRAY,
                        segment_index=0,
                        provenance=SourceType.SOLAR_API,
                    )
                )

        levels = self._levels(profile, height_m)
        total_height = sum(level.height_m for level in levels)
        return GeometrySpec(
            address_id=profile.address_id,
            generated_at=now,
            footprint=footprint,
            levels=tuple(levels),
            roof_segments=tuple(segments),
            obstructions=tuple(obstructions),
            faces=tuple(
                Face(label=label)
                for label in (
                    FaceLabel.ALPHA,
                    FaceLabel.BRAVO,
                    FaceLabel.CHARLIE,
                    FaceLabel.DELTA,
                )
            ),
            collapse_zone_radius_m=collapse_zone_radius(total_height),
        )

    def _levels(self, profile: BuildingProfile, height_m: float | None) -> list[Level]:
        """One level per storey, with the disputed ones marked.

        The filed count and the measured count are both represented: levels up
        to the filed count are confirmed, and any measured storey beyond it is
        ``DISPUTED``. The conflict lives in the data, not in the renderer.
        """
        fact_set = profile.fact_sets.get(Keys.STORIES)
        filed = 0
        filed_fact_id: str | None = None
        measured = stories_from_height(height_m) if height_m else 0
        measured_fact_id: str | None = None

        if fact_set is not None:
            for fact in fact_set.active:
                if not fact.is_known:
                    continue
                if fact.source_type is SourceType.PERMIT and int(fact.value.unwrap()) > filed:
                    filed = int(fact.value.unwrap())
                    filed_fact_id = fact.fact_id
                if fact.source_type is SourceType.LIDAR_DSM:
                    measured = max(measured, int(fact.value.unwrap()))
                    measured_fact_id = fact.fact_id

        total = max(filed, measured, 1)
        levels: list[Level] = []
        for index in range(total):
            confirmed = index < filed
            levels.append(
                Level(
                    height_m=round((height_m / total) if height_m else TYPICAL_STOREY_M, 2),
                    provenance=SourceType.PERMIT if confirmed else SourceType.LIDAR_DSM,
                    status=AssertionStatus.CONFIRMED if confirmed else AssertionStatus.DISPUTED,
                    fact_id=filed_fact_id if confirmed else measured_fact_id,
                )
            )
        return levels

    def _fact(
        self,
        address_id: str,
        key: str,
        value: Any,
        *,
        record: SourceRecord,
        source_type: SourceType,
        confidence: float,
        now: datetime,
        source_ref: str | None = None,
    ) -> StructuralFact:
        # A fact derived from more than one reading cites all of them, so an
        # officer who doubts the height can find both numbers it came from.
        cited = source_ref or record.record_ref
        return StructuralFact(
            fact_id=natural_fact_id(
                address_id=address_id,
                canonical_key=key,
                source_ref=cited,
                observed_at=record.observed_at,
                rendered_value=value.render(),
            ),
            address_id=address_id,
            canonical_key=key,
            value=value,
            source_type=source_type,
            source_ref=cited,
            source_snapshot_id=record.record_ref,
            observed_at=record.observed_at,
            ingested_at=now,
            confidence=confidence,
            classification=record.classification or Classification.PUBLIC,
            produced_by_agent=AGENT_ID,
            produced_by_version=self._agent_version,
        )
