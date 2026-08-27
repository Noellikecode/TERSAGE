"""A landscape generated from coordinates, and drawn so nobody mistakes it for one.

Fake mode is the default and the whole test suite, so the terrain view has to
work with no credentials. That means producing both grids the mesh needs -- real
RGB-encoded height, and a skin to drape on it -- from arithmetic.

**Continuity is the hard part, and it is why the height is a function of
longitude and latitude rather than of pixel position.** A tile generated from
its own ``x``/``y`` looks fine alone and produces a wall at every seam, because
neighbouring tiles disagree about the edge they share. Evaluating a continuous
function of the actual ground coordinate makes adjacent tiles agree by
construction, at every zoom, with no blending.

**It says what it is twice.** The drape is banded and hatched rather than
photographic -- nothing here resembles an aerial photograph -- and the port
reports ``synthetic`` so the console's key names the provider. A generated
hillside that looked like Sonoma County would be a landscape an officer could
plan against, which is the one thing this must not be.

**No image library.** PNG is a signature, three chunks and a zlib stream, and
writing those directly costs forty lines and avoids adding a dependency to the
credential-free path. Nothing here decodes an image; it only writes them.
"""

from __future__ import annotations

import math
import struct
import zlib
from typing import Final

from firstdue.adapters.mercator import tile_bounds, unproject_y
from firstdue.ports.tiles import MAX_ZOOM, MIN_ZOOM, MapTile, TileLayer, unavailable

#: Every Web Mercator tile is this square.
TILE_PX: Final[int] = 256

#: Height is evaluated on this grid and repeated up to the tile. Terrain here is
#: smooth by construction, so the finer detail would be arithmetic nobody sees,
#: and 4,096 evaluations a tile keeps a demo responsive.
SAMPLE_PX: Final[int] = 64

#: Terrarium's encoding: ``(r * 256 + g + b / 256) - 32768`` metres.
TERRARIUM_OFFSET: Final[int] = 32768

#: Sea level, and the ceiling of the generated range. Northern California's real
#: relief runs from the Pacific to about 4,300 m at Whitney; this stays inside
#: that so the mesh has a plausible vertical scale without claiming to be it.
MIN_ELEVATION_M: Final[float] = -20.0
MAX_ELEVATION_M: Final[float] = 2_600.0


def _elevation_at(longitude: float, latitude: float) -> float:
    """Height in metres at one coordinate. Continuous, deterministic, invented.

    A sum of three sine ridges at different angles and wavelengths, plus a
    coastal falloff so the western edge runs down to the sea rather than ending
    in a cliff. No noise function and no randomness: the same coordinate is the
    same height in every process, on every pass, which is what makes a seeded
    demo and a replay identical.
    """
    ridge_a = math.sin(longitude * 1.7 + latitude * 0.9)
    ridge_b = math.sin(longitude * 0.6 - latitude * 2.1 + 1.3)
    ridge_c = math.sin(longitude * 3.4 + latitude * 2.8 + 0.7)

    # Weighted so one ridge system dominates and the others texture it; three
    # equal sines read as a quilt rather than as country.
    shaped = 0.58 * ridge_a + 0.30 * ridge_b + 0.12 * ridge_c

    # Squared towards the low end, because real terrain spends more of its area
    # near the valley floor than near the ridge line.
    normalised: float = ((shaped + 1.0) / 2.0) ** 1.6

    # The Pacific. West of this meridian the land runs out; the falloff is over
    # about a degree so the coast is a slope and not a step.
    coastal = min(1.0, max(0.0, (longitude + 124.2) / 1.0))

    return MIN_ELEVATION_M + (MAX_ELEVATION_M - MIN_ELEVATION_M) * normalised * coastal


def _encode_terrarium(metres: float) -> tuple[int, int, int]:
    """One height as the three bytes terrarium reads it back out of."""
    value = max(0.0, min(65535.0, metres + TERRARIUM_OFFSET))
    whole = int(value)
    return (whole >> 8) & 0xFF, whole & 0xFF, int((value - whole) * 256.0) & 0xFF


