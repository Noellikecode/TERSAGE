"""The drone sweep: what it flies, what it refuses, and what it must not invent.

The sweep exists because **Sensor Fusion** was correct and idle -- it could read
a frame and register a thermal observation, and nothing ever handed it one
unless an operator pressed a button per wall. These tests hold the two
properties that make an automatic sweep safe rather than merely convenient:

* it refuses to send generated frames to a real vision model, and
* it plans from the measured footprint rather than from compass points.
"""

from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime

from firstdue.domain.enums import AssertionStatus, FaceLabel, SourceType
from firstdue.domain.geometry import Face, GeometrySpec, Level, RoofSegment, resolve_face
from firstdue.incident.drone import (
    SWEEP_ORDER,
    SYNTHETIC_SOURCE,
    camera_bearing_for,
    next_face,
    sweep_permitted,
    synthetic_frame,
)

NOW = datetime(2026, 8, 21, 3, 14, tzinfo=UTC)

#: A plain rectangle: four unambiguous walls, so a bearing that fails to
#: resolve is the planner's fault and not the parcel's.
FOOTPRINT = ((0.0, 0.0), (20.0, 0.0), (20.0, 10.0), (0.0, 10.0))


def _spec(footprint: tuple[tuple[float, float], ...] | None = None) -> GeometrySpec:
    points = footprint if footprint is not None else FOOTPRINT
    faces = tuple(
        Face(label=label)
        for label in (FaceLabel.ALPHA, FaceLabel.BRAVO, FaceLabel.CHARLIE, FaceLabel.DELTA)
    )
    return GeometrySpec(
        address_id="sf-0450-hayes",
        generated_at=NOW,
        footprint=points,
        levels=(
            Level(height_m=3.2, provenance=SourceType.PERMIT, status=AssertionStatus.CONFIRMED),
        ),
        roof_segments=(RoofSegment(pitch_deg=18.0, azimuth_deg=210.0),),
        faces=faces,
        collapse_zone_radius_m=9.6,
    )


class TestTheRefusal:
    def test_a_synthetic_sweep_is_refused_against_a_live_model(self) -> None:
        """The frames are generated. A real model reading one produces a real
        reading of an imaginary building, and nothing on screen distinguishes
        that from a reading of the real one."""
        reason = sweep_permitted(vision_model_ref="gemini-3.5-flash")
        assert reason
        assert "generated" in reason

    def test_and_permitted_against_the_deterministic_double(self) -> None:
        assert sweep_permitted(vision_model_ref="fake/vision-1") == ""


class TestThePlan:
    def test_a_camera_bearing_resolves_back_to_the_face_it_aimed_at(self) -> None:
        """The round trip is the whole contract. A bearing computed here that
        `resolve_face` maps to a different wall would paint a measured
        temperature onto a side nobody photographed."""
        spec = _spec()
        for face in SWEEP_ORDER:
            bearing = camera_bearing_for(face, spec)
            assert bearing is not None
            resolved = resolve_face(bearing, spec.footprint)
            assert resolved is not None
            assert resolved.label is face

    def test_the_sweep_advances_and_then_reports_itself_done(self) -> None:
        spec = _spec()
        scanned: set[FaceLabel] = set()
        flown: list[FaceLabel] = []
        while (face := next_face(frozenset(scanned), spec)) is not None:
            flown.append(face)
            scanned.add(face)
        assert flown == list(SWEEP_ORDER)

    def test_a_sweep_terminates_on_a_footprint_that_is_not_a_rectangle(self) -> None:
        """`face_geometries` labels *any* footprint with four faces -- it works
        from the bounding rectangle, so a triangular parcel still has an Alpha
        through Delta. The property that matters is therefore termination, not
        skipping: whatever the shape, the sweep ends."""
        triangle = ((0.0, 0.0), (12.0, 0.0), (6.0, 9.0))
        spec = _spec(triangle)
        scanned: set[FaceLabel] = set()
        for _ in range(len(SWEEP_ORDER) + 1):
            face = next_face(frozenset(scanned), spec)
            if face is None:
                break
            scanned.add(face)
        assert next_face(frozenset(scanned), spec) is None
        assert len(scanned) <= len(SWEEP_ORDER)


class TestTheFrames:
    def test_a_frame_is_a_real_decodable_png(self) -> None:
        """Random bytes would satisfy the fake client and fail the live one on
        the day somebody points this at real imagery."""
        frame = synthetic_frame(address_id="sf-0450-hayes", face=FaceLabel.ALPHA)
        assert frame[:8] == b"\x89PNG\r\n\x1a\n"
        start = frame.index(b"IDAT")
        length = struct.unpack(">I", frame[start - 4 : start])[0]
        pixels = zlib.decompress(frame[start + 4 : start + 4 + length])
        # One filter byte plus three channels per pixel, per scanline.
        assert len(pixels) % (1 + 3) == 0

    def test_frames_are_deterministic_per_face_and_differ_between_faces(self) -> None:
        """Deterministic so a seeded run replays; distinct so the fake client
        derives a different reading per wall instead of one temperature painted
        the whole way round."""
        first = synthetic_frame(address_id="sf-0450-hayes", face=FaceLabel.ALPHA)
        again = synthetic_frame(address_id="sf-0450-hayes", face=FaceLabel.ALPHA)
        other_face = synthetic_frame(address_id="sf-0450-hayes", face=FaceLabel.BRAVO)
        other_address = synthetic_frame(address_id="sf-0415-mission", face=FaceLabel.ALPHA)
        assert first == again
        assert first != other_face
        assert first != other_address

    def test_the_synthetic_label_is_one_string(self) -> None:
        """So a search for it finds every generated reading in a run."""
        assert SYNTHETIC_SOURCE == "synthetic-drone"
