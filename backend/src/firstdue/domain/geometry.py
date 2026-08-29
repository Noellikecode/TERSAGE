"""The client-side geometry spec.

Python emits a small JSON spec; the browser renders it. No meshes, no model
files, no server-side rendering.

Every element carries provenance and status, so **the conflict is in the data,
not the renderer** -- a disputed third floor arrives marked ``DISPUTED`` and any
renderer, including the static SVG fallback, shows it as disputed.
"""

from __future__ import annotations

import math
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


class ThermalCell(BaseModel):
    """One measured patch of a face, in face-plane coordinates.

    This is the heat map, and it is **registered** rather than decorative: a
    cell names the rectangle of the wall it was measured on, so a renderer maps
    it onto the extruded face quad at exactly the place the camera saw it.

    Coordinates are fractions of the face itself, which the renderer already
    knows the real size of -- the width comes from the footprint edge the
    Geometry Watcher measured, the height from the levels it derived. So the
    spec stays small and the overlay still lands in metres.

    * ``u`` runs across the face width, 0 at the first corner of the edge.
    * ``v`` runs **up** the face, 0 at the ground and 1 at the eaves.

    ``v`` is deliberately not image ``y``. Image y grows downward and a renderer
    handed raw image coordinates would paint the cockloft onto the foundation,
    which is precisely inverted from the one thing this overlay exists to show.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    u_from: float = Field(ge=0.0, le=1.0)
    u_to: float = Field(gt=0.0, le=1.0)
    v_from: float = Field(ge=0.0, le=1.0)
    v_to: float = Field(gt=0.0, le=1.0)
    temperature_c: float = Field(ge=-50.0, le=1500.0)

    @model_validator(mode="after")
    def _extents_are_ordered(self) -> Self:
        if self.u_to <= self.u_from or self.v_to <= self.v_from:
            raise ValidationError(
                "a thermal cell must have positive extent in both directions",
                details={"u": [self.u_from, self.u_to], "v": [self.v_from, self.v_to]},
            )
        return self

    @property
    def height_fraction(self) -> float:
        return self.v_to - self.v_from


class Face(BaseModel):
    """A labelled exterior face, and what thermal coverage it has."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: FaceLabel
    thermal: FaceThermal = UnscannedValue()
    observed_at: datetime | None = None
    #: The registered heat map. Every cell is a rectangle a camera actually
    #: measured; there is no cell anywhere that was interpolated, defaulted, or
    #: predicted, and the gaps between cells are gaps on purpose.
    thermal_cells: tuple[ThermalCell, ...] = ()

    @model_validator(mode="after")
    def _coverage_requires_timestamp(self) -> Self:
        if isinstance(self.thermal, QuantityValue) and self.observed_at is None:
            raise ValidationError(
                "a measured face temperature must carry the time it was observed",
                details={"face": str(self.label)},
            )
        # The type-level version of "UNSCANNED is not cool". A face nobody
        # measured cannot carry cells, so no renderer can shade one warm.
        if self.thermal_cells and not isinstance(self.thermal, QuantityValue):
            raise ValidationError(
                "an unscanned or unavailable face cannot carry thermal cells",
                details={"face": str(self.label)},
            )
        return self

    @property
    def peak_cell(self) -> ThermalCell | None:
        """The hottest measured cell. What the face summary reports."""
        return max(self.thermal_cells, key=lambda c: c.temperature_c, default=None)

    @property
    def scanned_fraction(self) -> float:
        """Fraction of the face area the cells actually cover.

        Summed cell area, capped at one. Rendered next to the heat map so a
        partly-flown wall cannot read as a fully-measured one.
        """
        area = sum((c.u_to - c.u_from) * (c.v_to - c.v_from) for c in self.thermal_cells)
        return round(min(1.0, max(0.0, area)), 3)


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


#: The four labelled faces, in fireground order. ROOF is deliberately absent:
#: it is not a vertical face and nothing resolves a ground-level bearing to it.
GROUND_FACES: Final[tuple[FaceLabel, ...]] = (
    FaceLabel.ALPHA,
    FaceLabel.BRAVO,
    FaceLabel.CHARLIE,
    FaceLabel.DELTA,
)

#: How far off a face normal a camera bearing may be and still resolve to that
#: face. Ninety degrees would tile the compass with no gaps, which sounds
#: desirable and is not -- see the ambiguity margin below. Outside this, the
#: reading is refused and the face stays UNSCANNED.
FACE_BEARING_TOLERANCE_DEG: Final[float] = 55.0

#: How much closer the best face must be than the runner-up. A camera on the
#: corner of a building is equidistant from two walls, and picking one is
#: picking whichever won a floating-point comparison -- it would paint a
#: measured temperature onto a wall nobody pointed a camera at, which an
#: officer then reads as coverage. A corner shot resolves to no face.
FACE_AMBIGUITY_MARGIN_DEG: Final[float] = 10.0


