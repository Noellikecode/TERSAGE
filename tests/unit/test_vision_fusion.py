"""Imagery in, massing model out -- and the refusals along the way.

The Sensor Fusion agent now does its own extraction: a frame arrives, Gemini
reports observations bound to image regions, and deterministic code turns those
into a registered thermal frame and an amended massing model.

What these tests mostly hold is the *refusals*, because that is where an
imagery pipeline becomes dangerous. A frame attributed to the wrong wall paints
a measured temperature onto a side nobody photographed, and an officer reads
that as coverage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.adapters.fake.vision import FakeVisionClient
from firstdue.domain.enums import AssertionStatus, FaceLabel, SourceType
from firstdue.domain.geometry import (
    Face,
    GeometrySpec,
    Level,
    ObstructionType,
    RoofSegment,
    face_geometries,
    resolve_face,
)
from firstdue.domain.values import UnscannedValue
from firstdue.domain.vision import (
    ImageRegion,
    ObservationKind,
    VisionObservation,
    VisionResult,
)
from firstdue.incident.fusion import SensorFusion

NOW = datetime(2026, 8, 21, 3, 14, tzinfo=UTC)

#: 20m x 10m, wound counter-clockwise. Long walls run east-west, so Alpha's
#: outward normal is north and a camera looking south sees it.
FOOTPRINT = ((0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0))


def spec(levels: int = 2, roof: bool = True) -> GeometrySpec:
    return GeometrySpec(
        address_id="sf-0450-hayes",
        generated_at=NOW,
        footprint=FOOTPRINT,
        levels=tuple(
            Level(height_m=3.2, provenance=SourceType.PERMIT, status=AssertionStatus.CONFIRMED)
            for _ in range(levels)
        ),
        roof_segments=(RoofSegment(pitch_deg=18.0, azimuth_deg=210.0),) if roof else (),
        faces=tuple(
            Face(label=label)
            for label in (FaceLabel.ALPHA, FaceLabel.BRAVO, FaceLabel.CHARLIE, FaceLabel.DELTA)
        ),
        collapse_zone_radius_m=9.6,
    )


class StubVision:
    """A vision client that returns exactly what a test hands it."""

    model_ref = "stub/vision"

    def __init__(self, result: VisionResult) -> None:
        self._result = result
        self.calls = 0

    async def observe(self, *, image: bytes, mime_type: str, deadline_ms: int) -> VisionResult:
        self.calls += 1
        return self._result


def thermal(*pairs: tuple[float, float]) -> VisionResult:
    """A result with thermal regions given as ``(y, celsius)``."""
    return VisionResult(
        observations=tuple(
            VisionObservation(
                kind=ObservationKind.THERMAL_REGION,
                region=ImageRegion(x=0.0, y=y, width=1.0, height=0.3),
                raw_value=f"{celsius}C",
                model_confidence=0.8,
            )
            for y, celsius in pairs
        ),
        model_ref="stub/vision",
    )


class TestTheSlowLoopDecidesTheFace:
    """The Geometry Watcher's footprint is what resolves a frame to a wall.

    This is the load-bearing dependency between the two loops. The incident
    loop cannot attribute a frame to a face on its own, and it is not allowed
    to guess.
    """

    async def test_a_bearing_resolves_through_the_measured_footprint(self) -> None:
        """A camera looking south sees the north-facing wall."""
        fusion = SensorFusion(vision=StubVision(thermal((0.6, 300.0), (0.0, 340.0))))
        analysis = await fusion.analyze_frame(
            incident_id="inc-1",
            image=b"jpegbytes",
            mime_type="image/jpeg",
            camera_bearing_deg=180.0,
            observed_at=NOW,
            spec=spec(),
        )
        assert analysis.registered
        assert analysis.frame is not None
        assert analysis.frame.face is FaceLabel.ALPHA

    async def test_no_profile_means_no_registration_at_all(self) -> None:
        """Cold start is a refusal, not a guess.

        With no slow-loop profile there is no footprint, and with no footprint
        a frame cannot be attributed to any wall. Registering it anyway would
        put a temperature on a face nobody measured.
        """
        fusion = SensorFusion(vision=StubVision(thermal((0.0, 400.0))))
        analysis = await fusion.analyze_frame(
            incident_id="inc-1",
            image=b"jpegbytes",
            mime_type="image/jpeg",
            camera_bearing_deg=180.0,
            observed_at=NOW,
            spec=None,
        )
        assert not analysis.registered
        assert analysis.rejected is not None
        assert analysis.rejected.cold_start is True

    async def test_a_corner_shot_is_refused_rather_than_assigned(self) -> None:
        """Equidistant from two walls is not evidence about either one."""
        vision = StubVision(thermal((0.0, 400.0)))
        fusion = SensorFusion(vision=vision)
        analysis = await fusion.analyze_frame(
            incident_id="inc-1",
            image=b"jpegbytes",
            mime_type="image/jpeg",
            camera_bearing_deg=45.0,
            observed_at=NOW,
            spec=spec(),
        )
        assert not analysis.registered
        assert analysis.rejected is not None
        assert analysis.rejected.cold_start is False
        # Refused before spending a model call: geometry is checked first.
        assert vision.calls == 0

    def test_alpha_is_the_longest_wall_and_the_rest_follow_clockwise(self) -> None:
        faces = face_geometries(FOOTPRINT)
        assert [f.label for f in faces] == [
            FaceLabel.ALPHA,
            FaceLabel.BRAVO,
            FaceLabel.CHARLIE,
            FaceLabel.DELTA,
        ]
        bearings = [f.bearing_deg for f in faces]
        assert bearings == [0.0, 90.0, 180.0, 270.0]

    def test_labelling_is_stable_across_runs_of_the_same_footprint(self) -> None:
        """A square has four equal walls and must still label identically.

        An Alpha that moved between two renders of one building would be worse
        than an Alpha that is arguable.
        """
        square = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
        assert face_geometries(square) == face_geometries(square)
        assert resolve_face(180.0, square) == resolve_face(180.0, square)


class TestRegionOrderIsSpatialNotReadingOrder:
    """The void rule compares adjacent regions, so the order has to be real."""

    async def test_regions_are_ordered_from_the_ground_up(self) -> None:
        """A model emitting top-first must not make the roof the baseline.

        Image y grows downward, so the largest ``y + height`` is closest to the
        ground. Emitted here top-first on purpose.
        """
        fusion = SensorFusion(vision=StubVision(thermal((0.0, 340.0), (0.35, 120.0), (0.7, 20.0))))
        analysis = await fusion.analyze_frame(
            incident_id="inc-1",
            image=b"jpegbytes",
            mime_type="image/jpeg",
            camera_bearing_deg=180.0,
            observed_at=NOW,
            spec=spec(),
        )
        assert analysis.frame is not None
        assert analysis.frame.region_temps_c == (20.0, 120.0, 340.0)

    async def test_a_hot_cockloft_over_a_cool_ground_floor_is_a_void(self) -> None:
        """The finding the agent exists for, through the imagery path."""
        fusion = SensorFusion(vision=StubVision(thermal((0.7, 20.0), (0.35, 120.0), (0.0, 340.0))))
        await fusion.analyze_frame(
            incident_id="inc-1",
            image=b"jpegbytes",
            mime_type="image/jpeg",
            camera_bearing_deg=180.0,
            observed_at=NOW,
            spec=spec(),
        )
        voids = fusion.voids("inc-1", now=NOW)
        assert voids
        assert voids[0].face is FaceLabel.ALPHA
        assert "cannot see through walls" in voids[0].render


class TestUnparseableReadingsAreDiscardedNotCoerced:
    async def test_a_garbled_temperature_never_becomes_a_number(self) -> None:
        result = VisionResult(
            observations=(
                VisionObservation(
                    kind=ObservationKind.THERMAL_REGION,
                    region=ImageRegion(x=0.0, y=0.0, width=1.0, height=1.0),
                    raw_value="very hot",
                    model_confidence=0.9,
                ),
            ),
            model_ref="stub/vision",
        )
        fusion = SensorFusion(vision=StubVision(result))
        analysis = await fusion.analyze_frame(
            incident_id="inc-1",
            image=b"jpegbytes",
            mime_type="image/jpeg",
            camera_bearing_deg=180.0,
            observed_at=NOW,
            spec=spec(),
        )
        # No parseable region means no frame, which means the face stays
        # UNSCANNED rather than acquiring an invented temperature.
        assert not analysis.registered

    @pytest.mark.parametrize("raw", ["-273.0C", "9000C", "hot", "3 0 0 C", "NaNC"])
    def test_physically_impossible_readings_do_not_parse(self, raw: str) -> None:
        """Out of band, or not a number at all. Both discard rather than coerce."""
        observation = VisionObservation(
            kind=ObservationKind.THERMAL_REGION,
            region=ImageRegion(x=0.0, y=0.0, width=1.0, height=1.0),
            raw_value=raw,
            model_confidence=0.9,
        )
        assert observation.temperature_c is None


class TestTheMassingModelIsAmendedNotAuthored:
    """Vision contributes facts. Geometry stays the slow loop's product."""

    async def test_an_observed_extra_storey_arrives_disputed(self) -> None:
        result = VisionResult(
            observations=tuple(
                VisionObservation(
                    kind=ObservationKind.STOREY_BAND,
                    region=ImageRegion(x=0.1, y=0.1 * i, width=0.8, height=0.1),
                    raw_value=f"row of windows {i}",
                    model_confidence=0.6,
                )
                for i in range(3)
            ),
            model_ref="stub/vision",
        )
        fusion = SensorFusion(vision=StubVision(result))
        analysis = await fusion.analyze_frame(
            incident_id="inc-1",
            image=b"jpegbytes",
            mime_type="image/jpeg",
            camera_bearing_deg=180.0,
            observed_at=NOW,
            spec=spec(levels=2),
        )
        assert analysis.observed_storeys == 3

        amended = fusion.apply_analysis_to_geometry(
            spec(levels=2), analysis, incident_id="inc-1", now=NOW
        )
        assert len(amended.levels) == 3
        assert amended.levels[-1].status is AssertionStatus.DISPUTED
        assert amended.levels[-1].provenance is SourceType.STREET_VIEW
        assert amended.has_disputed_mass

    async def test_seeing_fewer_storeys_never_removes_mass(self) -> None:
        """One viewpoint of one face is not evidence a storey is absent.

        A massing model that shrank because of a bad camera angle would delete
        exactly the mass a crew most needs to know about.
        """
        result = VisionResult(
            observations=(
                VisionObservation(
                    kind=ObservationKind.STOREY_BAND,
                    region=ImageRegion(x=0.1, y=0.5, width=0.8, height=0.1),
                    raw_value="one row of windows",
                    model_confidence=0.6,
                ),
            ),
            model_ref="stub/vision",
        )
        fusion = SensorFusion(vision=StubVision(result))
        analysis = await fusion.analyze_frame(
            incident_id="inc-1",
            image=b"jpegbytes",
            mime_type="image/jpeg",
            camera_bearing_deg=180.0,
            observed_at=NOW,
            spec=spec(levels=3),
        )
        amended = fusion.apply_analysis_to_geometry(
            spec(levels=3), analysis, incident_id="inc-1", now=NOW
        )
        assert len(amended.levels) == 3

    async def test_an_unmodelled_obstruction_is_not_rendered(self) -> None:
        """The renderer shows only obstruction types this system defines."""
        result = VisionResult(
            observations=(
                VisionObservation(
                    kind=ObservationKind.OBSTRUCTION,
                    region=ImageRegion(x=0.2, y=0.1, width=0.4, height=0.2),
                    raw_value="a large inflatable gorilla",
                    model_confidence=0.95,
                ),
                VisionObservation(
                    kind=ObservationKind.OBSTRUCTION,
                    region=ImageRegion(x=0.2, y=0.1, width=0.4, height=0.2),
                    raw_value="rooftop solar array",
                    model_confidence=0.9,
                ),
            ),
            model_ref="stub/vision",
        )
        fusion = SensorFusion(vision=StubVision(result))
        analysis = await fusion.analyze_frame(
            incident_id="inc-1",
            image=b"jpegbytes",
            mime_type="image/jpeg",
            camera_bearing_deg=180.0,
            observed_at=NOW,
            spec=spec(),
        )
        assert analysis.obstructions == (ObstructionType.SOLAR_ARRAY,)

    async def test_the_collapse_zone_follows_the_amended_height(self) -> None:
        """A disputed third storey widens the ring an officer stands outside."""
        result = VisionResult(
            observations=tuple(
                VisionObservation(
                    kind=ObservationKind.STOREY_BAND,
                    region=ImageRegion(x=0.1, y=0.1 * i, width=0.8, height=0.1),
                    raw_value=f"row {i}",
                    model_confidence=0.6,
                )
                for i in range(3)
            ),
            model_ref="stub/vision",
        )
        fusion = SensorFusion(vision=StubVision(result))
        analysis = await fusion.analyze_frame(
            incident_id="inc-1",
            image=b"jpegbytes",
            mime_type="image/jpeg",
            camera_bearing_deg=180.0,
            observed_at=NOW,
            spec=spec(levels=2),
        )
        before = spec(levels=2)
        after = fusion.apply_analysis_to_geometry(before, analysis, incident_id="inc-1", now=NOW)
        assert after.collapse_zone_radius_m > before.collapse_zone_radius_m


