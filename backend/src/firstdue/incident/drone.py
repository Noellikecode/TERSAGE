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
import math
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

#: Frame size. It was 64 px, "small on purpose", when the only reader was the
#: fake vision client -- which digests the bytes and never looks at the
#: picture. A real model does look, and 256 px is where a drawn scale is
#: legible to it: measured against live Gemini, this frame returns two
#: ``THERMAL_REGION`` observations bound to areas of the wall, and the same
#: scene at 192 px returns two boxes drawn around the *legend* instead. Larger
#: costs latency for nothing -- 384 px measured 9.3 s against 7.1 s -- and
#: latency here is a wall an officer is waiting on.
_FRAME_PX: Final[int] = 256

#: Width of the temperature scale burned into the right-hand edge, in pixels.
#: A real thermal camera writes one into every frame, which is the only reason
#: a number read off one of these images means anything.
_SCALE_PX: Final[int] = 34

#: What the scale runs between. Not a claim about this fire: it is the range
#: the palette is stretched over, exactly as a camera's is, and it is what the
#: two ends of the bar are labelled with.
_SCALE_MIN_C: Final[float] = 18.0
_SCALE_MAX_C: Final[float] = 340.0

#: A 5x7 bitmap font, one string per row, ``1`` where the ink goes. Ten digits
#: and a ``C``, which is every glyph a temperature scale needs.
#:
#: Hand-drawn for the same reason ``adapters/fake/tiles.py`` encodes its own
#: PNGs and ``incident/pdf.py`` writes its own PDFs: this runs on the
#: credential-free path, and a font file or an imaging library added here is a
#: dependency the whole system then has, for eleven glyphs.
_GLYPHS: Final[dict[str, tuple[str, ...]]] = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
}

#: Pixels per glyph cell, and the advance between glyphs.
_GLYPH_SCALE: Final[int] = 2
_GLYPH_ADVANCE: Final[int] = 6


