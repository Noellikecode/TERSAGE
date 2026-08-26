"""A drone sweep: four faces, one frame each, through the real fusion agent.

The **Sensor Fusion** agent has always been able to read a frame and register a
thermal observation. What it never had was anything flying. Frames arrived only
when an operator pressed a button per face, so an incident nobody hand-fed
showed four UNSCANNED walls and a fleet card reading zero -- the agent was
working correctly and had nothing to work on.

This module is the flight. It plans a sweep from the footprint the **Geometry
Watcher** measured, and hands one frame at a time to the same
``analyze_imagery`` path a ground station would use. Nothing here reads a
frame, decides a temperature, or touches the brief: it produces bytes and a
bearing, and the agent does the rest. That separation is the point -- a sweep
planner that also authored readings would be a second vision model nobody
reviewed.

**The frames are synthetic and say so.** Every frame this module produces is
generated from a digest of the address and the face; it is not a photograph of
anything. It carries ``source="synthetic-drone"`` all the way into the incident
log, and :func:`sweep_permitted` refuses to run the sweep at all against a live
vision model -- sending a generated picture to Gemini would produce a real
reading of an imaginary building, and the console would have no way to tell that
from a real one. In live mode the answer is a stated refusal, not a quieter fake.

The build-up an officer sees on the massing model is therefore real in the only
sense that matters here: the coverage, the void arithmetic and the amended brief
are all computed by the agent from the frames it was given, in the order it was
given them.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from typing import Final

from firstdue.domain.enums import FaceLabel
from firstdue.domain.geometry import GeometrySpec, face_geometries

#: What every frame from this module is labelled, in the log and on screen.
#: One string, so a search for it finds every synthetic reading in a run.
SYNTHETIC_SOURCE: Final[str] = "synthetic-drone"

#: The sweep order. Alpha first because it is the address side and the side an
#: arriving company sees; then clockwise, which is how a fireground is walked.
SWEEP_ORDER: Final[tuple[FaceLabel, ...]] = (
    FaceLabel.ALPHA,
    FaceLabel.BRAVO,
    FaceLabel.CHARLIE,
    FaceLabel.DELTA,
)

#: Frame size. Small on purpose: the fake vision client digests the bytes and
#: never looks at the picture, so pixels beyond what makes each face's frame
#: distinct are bytes moved for nothing.
_FRAME_PX: Final[int] = 64


def sweep_permitted(*, vision_model_ref: str) -> str:
    """Empty when a synthetic sweep is honest here, else the reason it is not.

    The check is on the vision client's own model reference rather than on a
    settings flag, because the question is precisely "would a real model be
    asked to read a generated picture" and the client is what answers it.
    """
    if vision_model_ref.startswith("fake/"):
        return ""
    return (
        "a synthetic drone sweep is refused against a live vision model: the "
        "frames are generated, and a real reading of a generated building is "
        "indistinguishable on screen from a real reading of a real one. Fly a "
        "real aircraft and post its frames to /frames instead."
    )


def camera_bearing_for(face: FaceLabel, spec: GeometrySpec) -> float | None:
    """Where a camera must point to be looking at ``face``, or ``None``.

    A face's stored bearing is its **outward normal**; a camera looking at it
    points the opposite way, which is the same reversal
    :func:`~firstdue.domain.geometry.resolve_face` applies when it reads a
    bearing back. Computing it here from the same face geometry -- rather than
    from a table of compass points -- is what keeps a sweep resolving to the
    face it aimed at on a parcel that is not a rectangle.

    ``None`` means the footprint resolved to no such face. With today's
    normalisation that cannot happen -- see :func:`next_face` -- so it is a
    guard rather than a case, and the caller reports it as a stated reason
    rather than aiming the camera somewhere plausible.
    """
    for geometry in face_geometries(spec.footprint):
        if geometry.label is face:
            return (geometry.bearing_deg + 180.0) % 360.0
    return None


def _chunk(tag: bytes, payload: bytes) -> bytes:
    """One PNG chunk: length, tag, payload, CRC over tag and payload."""
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


def synthetic_frame(*, address_id: str, face: FaceLabel) -> bytes:
    """A deterministic PNG standing in for one drone frame.

    Deterministic in the same sense as every other fake in this system: the
    same address and face always produce the same bytes, so a seeded demo is
    reproducible and a replay is byte-identical. Different faces produce
    genuinely different bytes, which is what makes the fake vision client
    derive a different reading per wall instead of painting one temperature
    around the building.

    A real PNG rather than random bytes, because the live path decodes what it
    is handed and a fixture that only works against the fake is a fixture that
    hides a decode bug until the day it matters.
    """
    seed = hashlib.sha256(f"{address_id}:{face}".encode()).digest()

    rows = bytearray()
    for y in range(_FRAME_PX):
        rows.append(0)  # PNG filter type 0 for this scanline
        for x in range(_FRAME_PX):
            # A smooth vertical gradient with a per-face offset. The picture is
            # not meant to be looked at; it is meant to be a stable, distinct
            # sequence of bytes per face.
            base = seed[(x + y) % len(seed)]
            rows.append((base + y * 3) % 256)
            rows.append((base + x * 2) % 256)
            rows.append(base)

    header = struct.pack(">IIBBBBB", _FRAME_PX, _FRAME_PX, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + _chunk(b"IEND", b"")
    )


def next_face(scanned: frozenset[FaceLabel], spec: GeometrySpec) -> FaceLabel | None:
    """The next face to fly, or ``None`` when the sweep is done.

    Filtered against the faces the footprint actually resolves to. Today that
    filter never removes anything -- :func:`face_geometries` labels every
    footprint with all four faces, working from its bounding rectangle, so even
    a triangular parcel has an Alpha through Delta. It is kept because the
    alternative is a sweep that assumes four walls, and the day that
    normalisation changes is the day such a sweep spins for ever waiting for a
    wall nobody can photograph.
    """
    available = {geometry.label for geometry in face_geometries(spec.footprint)}
    for face in SWEEP_ORDER:
        if face in available and face not in scanned:
            return face
    return None
