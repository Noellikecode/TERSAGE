"""Deterministic demo seed.

Same seed, same epoch, same bytes -- every time. ``make reset`` clears the state
directory and rebuilds it, and prints the content hash so determinism is a thing
you can check rather than a thing the README claims.

Everything seeded here is synthetic. The street addresses are real public
reference data; every permit number, inspection narrative, hazmat filing, and
survey record attached to them is invented for this demo.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from firstdue.adapters.clock import DeterministicIdGenerator
from firstdue.domain.conflict_engine import detect
from firstdue.domain.enums import (
    AssertionStatus,
    Classification,
    FaceLabel,
    SourceType,
)
from firstdue.domain.facts import SourceSpan, StructuralFact
from firstdue.domain.geometry import (
    Face,
    GeometrySpec,
    Level,
    Obstruction,
    ObstructionType,
    RoofSegment,
    collapse_zone_radius,
)
from firstdue.domain.keys import Keys
from firstdue.domain.profiles import BuildingProfile, ProfileEvent, ProfileEventType
from firstdue.domain.values import BooleanValue, EnumValue, IntegerValue, UnknownValue
from firstdue.ports.city import NormalizedAddress

SEED_FILE = "seed.json"
SEED_VERSION = 1

#: One address carries the unpermitted-third-floor disagreement the whole
#: product is built around. Named here so the demo is reproducible.
DISPUTED_ADDRESS_ID = "sf-0450-hayes"
#: One address carries a lightweight-truss floor system.
TRUSS_ADDRESS_ID = "sf-1215-fell"
#: One address has no records at all, so the cold-start path is demonstrable.
COLD_START_ADDRESS_ID = "sf-3120-24th"


def _fact(
    *,
    ids: DeterministicIdGenerator,
    address_id: str,
    key: str,
    value: Any,
    source_type: SourceType,
    source_ref: str,
    observed_at: datetime,
    ingested_at: datetime,
    confidence: float,
    classification: Classification = Classification.PUBLIC,
    span: SourceSpan | None = None,
) -> StructuralFact:
    return StructuralFact(
        fact_id=ids.new_id("fact"),
        address_id=address_id,
        canonical_key=key,
        value=value,
        source_type=source_type,
        source_ref=source_ref,
        source_snapshot_id=f"seed:{source_ref}",
        source_span=span,
        observed_at=observed_at,
        ingested_at=ingested_at,
        confidence=confidence,
        classification=classification,
    )


def _event(
    *,
    ids: DeterministicIdGenerator,
    sequence: int,
    occurred_at: datetime,
    event_type: ProfileEventType,
    summary: str,
    keys: tuple[str, ...] = (),
    fact_ids: tuple[str, ...] = (),
    conflict_id: str | None = None,
) -> ProfileEvent:
    return ProfileEvent(
        event_id=ids.new_id("evt"),
        sequence=sequence,
        occurred_at=occurred_at,
        type=event_type,
        actor="seed",
        actor_version=str(SEED_VERSION),
        summary=summary,
        canonical_keys=keys,
        fact_ids=fact_ids,
        conflict_id=conflict_id,
    )


def _build_profile(
    *,
    ids: DeterministicIdGenerator,
    address: NormalizedAddress,
    epoch: datetime,
) -> BuildingProfile:
    """Build one address's accumulated profile."""
    profile = BuildingProfile(
        address_id=address.address_id,
        district_id=address.district_id,
        hydrant_ids=(f"HYD-{address.address_id[-4:]}-A", f"HYD-{address.address_id[-4:]}-B"),
    )

    if address.address_id == COLD_START_ADDRESS_ID:
        # Deliberately empty: new construction, nothing on record. The brief
        # must say so rather than implying the structure is unremarkable.
        return profile

    permit_observed = epoch - timedelta(days=2870)
    assessor_observed = epoch - timedelta(days=1460)
    lidar_observed = epoch - timedelta(days=410)
    inspection_observed = epoch - timedelta(days=505)

    permit_stories = _fact(
        ids=ids,
        address_id=address.address_id,
        key=Keys.STORIES,
        value=IntegerValue(integer=2),
        source_type=SourceType.PERMIT,
        source_ref=f"permit/{address.address_id}/2018-04871",
        observed_at=permit_observed,
        ingested_at=epoch - timedelta(days=900),
        confidence=0.92,
        span=SourceSpan(
            locator=f"permit/{address.address_id}/2018-04871#p1",
            start_offset=41,
            end_offset=42,
            quoted_text="2",
        ),
    )
    profile = profile.with_fact(
        permit_stories,
        event=_event(
            ids=ids,
            sequence=profile.next_sequence,
            occurred_at=permit_stories.ingested_at,
            event_type=ProfileEventType.FACT_WRITTEN,
            summary="Permit filing recorded storey count",
            keys=(Keys.STORIES,),
            fact_ids=(permit_stories.fact_id,),
        ),
    )

    construction = _fact(
        ids=ids,
        address_id=address.address_id,
        key=Keys.CONSTRUCTION_TYPE,
        value=EnumValue(term="wood-frame", vocabulary="iso-construction"),
        source_type=SourceType.ASSESSOR,
        source_ref=f"assessor/{address.parcel_ref or address.address_id}",
        observed_at=assessor_observed,
        ingested_at=epoch - timedelta(days=800),
        confidence=0.88,
    )
    profile = profile.with_fact(
        construction,
        event=_event(
            ids=ids,
            sequence=profile.next_sequence,
            occurred_at=construction.ingested_at,
            event_type=ProfileEventType.FACT_WRITTEN,
            summary="Assessor roll recorded construction type",
            keys=(Keys.CONSTRUCTION_TYPE,),
            fact_ids=(construction.fact_id,),
        ),
    )

    # No sprinkler filing on record. UNKNOWN, explicitly -- not "no sprinklers".
    sprinklered = _fact(
        ids=ids,
        address_id=address.address_id,
        key=Keys.SUPPRESSION_SPRINKLERED,
        value=UnknownValue(checked_sources=("sf-permits", "sf-fire-inspections")),
        source_type=SourceType.FIRE_INSPECTION,
        source_ref=f"inspection/{address.address_id}/2025-0117",
        observed_at=inspection_observed,
        ingested_at=epoch - timedelta(days=500),
        confidence=0.55,
    )
    profile = profile.with_fact(
        sprinklered,
        event=_event(
            ids=ids,
            sequence=profile.next_sequence,
            occurred_at=sprinklered.ingested_at,
            event_type=ProfileEventType.FACT_WRITTEN,
            summary="No suppression filing found; recorded as UNKNOWN",
            keys=(Keys.SUPPRESSION_SPRINKLERED,),
            fact_ids=(sprinklered.fact_id,),
        ),
    )

    if address.address_id == TRUSS_ADDRESS_ID:
        truss = _fact(
            ids=ids,
            address_id=address.address_id,
            key=Keys.LIGHTWEIGHT_TRUSS,
            value=BooleanValue(boolean=True),
            source_type=SourceType.FIRE_INSPECTION,
            source_ref=f"inspection/{address.address_id}/2024-0904",
            observed_at=epoch - timedelta(days=690),
            ingested_at=epoch - timedelta(days=688),
            confidence=0.9,
            span=SourceSpan(
                locator=f"inspection/{address.address_id}/2024-0904#narrative",
                start_offset=12,
                end_offset=41,
                quoted_text="lightweight parallel-chord truss",
            ),
        )
        profile = profile.with_fact(
            truss,
            event=_event(
                ids=ids,
                sequence=profile.next_sequence,
                occurred_at=truss.ingested_at,
                event_type=ProfileEventType.FACT_WRITTEN,
                summary="Inspection narrative recorded lightweight truss floor system",
                keys=(Keys.LIGHTWEIGHT_TRUSS,),
                fact_ids=(truss.fact_id,),
            ),
        )

    levels = [
        Level(
            height_m=3.4,
            provenance=SourceType.PERMIT,
            status=AssertionStatus.CONFIRMED,
            fact_id=permit_stories.fact_id,
        ),
        Level(
            height_m=3.2,
            provenance=SourceType.PERMIT,
            status=AssertionStatus.CONFIRMED,
            fact_id=permit_stories.fact_id,
        ),
    ]

    if address.address_id == DISPUTED_ADDRESS_ID:
        # The permit says two storeys; the lidar measures three. Both facts stay
        # stored, and the disagreement is the finding.
        lidar_stories = _fact(
            ids=ids,
            address_id=address.address_id,
            key=Keys.STORIES,
            value=IntegerValue(integer=3),
            source_type=SourceType.LIDAR_DSM,
            source_ref="usgs-3dep/dsm/2025-q2",
            observed_at=lidar_observed,
            ingested_at=epoch - timedelta(days=405),
            confidence=0.81,
        )
        profile = profile.with_fact(
            lidar_stories,
            event=_event(
                ids=ids,
                sequence=profile.next_sequence,
                occurred_at=lidar_stories.ingested_at,
                event_type=ProfileEventType.FACT_WRITTEN,
                summary="Lidar DSM measured storey count",
                keys=(Keys.STORIES,),
                fact_ids=(lidar_stories.fact_id,),
            ),
        )

        # The conflict is produced by the engine that runs in production, not
        # written by hand here. If a rule changes, the demo changes with it --
        # which is the point of seeding through the real code path.
        detected_at = epoch - timedelta(days=404)
        findings = detect(address.address_id, profile.all_facts(), now=detected_at)
        story_findings = [f for f in findings if f.canonical_key == Keys.STORIES]
        if len(story_findings) != 1:
            raise ValueError(
                "the seeded disputed address must produce exactly one storey conflict; "
                f"the engine produced {len(story_findings)}"
            )
        conflict = story_findings[0].to_conflict(detected_at=detected_at)
        profile = profile.with_conflict(
            conflict,
            event=_event(
                ids=ids,
                sequence=profile.next_sequence,
                occurred_at=conflict.detected_at,
                event_type=ProfileEventType.CONFLICT_DETECTED,
                summary=conflict.summary,
                keys=(Keys.STORIES,),
                fact_ids=conflict.fact_ids,
                conflict_id=conflict.conflict_id,
            ),
        )
        levels.append(
            Level(
                height_m=2.9,
                provenance=SourceType.LIDAR_DSM,
                status=AssertionStatus.DISPUTED,
                fact_id=lidar_stories.fact_id,
            )
        )

    total_height = sum(level.height_m for level in levels)
    # The last flight, and it predates the permit that disputes it.
    #
    # This used to be `epoch - 400 days`, five days *after* the newest
    # geometry-invalidating fact -- so `geometry_is_stale` was false for every
    # seeded profile and `geometry-watcher` skipped the whole district on every
    # pass. The massing model on screen was this literal, and the agent that is
    # supposed to measure a building never ran in the demo at all.
    #
    # 420 days puts it before the 2025-07 permit, which is the sequence the
    # staleness rule exists for: a permit filed after the last flight describes
    # work the flight could not have seen, so the spec is re-derived. The demo
    # still opens with a model; the first pass replaces it with a measured one.
    geometry = GeometrySpec(
        address_id=address.address_id,
        generated_at=epoch - timedelta(days=420),
        footprint=((0.0, 0.0), (11.5, 0.0), (11.5, 22.0), (0.0, 22.0)),
        levels=tuple(levels),
        roof_segments=(
            RoofSegment(pitch_deg=18.0, azimuth_deg=210.0, area_m2=126.5),
            RoofSegment(pitch_deg=18.0, azimuth_deg=30.0, area_m2=126.5),
        ),
        obstructions=(
            (
                Obstruction(
                    type=ObstructionType.SOLAR_ARRAY,
                    segment_index=0,
                    provenance=SourceType.SOLAR_API,
                ),
            )
            if address.address_id in (DISPUTED_ADDRESS_ID, TRUSS_ADDRESS_ID)
            else ()
        ),
        faces=tuple(
            Face(label=label)
            for label in (FaceLabel.ALPHA, FaceLabel.BRAVO, FaceLabel.CHARLIE, FaceLabel.DELTA)
        ),
        collapse_zone_radius_m=collapse_zone_radius(total_height),
    )
    profile = profile.with_geometry(
        geometry,
        event=_event(
            ids=ids,
            sequence=profile.next_sequence,
            occurred_at=geometry.generated_at,
            event_type=ProfileEventType.GEOMETRY_UPDATED,
            summary="Geometry derived from Solar API roof segments and lidar DSM",
        ),
    )
    return profile


