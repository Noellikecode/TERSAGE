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
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import AssertionStatus, FaceLabel, SourceType
from firstdue.domain.geometry import (
    Face,
    GeometrySpec,
    Level,
    Obstruction,
    ObstructionType,
    collapse_zone_radius,
    resolve_face,
)
from firstdue.domain.values import QuantityValue, UnavailableValue, UnscannedValue
from firstdue.domain.vision import ObservationKind, VisionResult
from firstdue.errors import ValidationError
from firstdue.observability.logging import get_logger
from firstdue.ports.vision import VisionClient

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

#: Words in an obstruction reading that map to a modelled obstruction type.
#: Matched deterministically. Anything the map does not recognise is kept as an
#: observation and does **not** become an obstruction on the massing model --
#: the renderer only ever shows a type this system defines.
_OBSTRUCTION_WORDS: Final[tuple[tuple[str, ObstructionType], ...]] = (
    ("solar", ObstructionType.SOLAR_ARRAY),
    ("photovoltaic", ObstructionType.SOLAR_ARRAY),
    ("hvac", ObstructionType.HVAC_UNIT),
    ("air handler", ObstructionType.HVAC_UNIT),
    ("skylight", ObstructionType.SKYLIGHT),
    ("parapet", ObstructionType.PARAPET),
    ("antenna", ObstructionType.ANTENNA),
    ("satellite", ObstructionType.ANTENNA),
    ("ev charger", ObstructionType.EV_CHARGER),
)


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


class FrameRejected(BaseModel):
    """Why a frame produced no reading. Never silently dropped.

    A frame the system could not use is an operational fact an officer needs:
    it means that wall is still UNSCANNED and somebody should fly it again.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(min_length=1, max_length=300)
    #: True when the cause is that the slow loop never profiled this address.
    cold_start: bool = False


class FrameAnalysis(BaseModel):
    """What one frame produced, end to end."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frame: ThermalFrame | None = None
    #: Structural readings the frame supports, for the profile and the massing
    #: model. Advisory: the conflict engine decides what they mean.
    observed_storeys: int | None = Field(default=None, ge=1, le=200)
    obstructions: tuple[ObstructionType, ...] = ()
    #: What the model said it could not settle, carried through verbatim.
    unknowns: tuple[str, ...] = ()
    rejected: FrameRejected | None = None
    model_ref: str = Field(default="", max_length=120)

    @property
    def registered(self) -> bool:
        return self.frame is not None


