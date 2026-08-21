"""Sensor fusion: thermal frames onto the building's faces.

A drone flies the building and returns frames. Each frame is registered to one
labelled face -- Alpha, Bravo, Charlie, Delta -- and what it measures is
**surface temperature of the exterior skin**. That sentence is attached to every
reading this module produces, because the alternative is a commander reading a
cool Charlie wall as an empty Charlie side.

Three rules:

* **A face with no frame is UNSCANNED, not cool.** There is no default
  temperature anywhere in this module, and `Face.thermal` has no representation
  of "probably fine".
* **Coverage lapses.** A frame older than the coverage window stops counting,
  and the face returns to UNSCANNED rather than holding the last reading.
  Yesterday's warm wall is not today's warm wall.
* **Void detection is deterministic.** A void is a defined temperature
  difference between adjacent measured regions on one face -- arithmetic, stated
  with its threshold, and reported as an observation rather than as a finding
  about what is behind the wall.

Thermal imaging cannot see through walls. It reports the temperature of the
surface it is pointed at, and a hot surface has many causes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import FaceLabel
from firstdue.domain.geometry import Face, GeometrySpec
from firstdue.domain.values import QuantityValue, UnavailableValue, UnscannedValue
from firstdue.errors import ValidationError
from firstdue.observability.logging import get_logger

logger = get_logger(__name__)

#: The sentence that travels with every thermal reading.
THERMAL_CAVEAT: Final[str] = (
    "Thermal imaging measures the surface temperature of the exterior skin. "
    "It cannot see through walls, and a hot surface has many causes."
)

#: How long a frame counts as coverage. After this a face is UNSCANNED again.
DEFAULT_COVERAGE_WINDOW: Final[timedelta] = timedelta(minutes=5)

#: Temperature difference between adjacent regions that registers as a void.
#: A published threshold, stated so an officer can disagree with it.
VOID_DELTA_C: Final[float] = 25.0
#: Minimum absolute temperature before a delta means anything at all.
VOID_FLOOR_C: Final[float] = 40.0


class ThermalFrame(BaseModel):
    """One registered frame: a face, a time, and what was measured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frame_id: str = Field(min_length=1, max_length=120)
    incident_id: str = Field(min_length=1, max_length=120)
    face: FaceLabel
    observed_at: datetime
    #: Region temperatures across the face, in reading order. Celsius.
    region_temps_c: tuple[float, ...] = Field(min_length=1)
    #: Fraction of the face the frame actually covers.
    coverage: float = Field(ge=0.0, le=1.0, default=1.0)
    #: Recorded footage or a synthetic pass. Never presented as a live flight.
    source: str = Field(default="recorded", max_length=60)

    @property
    def peak_c(self) -> float:
        return max(self.region_temps_c)

    def is_current(self, now: datetime, window: timedelta = DEFAULT_COVERAGE_WINDOW) -> bool:
        """Whether this frame still counts as coverage."""
        return (now - self.observed_at) <= window


class VoidObservation(BaseModel):
    """A measured temperature difference on one face.

    An observation, not a conclusion. It says two adjacent regions of the
    exterior differ by more than the threshold; it does not say what is behind
    them, because a thermal camera cannot.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    face: FaceLabel
    region_index: int = Field(ge=0)
    delta_c: float
    peak_c: float
    threshold_c: float = VOID_DELTA_C
    observed_at: datetime
    caveat: str = THERMAL_CAVEAT

    @property
    def render(self) -> str:
        return (
            f"{self.face} region {self.region_index}: {self.delta_c:.0f} C warmer than "
            f"the adjacent region (threshold {self.threshold_c:.0f} C, peak "
            f"{self.peak_c:.0f} C). {self.caveat}"
        )


class FaceCoverage(BaseModel):
    """What is known about one face right now."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    face: FaceLabel
    scanned: bool
    observed_at: datetime | None = None
    peak_c: float | None = None
    coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    #: The rendering an officer reads. "UNSCANNED", never "cool".
    render: str = Field(min_length=1, max_length=300)