def sweep_permitted(*, vision_model_ref: str, simulation_declared: bool = False) -> str:
    """Empty when a synthetic sweep is honest here, else the reason it is not.

    The check is on the vision client's own model reference rather than on a
    settings flag, because the question is precisely "would a real model be
    asked to read a generated picture" and the client is what answers it.

    **``simulation_declared`` is the operator promising the screen will admit
    it.** Read the refusal below carefully: the objection is not that a frame is
    generated -- every frame in fake mode is, and always has been -- it is that
    a real model's reading of a generated building is *indistinguishable on
    screen* from a real reading of a real one. That is a claim about disclosure,
    and disclosure is a thing a deployment can actually provide.

    So this takes the rule the simulated 911 call already lives by: off by
    default, opt-in at launch and never inferred from the mode, and every record
    it produces carries :data:`SYNTHETIC_SOURCE` so the frame, the log entry,
    the audit step and the console all say what it is. What this module forbids
    is an *unlabelled* reading, not a labelled exercise -- and a department
    watching a demo is entitled to see the agent that reads walls read some.

    Without that declaration the refusal stands exactly as it did.
    """
    if vision_model_ref.startswith("fake/"):
        return ""
    if simulation_declared:
        return ""
    return (
        "a synthetic drone sweep is refused against a live vision model: the "
        "frames are generated, and a real reading of a generated building is "
        "indistinguishable on screen from a real reading of a real one. Fly a "
        "real aircraft and post its frames to /frames instead, or declare the "
        "sweep a simulation so every reading it produces is marked as one."
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


def _ironbow(fraction: float) -> tuple[int, int, int]:
    """The conventional thermal palette: cold is near-black, hot is white.

    The exact curve does not matter and is not a claim about anything. What
    matters is that it is monotonic in temperature and that the same curve
    paints the scale bar, because a reader -- a person or a model -- gets the
    number by matching a colour in the scene against the bar beside it.
    """
    clamped = min(1.0, max(0.0, fraction))
    red = int(255 * min(1.0, clamped * 2.2))
    green = int(255 * min(1.0, max(0.0, clamped * 1.8 - 0.5)))
    blue = int(255 * min(1.0, max(0.0, 1.0 - clamped * 2.4) * 0.7 + max(0.0, clamped - 0.8) * 4.0))
    return red, green, blue


def _draw_text(
    pixels: list[list[tuple[int, int, int]]],
    text: str,
    *,
    left: int,
    top: int,
) -> None:
    """Write one short label into the frame, in white, from :data:`_GLYPHS`.

    Silently skips a glyph it does not carry and clips at the frame edge. Both
    are the right failure for a legend: a label that ran off the edge is worth
    less than a frame that raised on its way to a fireground.
    """
    for index, character in enumerate(text):
        glyph = _GLYPHS.get(character)
        if glyph is None:
            continue
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit != "1":
                    continue
                for offset_y in range(_GLYPH_SCALE):
                    for offset_x in range(_GLYPH_SCALE):
                        x = left + (index * _GLYPH_ADVANCE + column) * _GLYPH_SCALE + offset_x
                        y = top + row * _GLYPH_SCALE + offset_y
                        if 0 <= x < _FRAME_PX and 0 <= y < _FRAME_PX:
                            pixels[y][x] = (255, 255, 255)


def synthetic_frame(*, address_id: str, face: FaceLabel) -> bytes:
    """One simulated thermal frame: a false-colour wall and a burned-in scale.

    Deterministic in the same sense as every other fake in this system: the
    same address and face always produce the same bytes, so a seeded demo is
    reproducible and a replay is byte-identical, and different faces produce
    genuinely different pictures rather than one temperature painted around the
    building.

    **It is drawn to be read, and that is a change.** This used to be a noise
    gradient with a comment saying the picture was "not meant to be looked at",
    which was true while the only reader was the fake vision client -- it
    digests the bytes and derives a reading from the digest. Against the real
    model the same bytes are an unreadable photograph, and Gemini said so
    correctly: zero observations, three times out of three. So a sweep launched
    with ``DEMO_SYNTHETIC_SWEEP`` against live Vertex registered no coverage on
    any wall, ``thermal.coverage`` could never pass, and the agent that reads
    walls spent an incident with nothing to report. Measured on the frame this
    now draws, the same model returns two ``THERMAL_REGION`` observations,
    ``340C`` and ``18C``, identical across calls.

    What is drawn is a hot region or three over a cooler wall, banded every few
    rows where a storey line would be, and a scale bar down the right-hand edge
    labelled at both ends -- which is what a real camera burns into a frame and
    the only reason a number read off one means anything. The positions and
    intensities come from the digest, so this is still a picture of nothing:
    the disclosure that it is synthetic travels with the reading either way,
    all the way into the incident log.

    A real PNG rather than random bytes, because the live path decodes what it
    is handed and a fixture that only works against the fake is a fixture that
    hides a decode bug until the day it matters.
    """
    seed = hashlib.sha256(f"{address_id}:{face}".encode()).digest()

    # A fire has a seat. Every face used to burn.
    #
    # The amplitude was `0.55 + seed/255 * 0.45` on all four walls, and three
    # overlapping hotspots saturate that to 1.0 -- the top of the scale, 340 C.
    # `THERMAL_BARRIER_C` is 300, and `_thermal_terms` builds *no door edge*
    # through a face at or above it, correctly: a wall that hot is not a way
    # in. So the sweep read 340 C on Alpha, Bravo, Charlie and Delta, all four
    # doors became barriers, and the A* solve refused every route -- "no leg of
    # the navigable graph connects the start to the goal". The path was not
    # broken; the synthetic fire was, and only once the sweep started landing
    # frames at all did it show.
    #
    # One seat, picked off the *address* so it is the same wall for every face
    # of one building, and the other three run cooler. The barrier rule is
    # untouched: a crew still gets no door through the seat, and still gets one
    # through the walls that are merely warm, which is the decision this whole
    # graph exists to support.
    seat = hashlib.sha256(address_id.encode()).digest()[0] % len(SWEEP_ORDER)
    is_seat = SWEEP_ORDER[seat] == face
    floor, span = (0.55, 0.45) if is_seat else (0.16, 0.24)

    # Three hot regions, placed and sized off the digest. Read four bytes each
    # so that two faces of one building are not near neighbours in the digest.
    hotspots = [
        (
            seed[index] / 255.0 * 0.7 + 0.1,
            seed[index + 1] / 255.0 * 0.7 + 0.1,
            0.05 + seed[index + 2] / 255.0 * 0.08,
            floor + seed[index + 3] / 255.0 * span,
        )
        for index in (0, 4, 8)
    ]

    scene_px = _FRAME_PX - _SCALE_PX
    pixels: list[list[tuple[int, int, int]]] = []
    for y in range(_FRAME_PX):
        row: list[tuple[int, int, int]] = []
        for x in range(_FRAME_PX):
            if x >= scene_px:
                # The scale: hot at the top, in a dark gutter so the bar reads
                # as an instrument rather than as part of the building.
                inset = 6
                if x < scene_px + inset or x >= _FRAME_PX - inset:
                    row.append((16, 16, 16))
                else:
                    row.append(_ironbow(1.0 - y / _FRAME_PX))
                continue
            u, v = x / scene_px, y / _FRAME_PX
            # A cool wall, marginally warmer towards the base, with the storey
            # bands a fraction cooler where a floor line would be.
            fraction = 0.06 + 0.10 * (1.0 - v)
            for centre_x, centre_y, radius, amplitude in hotspots:
                distance_sq = (u - centre_x) ** 2 + (v - centre_y) ** 2
                fraction += amplitude * math.exp(-distance_sq / (2 * radius * radius))
            if int(v * 16) % 4 == 0:
                fraction -= 0.03
            row.append(_ironbow(fraction))
        pixels.append(row)

    label_left = scene_px - (_GLYPH_ADVANCE * 4 + 2) * _GLYPH_SCALE
    _draw_text(pixels, f"{_SCALE_MAX_C:.0f}C", left=label_left, top=6)
    _draw_text(pixels, f"{_SCALE_MIN_C:.0f}C", left=label_left, top=_FRAME_PX - 22)

    rows = bytearray()
    for line in pixels:
        rows.append(0)  # PNG filter type 0 for this scanline
        for red, green, blue in line:
            rows += bytes((red, green, blue))

    header = struct.pack(">IIBBBBB", _FRAME_PX, _FRAME_PX, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + _chunk(b"IEND", b"")
    )


def next_face(
    scanned: frozenset[FaceLabel],
    spec: GeometrySpec,
    *,
    abandoned: frozenset[FaceLabel] = frozenset(),
) -> FaceLabel | None:
    """The next face to fly, or ``None`` when the sweep is done.

    Filtered against the faces the footprint actually resolves to. Today that
    filter never removes anything -- :func:`face_geometries` labels every
    footprint with all four faces, working from its bounding rectangle, so even
    a triangular parcel has an Alpha through Delta. It is kept because the
    alternative is a sweep that assumes four walls, and the day that
    normalisation changes is the day such a sweep spins for ever waiting for a
    wall nobody can photograph.

    ``abandoned`` is the wall that could not be read. Without it a face whose
    frame times out is picked again on the very next call, and again after that,
    because the only thing this function knows is that it is still UNSCANNED --
    so one slow wall consumed the entire sweep and Bravo, Charlie and Delta were
    never flown at all. Skipping it is not a claim that the wall is fine: the
    caller records the abandonment, the face stays UNSCANNED, and the readiness
    criterion and the route both price it as unknown. Three walls read and one
    stated unreadable beats one wall retried until the incident is over.
    """
    available = {geometry.label for geometry in face_geometries(spec.footprint)}
    for face in SWEEP_ORDER:
        if face in available and face not in scanned and face not in abandoned:
            return face
    return None
