"""Web Mercator, the small amount of it a static map image needs.

A tile server hands back a picture centred on a coordinate at an integer zoom.
Nothing in that answer says what ground the picture covers, and the console has
to know exactly, because it draws satellite fire detections on top of it. Place
the image against the box that was *asked for* rather than the box it actually
spans and every detection lands a few kilometres from where the instrument saw
it -- an error with no visible symptom, on a display whose whole purpose is
saying where a fire is.

So this module answers two questions and nothing else: which zoom covers a box,
and what ground a given centre, zoom and pixel size actually cover.

**The vertical centre is the trap.** Mercator stretches latitude towards the
poles, so the middle row of pixels in an image is *not* at the arithmetic mean
of its north and south edges. Asking a tile server to centre on
``(north + south) / 2`` therefore returns a picture whose real centre is south
of where it was wanted, and the offset grows with the height of the box: about
1.4 km over the five-degree Northern California region this console draws, which
is four VIIRS pixels. :func:`center_of` projects first, averages in projected
space, and unprojects -- which is the only reason the detections line up.

Pure functions over floats. No I/O, no provider, nothing to configure: the
convention is Google's, Mapbox's, OpenStreetMap's and everybody else's, and it
has not moved since 2005.
"""

from __future__ import annotations

import math
from typing import Final

from firstdue.ports.fireactivity import BoundingBox

#: The tile edge every Web Mercator server uses. At zoom ``z`` the whole world
#: is ``TILE_SIZE * 2**z`` pixels square.
TILE_SIZE: Final[int] = 256

#: Web Mercator cannot represent the poles -- ``y`` runs to infinity there -- so
#: the projection is defined on this band and the square world map is the
#: result. Latitudes outside it are clamped rather than allowed to produce
#: infinities that would propagate silently into a bounding box.
MAX_LATITUDE: Final[float] = 85.051_128_779_806_59

#: Deepest zoom worth asking a static map for. Past this the imagery is a street
#: and the region is a doorstep; the caller has misconfigured its box.
MAX_ZOOM: Final[int] = 20


def _clamp_latitude(latitude: float) -> float:
    return max(-MAX_LATITUDE, min(MAX_LATITUDE, latitude))


def project_x(longitude: float) -> float:
    """Longitude to a fraction of the world, west edge 0.0, east edge 1.0."""
    return (longitude + 180.0) / 360.0


def project_y(latitude: float) -> float:
    """Latitude to a fraction of the world, north edge 0.0, south edge 1.0.

    ``y`` grows *southward*, which is the image convention and the opposite of
    the way latitude runs. Every comparison in this module is written in ``y``
    for that reason rather than being flipped back and forth.
    """
    radians = math.radians(_clamp_latitude(latitude))
    return (1.0 - math.log(math.tan(radians) + 1.0 / math.cos(radians)) / math.pi) / 2.0


def unproject_x(x: float) -> float:
    """The inverse of :func:`project_x`."""
    return x * 360.0 - 180.0


def unproject_y(y: float) -> float:
    """The inverse of :func:`project_y`."""
    return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y))))


def center_of(bounds: BoundingBox) -> tuple[float, float]:
    """The latitude and longitude a static map must be centred on to hold ``bounds``.

    Longitude is linear in Mercator so its centre is the plain average.
    Latitude is not: see the module docstring. Returns ``(latitude, longitude)``
    in the order every mapping API takes them.
    """
    center_y = (project_y(bounds.north) + project_y(bounds.south)) / 2.0
    return unproject_y(center_y), (bounds.west + bounds.east) / 2.0


def zoom_for(
    bounds: BoundingBox, *, width_px: int, height_px: int, max_zoom: int = MAX_ZOOM
) -> int:
    """The deepest integer zoom at which ``bounds`` still fits in the pixel box.

    Deepest rather than shallowest: a zoom too far out wastes resolution on
    ocean, and the caller asked for this region because it is the one that
    matters. The answer always *covers* the box -- it is never cropped to fit,
    because a basemap that quietly dropped the northern third of a region would
    put fires off the edge of the picture rather than on it.

    Returns ``0`` for a box that does not fit even at the world view, which is
    the correct answer for one: zoom 0 is the whole earth.
    """
    span_x = abs(project_x(bounds.east) - project_x(bounds.west))
    span_y = abs(project_y(bounds.south) - project_y(bounds.north))
    for zoom in range(max_zoom, 0, -1):
        world = float(TILE_SIZE * (2**zoom))
        if span_x * world <= width_px and span_y * world <= height_px:
            return zoom
    return 0


def bounds_for(
    *, latitude: float, longitude: float, zoom: int, width_px: int, height_px: int
) -> BoundingBox:
    """The ground an image of this centre, zoom and pixel size actually covers.

    This is the value that has to travel with the picture. It is computed from
    the same three numbers the request was made with, so it cannot drift from
    what the provider drew.

    ``scale`` is deliberately not a parameter. Static Maps' ``scale=2`` returns
    twice the pixels for the *same ground*; folding it in here would halve every
    span and put the whole map at double size over the wrong footprint.
    """
    world = float(TILE_SIZE * (2**zoom))
    half_x = (width_px / world) / 2.0
    half_y = (height_px / world) / 2.0

    center_x = project_x(longitude)
    center_y = project_y(latitude)

    # `y` grows southward, so the north edge is the *smaller* y. Getting this
    # backwards produces a box that fails BoundingBox's own corner-order
    # validator rather than a quietly upside-down map, which is why that
    # validator is worth having.
    return BoundingBox(
        west=max(-180.0, unproject_x(center_x - half_x)),
        east=min(180.0, unproject_x(center_x + half_x)),
        north=min(MAX_LATITUDE, unproject_y(center_y - half_y)),
        south=max(-MAX_LATITUDE, unproject_y(center_y + half_y)),
    )


def covering_image(
    bounds: BoundingBox, *, width_px: int, height_px: int, max_zoom: int = MAX_ZOOM
) -> tuple[float, float, int, BoundingBox]:
    """Everything a static-map request needs, and what its answer will cover.

    Returns ``(latitude, longitude, zoom, covered)``. ``covered`` is always a
    superset of ``bounds`` -- that is the point of the whole module, and
    ``tests/unit/test_mercator.py`` asserts it over a spread of boxes rather
    than trusting the arithmetic here to be right by inspection.
    """
    latitude, longitude = center_of(bounds)
    zoom = zoom_for(bounds, width_px=width_px, height_px=height_px, max_zoom=max_zoom)
    covered = bounds_for(
        latitude=latitude, longitude=longitude, zoom=zoom, width_px=width_px, height_px=height_px
    )
    return latitude, longitude, zoom, covered


def tile_bounds(z: int, x: int, y: int) -> BoundingBox:
    """The ground one Web Mercator tile covers.

    Used to decide whether a requested tile is inside the region a proxy serves.
    Without this the proxy is an open relay pointed at somebody else's quota.
    """
    span = 1.0 / float(2**z)
    return BoundingBox(
        west=max(-180.0, unproject_x(x * span)),
        east=min(180.0, unproject_x((x + 1) * span)),
        north=min(MAX_LATITUDE, unproject_y(y * span)),
        south=max(-MAX_LATITUDE, unproject_y((y + 1) * span)),
    )


def boxes_overlap(a: BoundingBox, b: BoundingBox) -> bool:
    """Whether two boxes share any ground.

    Touching edges count as overlapping: a tile whose eastern edge is the
    region's western edge holds the pixels the region's first column needs, and
    excluding it would draw the map one tile short on every side.
    """
    return not (a.east < b.west or a.west > b.east or a.north < b.south or a.south > b.north)
