"""The NFPA 1620 pre-incident plan artifact.

NFPA 1620 is the standard for pre-incident planning: what a plan should contain
so that a crew arriving at a building already knows its construction, its
occupancy, its water supply, its hazards, and what nobody has confirmed.

That last section is why this module exists rather than a template file. A
pre-plan that lists five confirmed facts and silently omits the six attributes
nobody checked reads as a complete picture of a simple building. This one prints
the unknowns as a section with a heading, because an officer who knows what is
unknown makes a different decision from one who does not.

Nothing here is tactical. The plan states what is on file, what disagrees, and
what was never established. It recommends nothing.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.conflicts import ConflictStatus
from firstdue.domain.keys import CanonicalKey, Keys
from firstdue.domain.profiles import BuildingProfile

STANDARD: Final[str] = "NFPA 1620"
PLAN_VERSION: Final[int] = 1

#: Sections, in the order NFPA 1620 presents them, with the keys each covers.
SECTIONS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "Construction",
        (Keys.CONSTRUCTION_TYPE, Keys.STORIES, Keys.HEIGHT_M, Keys.YEAR_BUILT, Keys.ROOF_TYPE),
    ),
    ("Structural systems", (Keys.FLOOR_SYSTEM, Keys.LIGHTWEIGHT_TRUSS)),
    ("Occupancy", (Keys.OCCUPANCY_TYPE, Keys.OCCUPANT_LOAD, Keys.LIFE_SAFETY_NOTE)),
    (
        "Fire protection systems",
        (Keys.SUPPRESSION_SPRINKLERED, Keys.SUPPRESSION_STANDPIPE, Keys.FDC_LOCATION),
    ),
    ("Egress", (Keys.EGRESS_OBSTRUCTION, Keys.STAIRWELL_COUNT)),
    (
        "Hazardous materials and utilities",
        (
            Keys.HAZARD_TIER_II_PRESENT,
            Keys.HAZARD_TIER_II_LOCATION,
            Keys.HAZARD_SOLAR_ARRAY,
            Keys.HAZARD_EV_CHARGER,
            Keys.HAZARD_PIPELINE_PROXIMITY_M,
        ),
    ),
)


class PlanLine(BaseModel):
    """One attribute as the plan states it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    canonical_key: CanonicalKey
    value: str = Field(min_length=1, max_length=500)
    source: str = Field(min_length=1, max_length=120)
    observed_at: datetime | None = None
    #: True when the sources disagree. Rendered distinctly, never resolved away.
    disputed: bool = False
    #: True when nothing on file settles this attribute.
    unknown: bool = False


class PlanSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    lines: tuple[PlanLine, ...] = ()


class PreIncidentPlan(BaseModel):
    """The artifact written to the plan store."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    standard: str = STANDARD
    plan_version: int = PLAN_VERSION
    address_id: str = Field(min_length=1, max_length=120)
    district_id: str = Field(min_length=1, max_length=120)
    #: The exact profile version this plan describes, for replay.
    profile_version: int = Field(ge=0)
    generated_at: datetime

    sections: tuple[PlanSection, ...] = ()
    #: Open disagreements, stated as disagreements.
    conflicts: tuple[str, ...] = ()
    #: Attributes nobody has established. Printed, never omitted.
    unknowns: tuple[str, ...] = ()
    collapse_zone_radius_m: float | None = None
    hydrant_ids: tuple[str, ...] = ()
    #: Static geometry for a renderer that cannot run JavaScript.
    svg: str = Field(default="", max_length=100_000)

    @property
    def content_hash(self) -> str:
        payload = self.model_dump(mode="json")
        canonical = str(sorted(payload.items()))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_bytes(self) -> bytes:
        """Canonical JSON, so the same plan hashes the same on every run."""
        import json

        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")


def render_svg(profile: BuildingProfile) -> str:
    """A static elevation of the structure, disputed storeys marked.

    The fallback for a renderer that cannot run the interactive spec. It is
    deliberately crude and deliberately honest: a disputed storey is drawn with
    a dashed outline and labelled, because the conflict is in the data and any
    renderer must show it.
    """
    geometry = profile.geometry
    if geometry is None or not geometry.levels:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 120" '
            'role="img" aria-label="No geometry on record">'
            '<rect width="200" height="120" fill="none" stroke="#888" '
            'stroke-dasharray="4 3"/>'
            '<text x="100" y="64" text-anchor="middle" font-size="10" fill="#888">'
            "NO GEOMETRY ON RECORD</text></svg>"
        )

    level_height = 28
    width = 160
    height = level_height * len(geometry.levels) + 30
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width + 40} {height}" '
        f'role="img" aria-label="Elevation of {profile.address_id}">'
    ]
    for index, level in enumerate(reversed(geometry.levels)):
        y = 10 + index * level_height
        disputed = level.status.value == "DISPUTED"
        stroke_style = ' stroke-dasharray="5 3"' if disputed else ""
        parts.append(
            f'<rect x="20" y="{y}" width="{width}" height="{level_height - 4}" '
            f'fill="none" stroke="#333"{stroke_style}/>'
        )
        label = f"{level.height_m:g} m"
        if disputed:
            label += " DISPUTED"
        parts.append(
            f'<text x="{width + 24}" y="{y + 16}" font-size="9" fill="#333">{label}</text>'
        )
    parts.append(
        f'<text x="20" y="{height - 6}" font-size="9" fill="#333">'
        f"collapse zone {geometry.collapse_zone_radius_m:g} m</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def build_plan(profile: BuildingProfile, *, generated_at: datetime) -> PreIncidentPlan:
    """Build the plan for one profile, unknowns included."""
    resolved = profile.facts
    disputed_keys = {
        conflict.canonical_key
        for conflict in profile.conflicts
        if conflict.status is ConflictStatus.OPEN
    }

    sections: list[PlanSection] = []
    unknowns: list[str] = []

    for title, keys in SECTIONS:
        lines: list[PlanLine] = []
        for key in keys:
            fact = resolved.get(key)
            if fact is None:
                unknowns.append(key)
                lines.append(
                    PlanLine(
                        canonical_key=key,
                        value="UNKNOWN - no record found",
                        source="none",
                        unknown=True,
                    )
                )
                continue
            if not fact.value.is_known:
                unknowns.append(key)
            lines.append(
                PlanLine(
                    canonical_key=key,
                    value=fact.value.render(),
                    source=str(fact.source_type),
                    observed_at=fact.observed_at,
                    disputed=key in disputed_keys,
                    unknown=not fact.value.is_known,
                )
            )
        sections.append(PlanSection(title=title, lines=tuple(lines)))

    return PreIncidentPlan(
        address_id=profile.address_id,
        district_id=profile.district_id,
        profile_version=profile.profile_version,
        generated_at=generated_at,
        sections=tuple(sections),
        conflicts=tuple(c.summary for c in profile.conflicts if c.status is ConflictStatus.OPEN),
        unknowns=tuple(sorted(set(unknowns))),
        collapse_zone_radius_m=(
            profile.geometry.collapse_zone_radius_m if profile.geometry else None
        ),
        hydrant_ids=profile.hydrant_ids,
        svg=render_svg(profile),
    )