class SensorFusion:
    """Registers thermal frames to faces and reports coverage honestly."""

    def __init__(
        self,
        *,
        coverage_window: timedelta = DEFAULT_COVERAGE_WINDOW,
        vision: VisionClient | None = None,
        ids: Any | None = None,
    ) -> None:
        self._window = coverage_window
        self._frames: dict[tuple[str, FaceLabel], ThermalFrame] = {}
        #: Optional. Without it the agent still registers pre-extracted frames,
        #: which is what a ground station that already did the extraction
        #: sends. With it, the agent does the extraction itself.
        self._vision = vision
        self._ids = ids

    async def analyze_frame(
        self,
        *,
        incident_id: str,
        image: bytes,
        mime_type: str,
        camera_bearing_deg: float,
        observed_at: datetime,
        spec: GeometrySpec | None,
        deadline_ms: int = 8_000,
        source: str = "recorded",
    ) -> FrameAnalysis:
        """Imagery in, registered thermal frame out. The autonomous path.

        Four steps, and the order matters:

        1. **Resolve the face from the slow loop's geometry.** The footprint
           the Geometry Watcher measured decides which wall this camera is
           pointed at. Nothing else does -- not the caller, not the model.
        2. **Read the frame.** Gemini returns observations bound to image
           regions. It is not asked which wall it is looking at.
        3. **Order the regions bottom to top**, because the void rule compares
           adjacent regions and "adjacent" has to mean adjacent in the world
           rather than in whatever order the model happened to emit.
        4. **Build the frame** the existing deterministic machinery already
           consumes, so void detection, coverage expiry and UNSCANNED work
           exactly as they did.

        **Cold start is a refusal, not a guess.** With no profile there is no
        footprint, no footprint means no face bearings, and a frame that cannot
        be attributed to a wall is not registered at all. That is the two-loop
        dependency made load-bearing: this agent cannot do its job unless the
        slow loop already did its own.
        """
        if spec is None:
            return FrameAnalysis(
                rejected=FrameRejected(
                    reason=(
                        "no pre-incident geometry for this address, so a frame cannot "
                        "be attributed to a face"
                    ),
                    cold_start=True,
                )
            )

        face_geometry = resolve_face(camera_bearing_deg, spec.footprint)
        if face_geometry is None:
            return FrameAnalysis(
                rejected=FrameRejected(
                    reason=(
                        f"camera bearing {camera_bearing_deg:.0f} does not resolve to a "
                        "single face of the measured footprint"
                    )
                )
            )

        if self._vision is None:
            return FrameAnalysis(rejected=FrameRejected(reason="no imagery model is configured"))

        result = await self._vision.observe(
            image=image, mime_type=mime_type, deadline_ms=deadline_ms
        )
        if not result.accepted:
            return FrameAnalysis(
                rejected=FrameRejected(reason=result.rejection_reason or "frame rejected"),
                model_ref=result.model_ref,
            )

        temps = _temperatures_bottom_up(result)
        frame: ThermalFrame | None = None
        if temps:
            frame = self.register(
                ThermalFrame(
                    frame_id=self._new_frame_id(),
                    incident_id=incident_id,
                    face=face_geometry.label,
                    observed_at=observed_at,
                    region_temps_c=temps,
                    coverage=_coverage_of(result),
                    source=source,
                )
            )

        return FrameAnalysis(
            frame=frame,
            observed_storeys=_observed_storeys(result),
            obstructions=_obstructions(result),
            unknowns=result.unknowns,
            model_ref=result.model_ref,
        )

    def _new_frame_id(self) -> str:
        if self._ids is not None:
            ref: str = self._ids.new_id("frame")
            return ref
        # Derived from the frames already held, so a run without an id
        # generator is still deterministic and replayable.
        return f"frame_{len(self._frames) + 1:06d}"

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

    def apply_analysis_to_geometry(
        self, spec: GeometrySpec, analysis: FrameAnalysis, *, incident_id: str, now: datetime
    ) -> GeometrySpec:
        """Fold one frame's structural observations into the massing model.

        Thermal goes on the faces exactly as before. What is new is that an
        obstruction the frame showed is added to the model, and a storey count
        the frame supports that **exceeds** what the slow loop filed marks the
        extra mass ``DISPUTED``.

        Note what does not happen: an observed count *lower* than the filed one
        removes nothing. Imagery is one viewpoint of one face -- a storey the
        camera could not see from the street is not a storey that is not there,
        and a massing model that shrank because of a bad angle would delete
        exactly the mass a crew needs to know about.
        """
        spec = self.apply_to_geometry(spec, incident_id, now=now)

        obstructions = list(spec.obstructions)
        if spec.roof_segments:
            known = {o.type for o in obstructions}
            obstructions.extend(
                Obstruction(
                    type=kind,
                    segment_index=0,
                    provenance=SourceType.STREET_VIEW,
                    status=AssertionStatus.CONFIRMED,
                )
                for kind in analysis.obstructions
                if kind not in known
            )

        levels = list(spec.levels)
        observed = analysis.observed_storeys
        if observed is not None and levels and observed > len(levels):
            storey_height = levels[-1].height_m
            levels.extend(
                Level(
                    height_m=storey_height,
                    provenance=SourceType.STREET_VIEW,
                    status=AssertionStatus.DISPUTED,
                )
                for _ in range(observed - len(levels))
            )

        total_height = sum(level.height_m for level in levels)
        return spec.model_copy(
            update={
                "levels": tuple(levels),
                "obstructions": tuple(obstructions),
                "collapse_zone_radius_m": collapse_zone_radius(total_height),
            }
        )

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


def _temperatures_bottom_up(result: VisionResult) -> tuple[float, ...]:
    """Region temperatures ordered from the ground up.

    The void rule compares each region with the one before it, so the sequence
    has to be spatial. A model emitting regions in reading order -- top-left
    first -- would otherwise make the *roof* the baseline and turn a normal
    building into a void on every frame.

    Ordered by the bottom edge of each region descending, because image y grows
    downward: the largest ``y + height`` is closest to the ground.
    """
    thermal = [
        o for o in result.of_kind(ObservationKind.THERMAL_REGION) if o.temperature_c is not None
    ]
    thermal.sort(key=lambda o: o.region.y + o.region.height, reverse=True)
    return tuple(o.temperature_c for o in thermal if o.temperature_c is not None)


def _coverage_of(result: VisionResult) -> float:
    """Fraction of the frame the thermal regions actually span.

    Summed heights rather than area, and capped at one. It is a coverage
    figure an officer reads next to "UNSCANNED", so overstating it is the one
    thing it must not do.
    """
    heights = sum(o.region.height for o in result.of_kind(ObservationKind.THERMAL_REGION))
    return round(min(1.0, max(0.0, heights)), 3)


def _observed_storeys(result: VisionResult) -> int | None:
    """Storeys the frame supports, by counting bands. ``None`` when it saw none.

    Counting, not asking. The model reports bands of openings it can point at;
    how many storeys that implies is arithmetic here, so a model cannot answer
    "three storeys" without having shown three bands.
    """
    bands = result.of_kind(ObservationKind.STOREY_BAND)
    return len(bands) or None


def _obstructions(result: VisionResult) -> tuple[ObstructionType, ...]:
    """Obstruction readings mapped to modelled types, in a stable order.

    A reading this system has no type for is deliberately dropped rather than
    rendered: the massing model shows only obstruction types it defines, so a
    model cannot invent a new thing on the roof.
    """
    found: list[ObstructionType] = []
    for observation in result.of_kind(ObservationKind.OBSTRUCTION):
        lowered = observation.raw_value.lower()
        for word, kind in _OBSTRUCTION_WORDS:
            if word in lowered and kind not in found:
                found.append(kind)
                break
    return tuple(found)


def unscanned_faces(coverage: Sequence[FaceCoverage]) -> tuple[FaceLabel, ...]:
    """Which faces nobody has current coverage of."""
    return tuple(report.face for report in coverage if not report.scanned)