#: Addresses whose profile is built by the slow loop rather than seeded.
#:
#: ``_build_profile`` writes one template -- a two-storey wood-frame dwelling --
#: and that template is wrong for a 61-storey steel-frame tower in a way that
#: does not stay quiet: the seeded baseline disagreed with the records the slow
#: loop then read, and the profile carried two invented conflicts about
#: construction type and storey count. A synthetic baseline that argues with
#: real filings is worse than no baseline, so this address has none and every
#: fact on it comes from a source record.
RECORDS_ONLY: Final[frozenset[str]] = frozenset({"sf-0415-mission"})


def build_seed(
    *,
    addresses: list[NormalizedAddress],
    epoch: datetime,
    seed: str,
) -> dict[str, Any]:
    """Build the full deterministic seed document."""
    ids = DeterministicIdGenerator(seed)
    profiles = [
        _build_profile(ids=ids, address=address, epoch=epoch)
        for address in sorted(addresses, key=lambda a: a.address_id)
        if address.address_id not in RECORDS_ONLY
    ]
    document: dict[str, Any] = {
        "seed_version": SEED_VERSION,
        "seed": seed,
        "epoch": epoch.isoformat(),
        "synthetic": True,
        "profiles": [p.model_dump(mode="json") for p in profiles],
    }
    document["content_hash"] = content_hash(document)
    return document


def content_hash(document: dict[str, Any]) -> str:
    """Stable hash over everything except the hash field itself."""
    payload = {k: v for k, v in document.items() if k != "content_hash"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_seed(document: dict[str, Any], state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / SEED_FILE
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_seed(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir / SEED_FILE
    if not path.is_file():
        return None
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def profiles_from_seed(document: dict[str, Any]) -> list[BuildingProfile]:
    """Rehydrate profiles, validating every invariant on the way in."""
    return [BuildingProfile.model_validate(raw) for raw in document.get("profiles", [])]
