"""Generating a week of a municipality's records, deterministically.

What comes out is **input, not answers**: raw records carrying the prose a
clerk, an inspector or an applicant actually typed. A permit says
``CONVERT ATTIC TO HABITABLE SPACE; ADD SHED DORMER`` -- it does not say
``structure.stories = 3``. Every fact the fleet ends up holding still has to be
extracted through the screens, the triage, the span binding and the provenance
rules, which is the whole point of feeding the agents a corpus rather than a
finished profile.

Three properties are load-bearing.

**Deterministic.** One seed, one corpus, forever. The generator takes its epoch
as an argument and never reads a clock, and every random choice comes from a
seeded stream keyed on the address, so re-running produces byte-identical
records and a replay two years later reconstructs the same district. The same
discipline ``demo/seed.py`` already applies, for the same reason.

**Disagreements are planted, not manufactured later.** A handful of addresses
get a permit whose filed storey count is one *below* what the remote-measurement
sources will report. Nothing here writes a conflict: the records simply
disagree, the way municipal records really do, and the deterministic conflict
engine finds it downstream or the demo has nothing to show. Planting the
disagreement in the *source data* rather than in the profile is what makes the
detection real.

**Marked as generated, in the record.** Every document carries
``synthetic: true`` and the corpus version. A source's ``SourceMode`` reports
``FIXTURE`` when it is served from here, so the console says so on screen. A
hidden simulation is worse than an admitted one.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from firstdue.domain.enums import Classification
from firstdue.ports.city import NormalizedAddress
from firstdue.ports.sources import SourceRecord

#: Bumped when the shape of a generated record changes. Stored on every
#: document so a loaded corpus can be told from an older one without guessing.
CORPUS_VERSION: Final[str] = "1.0.0"

#: The department's own stores, and what each holds. Keyed by collection name
#: so the loader, the fetcher and the Firestore index policy read one list.
CENTRAL_COLLECTIONS: Final[dict[str, str]] = {
    "central_permits": "sf-permits",
    "central_assessor": "sf-assessor",
    "central_inspections": "sf-fire-inspections",
    "central_violations": "sf-violations",
    "central_hazmat": "tier-ii-confidential",
}

#: Records with no source behind them. These are the department describing
#: *itself* rather than a building -- read by the incident loop for prior
#: history and by the console for who is available, never extracted into
#: structural facts.
CENTRAL_REFERENCE_COLLECTIONS: Final[tuple[str, ...]] = (
    "central_incidents",
    "central_personnel",
)

_SOURCE_TO_COLLECTION: Final[dict[str, str]] = {
    source_id: collection for collection, source_id in CENTRAL_COLLECTIONS.items()
}


def collection_for_source(source_id: str) -> str | None:
    """Which central collection backs a source id, if any."""
    return _SOURCE_TO_COLLECTION.get(source_id)


# --------------------------------------------------------------- vocabulary
#
# Kept small and real. These are the phrases that actually appear on San
# Francisco filings, because the extraction path is being asked to read them --
# inventing florid prose would be testing the generator, not the extractor.

_PERMIT_WORK: Final[tuple[tuple[str, str], ...]] = (
    ("ALTERATION", "REROOF; TEAR OFF EXISTING AND INSTALL CLASS A ASSEMBLY"),
    ("ALTERATION", "KITCHEN AND BATH REMODEL; NO STRUCTURAL WORK"),
    ("ALTERATION", "SEISMIC RETROFIT OF SOFT STORY PER MANDATORY PROGRAM"),
    ("ALTERATION", "REPLACE WINDOWS IN KIND; NO CHANGE TO OPENINGS"),
    ("ADDITION", "CONVERT ATTIC TO HABITABLE SPACE; ADD SHED DORMER AT REAR"),
    ("ADDITION", "VERTICAL ADDITION; ADD ONE STOREY OVER EXISTING STRUCTURE"),
    ("ADDITION", "GROUND FLOOR REAR ADDITION; EXTEND KITCHEN 12 FT"),
    ("ELECTRICAL", "INSTALL ROOF MOUNTED PHOTOVOLTAIC ARRAY, 6.4 KW, 18 PANELS"),
    ("ELECTRICAL", "SERVICE UPGRADE TO 200 AMP; NEW PANEL AT GARAGE"),
    ("PLUMBING", "REPLACE WATER HEATER; RELOCATE TO EXTERIOR CLOSET"),
    ("DEMOLITION", "REMOVE NON-BEARING PARTITIONS AT SECOND FLOOR"),
)

_PERMIT_STATUS: Final[tuple[str, ...]] = (
    "COMPLETE",
    "ISSUED",
    "FILED",
    "APPROVED",
    "EXPIRED",
    # The interesting one: work that was never signed off.
    "WITHDRAWN",
)

_CONSTRUCTION: Final[tuple[str, ...]] = (
    "TYPE V WOOD FRAME",
    "TYPE III ORDINARY",
    "TYPE I FIRE RESISTIVE",
    "TYPE II NON-COMBUSTIBLE",
)

_INSPECTION_FINDINGS: Final[tuple[tuple[str, str], ...]] = (
    ("PASS", "Annual inspection. No violations observed. Egress clear."),
    ("PASS", "Sprinkler system tested; flow alarm operated within limits."),
    ("PASS", "Standpipe inspection complete. Hose valves accessible."),
    (
        "FAIL",
        "Rear stairwell obstructed by stored materials at second floor landing. "
        "Occupant advised to clear within 30 days.",
    ),
    (
        "FAIL",
        "Fire door at corridor propped open with wedge. Self-closer disconnected.",
    ),
    (
        "FAIL",
        "Sprinkler heads painted in two units. Heads require replacement before " "re-inspection.",
    ),
    (
        "REINSPECT",
        "Alarm panel in trouble. Monitoring company notified. Re-inspect in 14 days.",
    ),
    (
        "PASS",
        "Lightweight parallel chord truss floor system noted at first floor over "
        "garage. No defects observed. Recorded for pre-incident planning.",
    ),
)

_VIOLATION_CODES: Final[tuple[tuple[str, str], ...]] = (
    ("1103.1", "Means of egress obstructed"),
    ("901.6", "Fire protection system not maintained"),
    ("703.1", "Fire-resistance-rated assembly breached"),
    ("605.1", "Electrical hazard; open junction box"),
    ("304.1", "Accumulation of combustible waste"),
)

_CHEMICALS: Final[tuple[tuple[str, str, str], ...]] = (
    ("Sodium hydroxide", "1310-73-2", "CORROSIVE"),
    ("Sulfuric acid", "7664-93-9", "CORROSIVE"),
    ("Acetylene", "74-86-2", "FLAMMABLE GAS"),
    ("Diesel fuel", "68476-34-6", "COMBUSTIBLE LIQUID"),
    ("Lithium ion batteries", "N/A", "REACTIVE"),
    ("Chlorine", "7782-50-5", "TOXIC GAS"),
)

_OCCUPANCY: Final[tuple[str, ...]] = (
    "R-2 RESIDENTIAL",
    "R-3 RESIDENTIAL",
    "B BUSINESS",
    "M MERCANTILE",
    "F-1 FACTORY",
    "S-1 STORAGE",
)

_INCIDENT_TYPES: Final[tuple[tuple[str, str], ...]] = (
    ("111", "Building fire"),
    ("113", "Cooking fire, confined to container"),
    ("118", "Trash or rubbish fire, contained"),
    ("151", "Outside rubbish fire"),
    ("311", "Medical assist"),
    ("412", "Gas leak, natural gas or LPG"),
    ("445", "Arcing, shorted electrical equipment"),
    ("743", "Smoke detector activation, no fire"),
)

_RANKS: Final[tuple[tuple[str, int], ...]] = (
    ("Chief", 1),
    ("Captain", 1),
    ("Lieutenant", 1),
    ("Engineer", 1),
    ("Firefighter", 4),
    ("Paramedic", 2),
)

_APPARATUS: Final[tuple[str, ...]] = ("Engine", "Truck", "Rescue", "Battalion")

_SURNAMES: Final[tuple[str, ...]] = (
    "Alvarez",
    "Boyle",
    "Castellano",
    "Duong",
    "Ellery",
    "Fitzgerald",
    "Guerrero",
    "Halloran",
    "Ikeda",
    "Jamison",
    "Kowalski",
    "Lindqvist",
    "Moreau",
    "Nakamura",
    "Okonkwo",
    "Pereira",
    "Quill",
    "Rasmussen",
    "Sandoval",
    "Tremblay",
)

_GIVEN: Final[tuple[str, ...]] = (
    "A.",
    "B.",
    "C.",
    "D.",
    "E.",
    "F.",
    "G.",
    "H.",
    "J.",
    "K.",
    "L.",
    "M.",
    "N.",
    "P.",
    "R.",
    "S.",
    "T.",
    "W.",
)

#: One address in this many gets a permit that under-reports its storey count,
#: so the remote measurement disagrees with the filing. The rate is low on
#: purpose: a district where every third building is disputed is a district
#: nobody would believe.
_DISPUTE_EVERY: Final[int] = 23

#: And one in this many gets a hazardous-materials filing.
_HAZMAT_EVERY: Final[int] = 11


@dataclass(frozen=True, slots=True)
class CentralCorpus:
    """A generated week, ready to load.

    ``records`` is keyed by collection. ``reference`` holds the department's
    own rows -- incidents and personnel -- which are plain documents rather
    than :class:`SourceRecord`s because nothing extracts structural facts from
    them.
    """

    corpus_version: str
    seed: str
    epoch: datetime
    window_days: int
    records: dict[str, tuple[SourceRecord, ...]] = field(default_factory=dict)
    reference: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)

    @property
    def record_count(self) -> int:
        return sum(len(v) for v in self.records.values()) + sum(
            len(v) for v in self.reference.values()
        )

    def summary(self) -> dict[str, int]:
        counts = {name: len(rows) for name, rows in self.records.items()}
        counts.update({name: len(rows) for name, rows in self.reference.items()})
        return dict(sorted(counts.items()))


def _stream(seed: str, *parts: str) -> random.Random:
    """A random stream keyed on the address, not on iteration order.

    Keying per address is what lets the corpus grow without rewriting itself:
    adding a building to the reference file changes that building's records and
    nothing else, so a district's history stays stable as the fixture expands.
    """
    material = "|".join((seed, *parts))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))  # noqa: S311 - shapes a fixture, not a secret


def _ref(prefix: str, seed: str, *parts: str) -> str:
    material = "|".join((seed, prefix, *parts))
    return f"{prefix}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12].upper()}"


def _within(rng: random.Random, epoch: datetime, days: int) -> datetime:
    """A timestamp inside the window, to the minute."""
    return epoch - timedelta(
        days=rng.randrange(days), hours=rng.randrange(24), minutes=rng.randrange(60)
    )


def _permits(
    address: NormalizedAddress, rng: random.Random, epoch: datetime, seed: str, days: int
) -> list[SourceRecord]:
    """Filed building permits, including the one that under-reports storeys."""
    out: list[SourceRecord] = []
    filed = rng.choices((0, 1, 2, 3), weights=(18, 42, 28, 12))[0]
    disputes = _stream(seed, "dispute", address.address_id).randrange(_DISPUTE_EVERY) == 0
    true_stories = rng.choice((2, 2, 3, 3, 4))

    for index in range(filed):
        work_type, description = rng.choice(_PERMIT_WORK)
        observed = _within(rng, epoch, days)
        # The disagreement: the filing says one storey fewer than the building
        # has. Nothing marks it -- the conflict engine has to find it.
        stories = true_stories - 1 if (disputes and index == 0) else true_stories
        number = _ref("PERMIT", seed, address.address_id, str(index))
        out.append(
            SourceRecord(
                record_ref=f"permit/{number}",
                address_id=address.address_id,
                classification=Classification.PUBLIC,
                fields={
                    "permit_number": number,
                    "status": rng.choice(_PERMIT_STATUS),
                    "permit_type": work_type,
                    "street_address": address.display,
                    "stories_filed": stories,
                    "construction_type": rng.choice(_CONSTRUCTION),
                    "estimated_cost": rng.randrange(5, 900) * 1000,
                    "block_lot": address.parcel_ref,
                    "synthetic": True,
                },
                document_text=(
                    f"{description}. Existing building of {stories} storeys, "
                    f"{rng.choice(_OCCUPANCY).lower()} occupancy. "
                    f"Work at {address.display}."
                ),
                observed_at=observed,
            )
        )
    return out


def _assessor(
    address: NormalizedAddress, rng: random.Random, epoch: datetime, seed: str
) -> list[SourceRecord]:
    """One roll entry per parcel. The assessor publishes annually, not weekly."""
    year_built = rng.randrange(1890, 2019)
    return [
        SourceRecord(
            record_ref=f"assessor/{address.parcel_ref or address.address_id}",
            address_id=address.address_id,
            classification=Classification.PUBLIC,
            fields={
                "block_lot": address.parcel_ref,
                "street_address": address.display,
                "year_built": year_built,
                "stories": rng.choice((1, 2, 2, 3, 3, 4)),
                "units": rng.choice((1, 1, 2, 3, 4, 6, 12)),
                "construction_class": rng.choice(_CONSTRUCTION),
                "use_code": rng.choice(_OCCUPANCY),
                "closed_roll_year": epoch.year - 1,
                "synthetic": True,
            },
            document_text=None,
            observed_at=epoch - timedelta(days=rng.randrange(120, 400)),
        )
    ]


def _inspections(
    address: NormalizedAddress, rng: random.Random, epoch: datetime, seed: str, days: int
) -> list[SourceRecord]:
    """Fire inspections, with the narrative an inspector actually types."""
    out: list[SourceRecord] = []
    for index in range(rng.choices((0, 1, 2), weights=(28, 52, 20))[0]):
        status, narrative = rng.choice(_INSPECTION_FINDINGS)
        number = _ref("INSP", seed, address.address_id, str(index))
        out.append(
            SourceRecord(
                record_ref=f"inspection/{number}",
                address_id=address.address_id,
                classification=Classification.PUBLIC,
                fields={
                    "inspection_number": number,
                    "street_address": address.display,
                    "inspection_type": rng.choice(
                        ("ANNUAL", "COMPLAINT", "REINSPECTION", "PERMIT")
                    ),
                    "status": status,
                    "station": f"Station {rng.randrange(1, 52)}",
                    "battalion": f"B{rng.randrange(1, 11):02d}",
                    "sprinklered": rng.choice((True, False, False)),
                    "synthetic": True,
                },
                document_text=narrative,
                observed_at=_within(rng, epoch, days),
            )
        )
    return out


def _violations(
    address: NormalizedAddress, rng: random.Random, epoch: datetime, seed: str, days: int
) -> list[SourceRecord]:
    out: list[SourceRecord] = []
    for index in range(rng.choices((0, 1), weights=(72, 28))[0]):
        code, description = rng.choice(_VIOLATION_CODES)
        number = _ref("VIOL", seed, address.address_id, str(index))
        abated = rng.choice((True, False))
        out.append(
            SourceRecord(
                record_ref=f"violation/{number}",
                address_id=address.address_id,
                classification=Classification.PUBLIC,
                fields={
                    "violation_number": number,
                    "street_address": address.display,
                    "code_section": code,
                    "status": "ABATED" if abated else "OPEN",
                    "synthetic": True,
                },
                document_text=(
                    f"{description}. Cited at {address.display}. "
                    + ("Abated on re-inspection." if abated else "Remains open.")
                ),
                observed_at=_within(rng, epoch, days),
            )
        )
    return out


def _hazmat(
    address: NormalizedAddress, rng: random.Random, epoch: datetime, seed: str
) -> list[SourceRecord]:
    """Tier II filings. Confidential under EPCRA, and classified as such.

    This is the one collection whose classification is not ``PUBLIC``, which is
    what makes it the gateway's exercise in the slow loop rather than only
    during an incident: an agent without ``read:tier-ii-metadata`` never sees
    these, and a question raised by one is invisible to it in the memory bank
    too.
    """
    if _stream(seed, "hazmat", address.address_id).randrange(_HAZMAT_EVERY) != 0:
        return []
    name, cas, hazard = rng.choice(_CHEMICALS)
    number = _ref("TIER2", seed, address.address_id)
    return [
        SourceRecord(
            record_ref=f"tier-ii/{number}",
            address_id=address.address_id,
            classification=Classification.TIER_II_CONFIDENTIAL,
            fields={
                "filing_number": number,
                "facility_name": (
                    f"{rng.choice(_SURNAMES).upper()} "
                    f"{rng.choice(('PLATING', 'WORKS', 'SUPPLY', 'LABS'))} INC"
                ),
                "street_address": address.display,
                "chemical_name": name,
                "cas_number": cas,
                "hazard_class": hazard,
                "max_quantity_lbs": rng.randrange(200, 40_000),
                "storage": rng.choice(("BASEMENT", "GROUND FLOOR", "EXTERIOR TANK", "ROOF")),
                "reporting_year": epoch.year - 1,
                "synthetic": True,
            },
            document_text=(
                f"{name} stored in {rng.choice(('drums', 'cylinders', 'a bulk tank'))}. "
                f"Hazard class {hazard}."
            ),
            observed_at=epoch - timedelta(days=rng.randrange(30, 300)),
        )
    ]


def _incidents(
    addresses: Sequence[NormalizedAddress], seed: str, epoch: datetime
) -> list[dict[str, Any]]:
    """Prior incidents, over years rather than a week.

    Read by the incident loop, not by the watchers: what happened at an address
    before is history a commander wants on arrival, and it is never extracted
    into a structural fact about the building.
    """
    out: list[dict[str, Any]] = []
    for address in addresses:
        rng = _stream(seed, "incident", address.address_id)
        for index in range(rng.choices((0, 0, 1, 2), weights=(52, 22, 18, 8))[0]):
            code, label = rng.choice(_INCIDENT_TYPES)
            opened = epoch - timedelta(days=rng.randrange(30, 2200), minutes=rng.randrange(1440))
            out.append(
                {
                    "incident_ref": _ref("INC", seed, address.address_id, str(index)),
                    "address_id": address.address_id,
                    "street_address": address.display,
                    "incident_type_code": code,
                    "incident_type": label,
                    "alarm_level": rng.choices((1, 2, 3), weights=(80, 16, 4))[0],
                    "opened_at": opened.isoformat(),
                    "contained_minutes": rng.randrange(4, 180),
                    "companies": sorted(
                        rng.sample(
                            [f"{a} {rng.randrange(1, 52)}" for a in _APPARATUS],
                            k=rng.randrange(1, 4),
                        )
                    ),
                    "civilian_injuries": rng.choices((0, 1, 2), weights=(90, 8, 2))[0],
                    "notes": (
                        f"{label} at {address.display}. "
                        "Contained to "
                        f"{rng.choice(('room', 'floor', 'unit', 'structure'))} of origin."
                    ),
                    "synthetic": True,
                }
            )
    return out


def _personnel(seed: str, epoch: datetime, districts: Sequence[str]) -> list[dict[str, Any]]:
    """Companies, apparatus and who is riding them.

    No real person appears here: names are drawn from a fixed word list and
    paired with a shift, which is the whole record. Nothing in the system reads
    a person's details -- this exists so a district's staffing is a real number
    on the console rather than an assumption.
    """
    out: list[dict[str, Any]] = []
    rng = _stream(seed, "personnel")
    station_numbers = sorted(rng.sample(range(1, 52), k=8))
    for station in station_numbers:
        district = districts[station % len(districts)]
        for apparatus in rng.sample(_APPARATUS, k=rng.randrange(1, 3)):
            unit = f"{apparatus} {station}"
            for rank, count in _RANKS:
                for slot in range(count):
                    out.append(
                        {
                            "personnel_ref": _ref("PSN", seed, unit, rank, str(slot)),
                            "unit": unit,
                            "station": f"Station {station}",
                            "district_id": district,
                            "rank": rank,
                            "name": f"{rng.choice(_GIVEN)} {rng.choice(_SURNAMES)}",
                            "shift": rng.choice(("A", "B", "C")),
                            "certifications": sorted(
                                rng.sample(
                                    ["EMT", "PARAMEDIC", "HAZMAT-TECH", "RESCUE-SYS", "DRIVER"],
                                    k=rng.randrange(1, 4),
                                )
                            ),
                            "years_of_service": rng.randrange(1, 31),
                            "synthetic": True,
                        }
                    )
    return out


def build_corpus(
    *,
    addresses: Sequence[NormalizedAddress],
    districts: Sequence[str],
    epoch: datetime,
    seed: str = "firstdue-central",
    window_days: int = 7,
) -> CentralCorpus:
    """Generate the department's records over ``window_days`` ending at ``epoch``.

    Args:
        addresses: every structure the municipality knows about.
        districts: district ids, for staffing assignment.
        epoch: the end of the window. Passed in, never read from a clock, so
            the corpus is reproducible.
        seed: the random seed. A different seed is a different municipality.
        window_days: how much recent activity to generate. Permits, inspections
            and violations land inside it; the assessor's roll, hazardous
            materials filings and prior incidents are older by nature and are
            dated accordingly.
    """
    if epoch.tzinfo is None:
        raise ValueError("epoch must be timezone-aware")

    buckets: dict[str, list[SourceRecord]] = {name: [] for name in CENTRAL_COLLECTIONS}
    for address in sorted(addresses, key=lambda a: a.address_id):
        rng = _stream(seed, "records", address.address_id)
        buckets["central_permits"].extend(_permits(address, rng, epoch, seed, window_days))
        buckets["central_assessor"].extend(_assessor(address, rng, epoch, seed))
        buckets["central_inspections"].extend(_inspections(address, rng, epoch, seed, window_days))
        buckets["central_violations"].extend(_violations(address, rng, epoch, seed, window_days))
        buckets["central_hazmat"].extend(_hazmat(address, rng, epoch, seed))

    return CentralCorpus(
        corpus_version=CORPUS_VERSION,
        seed=seed,
        epoch=epoch,
        window_days=window_days,
        records={
            # Sorted by record_ref so the load order is stable and two runs
            # write the same documents in the same sequence.
            name: tuple(sorted(rows, key=lambda r: r.record_ref))
            for name, rows in buckets.items()
        },
        reference={
            "central_incidents": tuple(
                sorted(_incidents(addresses, seed, epoch), key=lambda r: r["incident_ref"])
            ),
            "central_personnel": tuple(
                sorted(_personnel(seed, epoch, districts), key=lambda r: r["personnel_ref"])
            ),
        },
    )


def corpus_epoch(now: datetime) -> datetime:
    """The epoch a load should use: midnight UTC today.

    Rounding to the day is what keeps a corpus stable across a demo. Generating
    against `now` would produce a different document set every run and make the
    content hash meaningless.
    """
    return datetime(now.year, now.month, now.day, tzinfo=UTC)