class SensorFusion:
    """Registers thermal frames to faces and reports coverage honestly."""

    def __init__(self, *, coverage_window: timedelta = DEFAULT_COVERAGE_WINDOW) -> None:
        self._window = coverage_window
        self._frames: dict[tuple[str, FaceLabel], ThermalFrame] = {}

    def register(self, frame: ThermalFrame) -> ThermalFrame:
        """Register a frame to a face. The newest frame per face wins.

        Raises:
            ValidationError: for a frame with no regions, which is not a
                measurement of anything.
        """
        if not frame.region_temps_c:
            raise ValidationError(
                "a thermal frame must carry at least one region temperature",
                details={"frame_id": frame.frame_id},
            )
        key = (frame.incident_id, frame.face)
        existing = self._frames.get(key)
        if existing is None or frame.observed_at >= existing.observed_at:
            self._frames[key] = frame
        return self._frames[key]

    def frame_for(self, incident_id: str, face: FaceLabel) -> ThermalFrame | None:
        return self._frames.get((incident_id, face))

    def coverage(self, incident_id: str, *, now: datetime) -> tuple[FaceCoverage, ...]:
        """Coverage for all four faces, in fireground order.

        Every face appears. A face with no current frame appears as UNSCANNED,
        which is the whole point of returning all four rather than only the ones
        that were flown.
        """
        report: list[FaceCoverage] = []
        for face in (FaceLabel.ALPHA, FaceLabel.BRAVO, FaceLabel.CHARLIE, FaceLabel.DELTA):
            frame = self._frames.get((incident_id, face))
            if frame is None or not frame.is_current(now, self._window):
                reason = "no coverage" if frame is None else "coverage lapsed"
                report.append(
                    FaceCoverage(
                        face=face,
                        scanned=False,
                        render=f"UNSCANNED - {reason}. {THERMAL_CAVEAT}",
                    )
                )
                continue
            report.append(
                FaceCoverage(
                    face=face,
                    scanned=True,
                    observed_at=frame.observed_at,
                    peak_c=frame.peak_c,
                    coverage=frame.coverage,
                    render=(
                        f"{frame.peak_c:.0f} C peak surface temperature, "
                        f"{frame.coverage * 100:.0f}% of the face, "
                        f"observed {frame.observed_at.isoformat()}. {THERMAL_CAVEAT}"
                    ),
                )
            )
        return tuple(report)

    def voids(self, incident_id: str, *, now: datetime) -> tuple[VoidObservation, ...]:
        """Deterministic void detection across current frames.

        A region that is more than :data:`VOID_DELTA_C` warmer than the region
        beside it, and above the absolute floor. Fixed threshold, fixed
        comparison, no model -- so two runs over the same frames report the same
        observations, and an officer can check the arithmetic.
        """
        found: list[VoidObservation] = []
        for face in (FaceLabel.ALPHA, FaceLabel.BRAVO, FaceLabel.CHARLIE, FaceLabel.DELTA):
            frame = self._frames.get((incident_id, face))
            if frame is None or not frame.is_current(now, self._window):
                continue
            temps = frame.region_temps_c
            for index in range(1, len(temps)):
                delta = temps[index] - temps[index - 1]
                if delta >= VOID_DELTA_C and temps[index] >= VOID_FLOOR_C:
                    found.append(
                        VoidObservation(
                            face=face,
                            region_index=index,
                            delta_c=round(delta, 1),
                            peak_c=frame.peak_c,
                            observed_at=frame.observed_at,
                        )
                    )
        return tuple(found)

    def apply_to_geometry(
        self, spec: GeometrySpec, incident_id: str, *, now: datetime
    ) -> GeometrySpec:
        """Return the spec with thermal readings attached to its faces.

        Faces with no current frame keep :class:`UnscannedValue`. The geometry
        model has no way to express "cool by default", so this cannot silently
        fill one in.
        """
        by_face = {c.face: c for c in self.coverage(incident_id, now=now)}
        faces: list[Face] = []
        for face in spec.faces:
            report = by_face.get(face.label)
            if report is None or not report.scanned or report.peak_c is None:
                faces.append(face.model_copy(update={"thermal": UnscannedValue()}))
                continue
            faces.append(
                face.model_copy(
                    update={
                        "thermal": QuantityValue(magnitude=report.peak_c, unit="C"),
                        "observed_at": report.observed_at,
                    }
                )
            )
        return spec.model_copy(update={"faces": tuple(faces)})

    def unavailable(self, spec: GeometrySpec, *, source_id: str, reason: str) -> GeometrySpec:
        """Mark every face as UNAVAILABLE because the sensor feed is down.

        Distinct from UNSCANNED: "the drone is offline" and "nobody flew that
        side" are different operational facts, and neither is "it is cool".
        """
        return spec.model_copy(
            update={
                "faces": tuple(
                    face.model_copy(
                        update={
                            "thermal": UnavailableValue(source_id=source_id, reason=reason),
                            "observed_at": None,
                        }
                    )
                    for face in spec.faces
                )
            }
        )

    @property
    def frame_count(self) -> int:
        return len(self._frames)


def unscanned_faces(coverage: Sequence[FaceCoverage]) -> tuple[FaceLabel, ...]:
    """Which faces nobody has current coverage of."""
    return tuple(report.face for report in coverage if not report.scanned)