class FaceGeometry(BaseModel):
    """One face of the footprint, with the compass bearing it looks out along.

    Derived from the parcel footprint the **Geometry Watcher** established
    during the slow loop. Nothing in the incident loop computes this: if the
    slow loop never profiled the address, there are no face bearings and an
    incoming frame cannot be resolved to a face at all.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: FaceLabel
    #: Outward normal, degrees clockwise from north.
    bearing_deg: float = Field(ge=0.0, lt=360.0)
    #: Length of the wall, metres. The longest wall becomes Alpha.
    length_m: float = Field(gt=0.0)


def edge_normal_bearing(start: Point2D, end: Point2D) -> float:
    """Outward normal of an edge, in degrees clockwise from north.

    The footprint is in local ENU metres (x east, y north) and wound
    counter-clockwise, so the outward normal of an edge running (x0,y0)->(x1,y1)
    is the edge vector rotated -90 degrees.
    """
    dx, dy = end[0] - start[0], end[1] - start[1]
    # Rotate -90 degrees: (dx, dy) -> (dy, -dx), then convert to a compass
    # bearing, which measures clockwise from north rather than
    # counter-clockwise from east.
    normal_x, normal_y = dy, -dx
    return math.degrees(math.atan2(normal_x, normal_y)) % 360.0


def face_geometries(footprint: tuple[Point2D, ...]) -> tuple[FaceGeometry, ...]:
    """Resolve a footprint to four labelled faces with compass bearings.

    **Alpha is the longest wall.** The fire service convention is that Alpha is
    the address side, and a bare polygon does not know where the street is --
    so this states the rule it actually applies rather than implying knowledge
    it does not have. On the overwhelming majority of parcels the longest wall
    is the street frontage; where it is not, the labelling is consistent and
    wrong in a way an officer can see on the massing model and correct, which
    is better than a labelling that is inconsistent between runs.

    Bravo, Charlie and Delta follow clockwise from Alpha, which is the
    convention on every fireground.

    Raises:
        ValidationError: for a footprint with fewer than three points, which is
            not a polygon and cannot have faces.
    """
    if len(footprint) < 3:
        raise ValidationError(
            "a footprint needs at least three points to have faces",
            details={"points": len(footprint)},
        )

    edges: list[tuple[float, float]] = []
    for index, start in enumerate(footprint):
        end = footprint[(index + 1) % len(footprint)]
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        if length <= 0.0:
            continue
        edges.append((edge_normal_bearing(start, end), length))

    if not edges:
        raise ValidationError("a footprint of zero-length edges has no faces")

    # Alpha is the longest wall. Ties break on the lower bearing so a square
    # footprint labels identically on every run -- a massing model whose Alpha
    # moved between two renders of the same building would be worse than one
    # whose Alpha is arguable.
    alpha_bearing = min(edges, key=lambda e: (-e[1], e[0]))[0]
    longest = max(length for _, length in edges)

    return tuple(
        FaceGeometry(
            label=label,
            bearing_deg=(alpha_bearing + 90.0 * offset) % 360.0,
            length_m=longest if offset % 2 == 0 else max(longest / 2.0, 0.1),
        )
        for offset, label in enumerate(GROUND_FACES)
    )


def angular_distance_deg(a: float, b: float) -> float:
    """Smallest angle between two bearings, 0-180."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def resolve_face(
    camera_bearing_deg: float,
    footprint: tuple[Point2D, ...],
    *,
    tolerance_deg: float = FACE_BEARING_TOLERANCE_DEG,
    ambiguity_margin_deg: float = FACE_AMBIGUITY_MARGIN_DEG,
) -> FaceGeometry | None:
    """Which face a camera on this bearing is looking at, or ``None``.

    The camera bearing is the direction the lens points. A camera looking at
    Alpha points roughly *opposite* Alpha's outward normal, so the two are
    compared after reversing one of them.

    Returns ``None`` rather than a best guess in two cases: nothing is within
    ``tolerance_deg``, or the two nearest faces are within
    ``ambiguity_margin_deg`` of each other and the shot is on a corner. A frame
    attributed to the wrong wall is worse than a frame attributed to no wall --
    the first paints a measured temperature onto a side nobody pointed a camera
    at, and the officer reads it as coverage.
    """
    faces = face_geometries(footprint)
    looking_at = (camera_bearing_deg + 180.0) % 360.0
    ranked = sorted(faces, key=lambda f: angular_distance_deg(looking_at, f.bearing_deg))
    best_off = angular_distance_deg(looking_at, ranked[0].bearing_deg)
    if best_off > tolerance_deg:
        return None
    runner_up_off = angular_distance_deg(looking_at, ranked[1].bearing_deg)
    if runner_up_off - best_off < ambiguity_margin_deg:
        return None
    return ranked[0]