def _drape(metres: float, x: int, y: int) -> tuple[int, int, int]:
    """A colour for one height, banded and hatched so it reads as a diagram.

    Green in the valleys through olive to rock, which is the vocabulary a
    terrain map uses -- and then a hard diagonal hatch across all of it, because
    the bands alone would start to look like an aerial photograph at a distance.
    """
    t = max(0.0, min(1.0, (metres - MIN_ELEVATION_M) / (MAX_ELEVATION_M - MIN_ELEVATION_M)))
    if metres <= 0.0:
        red, green, blue = 12, 26, 44  # water
    elif t < 0.35:
        red, green, blue = 38, 64, 42
    elif t < 0.6:
        red, green, blue = 62, 78, 46
    elif t < 0.82:
        red, green, blue = 92, 84, 56
    else:
        red, green, blue = 122, 116, 104

    if metres > 0.0 and (x + y) % 24 < 2:
        # The admission, repeated every 24 pixels across the whole region.
        red, green, blue = min(255, red + 26), min(255, green + 26), min(255, blue + 26)
    return red, green, blue


def _png(rows: list[bytearray]) -> bytes:
    """An 8-bit RGB PNG from scanlines. Filter type 0, one deflate stream."""
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    height = len(rows)
    width = len(rows[0]) // 3

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def _render(layer: TileLayer, z: int, x: int, y: int) -> bytes:
    """One tile of either grid, evaluated on the ground it actually covers."""
    box = tile_bounds(z, x, y)
    span = 1.0 / float(2**z)
    step = TILE_PX // SAMPLE_PX

    # Latitude is sampled in *projected* space and unprojected per row: stepping
    # it linearly in degrees would stretch the terrain towards the poles and put
    # a visible kink at every tile boundary, which is the same trap `center_of`
    # exists for.
    rows: list[bytearray] = []
    for sample_y in range(SAMPLE_PX):
        norm_y = (y + (sample_y + 0.5) / SAMPLE_PX) * span
        latitude = unproject_y(norm_y)

        row = bytearray()
        for sample_x in range(SAMPLE_PX):
            longitude = box.west + (box.east - box.west) * ((sample_x + 0.5) / SAMPLE_PX)
            metres = _elevation_at(longitude, latitude)
            pixel = (
                _encode_terrarium(metres)
                if layer == "elevation"
                else _drape(metres, sample_x * step, sample_y * step)
            )
            row.extend(bytes(pixel) * step)
        rows.extend(bytearray(row) for _ in range(step))

    return _png(rows)


class FakeTileClient:
    """Generates both grids from coordinates. No credentials, no network."""

    provider_label: Final[str] = "synthetic"

    def __init__(
        self,
        *,
        region: object | None = None,
        min_zoom: int = MIN_ZOOM,
        max_zoom: int = MAX_ZOOM,
    ) -> None:
        #: Deliberately *not* region-bounded. The live client refuses a tile
        #: outside its region because it fronts somebody's metered quota; this
        #: one fronts arithmetic, and a demo that refused a tile the camera
        #: drifted onto would be modelling a cost that does not exist here.
        self._region = region
        self._min_zoom = min_zoom
        self._max_zoom = max_zoom
        self._cache: dict[str, bytes] = {}

    async def fetch(self, *, layer: TileLayer, z: int, x: int, y: int) -> MapTile:
        if z < self._min_zoom or z > self._max_zoom:
            return MapTile.refused(layer, z, x, y, unavailable("out_of_zoom"))
        span = 2**z
        if not (0 <= x < span and 0 <= y < span):
            return MapTile.refused(layer, z, x, y, unavailable("out_of_region"))

        key = f"{layer}/{z}/{x}/{y}"
        payload = self._cache.get(key)
        if payload is None:
            payload = _render(layer, z, x, y)
            self._cache[key] = payload

        return MapTile(
            available=True,
            layer=layer,
            z=z,
            x=x,
            y=y,
            content_type="image/png",
            payload=payload,
            # Generated, so it is the same next time and the browser may keep it
            # as long as it likes.
            max_age_s=7 * 24 * 3600,
        )
