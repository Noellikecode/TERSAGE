"""The client-side geometry spec.

Python emits a small JSON spec; the browser renders it. No meshes, no model
files, no server-side rendering.

Every element carries provenance and status, so **the conflict is in the data,
not the renderer** -- a disputed third floor arrives marked ``DISPUTED`` and any
renderer, including the static SVG fallback, shows it as disputed.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Self, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.domain.enums import AssertionStatus, FaceLabel, SourceType
from firstdue.domain.values import QuantityValue, UnavailableValue, UnscannedValue
from firstdue.errors import ValidationError

#: Fireground collapse-zone convention: 1.5x the structure height.
#: This is a geometric standard applied to a measured height. It is not a
#: prediction that this building will collapse.
COLLAPSE_ZONE_HEIGHT_MULTIPLIER: Final[float] = 1.5

Point2D: TypeAlias = tuple[float, float]

#: A face either has a measured temperature, has no coverage, or its sensor is
#: unavailable. There is no representation of "cool by default".
FaceThermal = Annotated[
    QuantityValue | UnscannedValue | UnavailableValue,
    Field(discriminator="kind"),
]


class ObstructionType(StrEnum):
    SOLAR_ARRAY = "SOLAR_ARRAY"
    HVAC_UNIT = "HVAC_UNIT"
    SKYLIGHT = "SKYLIGHT"
    PARAPET = "PARAPET"
    ANTENNA = "ANTENNA"
    EV_CHARGER = "EV_CHARGER"


class Level(BaseModel):
    """One storey of extruded mass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    height_m: float = Field(gt=0.0, le=200.0)
    provenance: SourceType
    status: AssertionStatus
    #: The fact this level was derived from, for the reasoning trace.
    fact_id: str | None = Field(default=None, max_length=120)


class RoofSegment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pitch_deg: float = Field(ge=0.0, le=90.0)
    azimuth_deg: float = Field(ge=0.0, lt=360.0)
    area_m2: float | None = Field(default=None, gt=0.0)
    provenance: SourceType = SourceType.SOLAR_API
    status: AssertionStatus = AssertionStatus.CONFIRMED


class Obstruction(BaseModel):
    """Something on the roof a crew cannot cut through."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: ObstructionType
    segment_index: int = Field(ge=0)
    provenance: SourceType
    status: AssertionStatus = AssertionStatus.CONFIRMED


class Face(BaseModel):
    """A labelled exterior face, and what thermal coverage it has."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: FaceLabel
    thermal: FaceThermal = UnscannedValue()
    observed_at: datetime | None = None

    @model_validator(mode="after")
    def _coverage_requires_timestamp(self) -> Self:
        if isinstance(self.thermal, QuantityValue) and self.observed_at is None:
            raise ValidationError(
                "a measured face temperature must carry the time it was observed",
                details={"face": str(self.label)},
            )
        return self


class GeometrySpec(BaseModel):
    """The renderable structural picture for one address."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec_version: int = 1
    address_id: str = Field(min_length=1, max_length=120)
    generated_at: datetime

    footprint: tuple[Point2D, ...] = Field(min_length=3, description="metres, local ENU")
    levels: tuple[Level, ...] = ()
    roof_segments: tuple[RoofSegment, ...] = ()
    obstructions: tuple[Obstruction, ...] = ()
    faces: tuple[Face, ...] = ()
    collapse_zone_radius_m: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _check_obstruction_targets(self) -> Self:
        for obstruction in self.obstructions:
            if obstruction.segment_index >= len(self.roof_segments):
                raise ValidationError(
                    "obstruction references a roof segment that does not exist",
                    details={"segment_index": obstruction.segment_index},
                )
        labels = [f.label for f in self.faces]
        if len(set(labels)) != len(labels):
            raise ValidationError("duplicate face labels in geometry spec")
        return self

    @property
    def total_height_m(self) -> float:
        return sum(level.height_m for level in self.levels)

    @property
    def has_disputed_mass(self) -> bool:
        """True when at least one level is disputed -- rendered distinctly."""
        return any(level.status is AssertionStatus.DISPUTED for level in self.levels)

    @property
    def unscanned_faces(self) -> tuple[FaceLabel, ...]:
        return tuple(f.label for f in self.faces if isinstance(f.thermal, UnscannedValue))


def collapse_zone_radius(total_height_m: float) -> float:
    """Deterministic collapse zone from measured height.

    Applies the standard 1.5x-height fireground convention. States a geometric
    standard; predicts nothing about this fire.
    """
    if total_height_m < 0:
        raise ValidationError("height cannot be negative")
    return round(total_height_m * COLLAPSE_ZONE_HEIGHT_MULTIPLIER, 2)
