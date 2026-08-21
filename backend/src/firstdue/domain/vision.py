"""What a model is allowed to report about an image.

An image arriving from a drone or a handheld TIC is **an untrusted source
document**, exactly like a permit PDF, and it is handled the same way: the model
extracts observations bound to a region of the frame, and deterministic code
decides what -- if anything -- those observations mean.

The shape here is deliberately the imagery twin of
:class:`~firstdue.ports.model.ExtractionResult`:

* every observation carries an :class:`ImageRegion`, which is the image's
  equivalent of a source span. A reading a human cannot point at in the frame
  is not an observation, it is a claim;
* ``unknowns`` is required, so a model that cannot see a face's storey count
  has to say so rather than filling it with a plausible number;
* ``accepted`` makes rejection a value rather than an exception.

**What is deliberately absent.** There is no field for a face label, no field
for a conclusion, and no field for anything about the fire. The face is decided
by :func:`~firstdue.domain.geometry.resolve_face` from the footprint the slow
loop measured; the model is never asked which wall it is looking at, because a
model that could name the wall could also name it wrong and paint a temperature
onto a side nobody photographed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.errors import ValidationError

#: Attached to every temperature this module carries, for the same reason it is
#: attached in the fusion agent: a thermal camera measures the outside of a
#: wall, and a commander reading a cool wall as an empty room is the failure
#: this sentence exists to prevent.
THERMAL_CAVEAT: Final[str] = (
    "Thermal imaging measures exterior surface temperature and cannot see through walls."
)


class ObservationKind(StrEnum):
    """What an image can be asked about.

    Closed on purpose, and short. Each member is something a camera can
    *see*; none of them is something a camera can conclude. There is no
    ``FIRE_LOCATION``, no ``OCCUPANCY``, no ``STRUCTURAL_INTEGRITY``: a
    capability absent from the enum cannot be reached for under deadline.
    """

    #: A measured surface temperature over one region of the frame.
    THERMAL_REGION = "THERMAL_REGION"
    #: A window, door, or other opening -- egress and ventilation.
    OPENING = "OPENING"
    #: A horizontal band of openings, which is evidence of one storey.
    STOREY_BAND = "STOREY_BAND"
    #: Something fixed to the structure a crew cannot cut through.
    OBSTRUCTION = "OBSTRUCTION"
    #: Visible exterior cladding or construction material.
    MATERIAL = "MATERIAL"


class ImageRegion(BaseModel):
    """A normalised box in the frame. The image's equivalent of a source span.

    Normalised to 0-1 rather than pixels so a region survives the frame being
    resized, and so a region is checkable against a re-render without knowing
    the sensor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _stays_inside_the_frame(self) -> ImageRegion:
        if self.x + self.width > 1.0001 or self.y + self.height > 1.0001:
            raise ValidationError(
                "an image region must lie inside the frame",
                details={"x": self.x, "y": self.y},
            )
        return self


class VisionObservation(BaseModel):
    """One thing a model reports seeing, bound to where it saw it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ObservationKind
    region: ImageRegion
    #: The raw reading. Interpreted by deterministic code, never trusted as a
    #: typed value straight from the model.
    raw_value: str = Field(min_length=1, max_length=400)
    model_confidence: float = Field(ge=0.0, le=1.0)

    @property
    def temperature_c(self) -> float | None:
        """``raw_value`` as a temperature, or ``None`` if it is not one.

        Parsing lives here rather than in the model contract because a model
        that returned a typed float would be asserting a measurement. What it
        returns is text, and text that does not parse is discarded rather than
        coerced -- a garbled reading must not become a plausible temperature.
        """
        if self.kind is not ObservationKind.THERMAL_REGION:
            return None
        try:
            value = float(self.raw_value.strip().removesuffix("C").strip())
        except ValueError:
            return None
        # Surface temperatures outside this band are a parse error wearing a
        # number: liquid nitrogen and steel furnaces are not fireground walls.
        return value if -50.0 <= value <= 1500.0 else None


class VisionResult(BaseModel):
    """Structured output contract for one frame.

    ``unknowns`` is required and may be empty. A model that must name what it
    could not determine cannot quietly fill a gap with something plausible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[VisionObservation, ...] = ()
    #: What the frame did not settle. Required, may be empty.
    unknowns: tuple[str, ...] = ()
    accepted: bool = True
    rejection_reason: str | None = Field(default=None, max_length=300)
    #: Which model produced this, for the audit trail.
    model_ref: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def _a_rejection_says_why(self) -> VisionResult:
        if not self.accepted and not self.rejection_reason:
            raise ValidationError("a rejected vision result must say why")
        if not self.accepted and self.observations:
            raise ValidationError(
                "a rejected vision result cannot carry observations",
                details={"observations": len(self.observations)},
            )
        return self

    def of_kind(self, kind: ObservationKind) -> tuple[VisionObservation, ...]:
        return tuple(o for o in self.observations if o.kind is kind)

    @property
    def temperatures_c(self) -> tuple[float, ...]:
        """Parsed thermal readings, in frame order, discarding what will not parse."""
        values = (o.temperature_c for o in self.of_kind(ObservationKind.THERMAL_REGION))
        return tuple(v for v in values if v is not None)