class TestCoverageStillLapses:
    async def test_a_face_returns_to_unscanned_after_the_window(self) -> None:
        """Yesterday's warm wall is not today's warm wall, imagery or not."""
        fusion = SensorFusion(vision=StubVision(thermal((0.0, 340.0))))
        await fusion.analyze_frame(
            incident_id="inc-1",
            image=b"jpegbytes",
            mime_type="image/jpeg",
            camera_bearing_deg=180.0,
            observed_at=NOW,
            spec=spec(),
        )
        later = NOW + timedelta(minutes=30)
        amended = fusion.apply_to_geometry(spec(), "inc-1", now=later)
        alpha = next(f for f in amended.faces if f.label is FaceLabel.ALPHA)
        assert isinstance(alpha.thermal, UnscannedValue)


class TestTheFakeVisionClientIsDeterministic:
    async def test_the_same_frame_always_reads_the_same(self) -> None:
        client = FakeVisionClient()
        first = await client.observe(image=b"frame-a", mime_type="image/jpeg", deadline_ms=1000)
        second = await client.observe(image=b"frame-a", mime_type="image/jpeg", deadline_ms=1000)
        assert first == second

    async def test_different_frames_read_differently(self) -> None:
        client = FakeVisionClient()
        first = await client.observe(image=b"frame-a", mime_type="image/jpeg", deadline_ms=1000)
        second = await client.observe(image=b"frame-b", mime_type="image/jpeg", deadline_ms=1000)
        assert first.temperatures_c != second.temperatures_c

    async def test_an_empty_frame_is_rejected_with_a_reason(self) -> None:
        client = FakeVisionClient()
        result = await client.observe(image=b"", mime_type="image/jpeg", deadline_ms=1000)
        assert not result.accepted
        assert result.rejection_reason
