"""Web Mercator, checked against the property that matters rather than by eye.

The one thing this module has to get right is that the box it reports is the
box the pixels cover. Everything else -- the projection formulae, the zoom
search -- is a means to it. So the tests are mostly one assertion made many
times over: whatever you ask for, you get back something that contains it.
"""

from __future__ import annotations

import math

import pytest

from firstdue.adapters.mercator import (
    MAX_LATITUDE,
    TILE_SIZE,
    bounds_for,
    center_of,
    covering_image,
    project_x,
    project_y,
    unproject_x,
    unproject_y,
    zoom_for,
)
from firstdue.ports.fireactivity import BoundingBox

#: The region the console actually draws -- Northern California, the box
#: `firms.DEFAULT_REGION` queries.
NORCAL = BoundingBox(west=-124.5, south=36.5, east=-119.5, north=40.5)

BOXES = [
    NORCAL,
    BoundingBox(west=-122.55, south=37.70, east=-122.35, north=37.84),  # San Francisco
    BoundingBox(west=-180.0, south=-60.0, east=180.0, north=70.0),  # most of the world
    BoundingBox(west=-0.5, south=-0.5, east=0.5, north=0.5),  # across both origins
    BoundingBox(west=10.0, south=59.0, east=31.0, north=71.0),  # high latitude, tall
    BoundingBox(west=-100.0, south=-40.0, east=-99.0, north=-39.0),  # southern hemisphere
]


@pytest.mark.parametrize("box", BOXES)
def test_projection_round_trips(box: BoundingBox) -> None:
    for longitude in (box.west, box.east):
        assert unproject_x(project_x(longitude)) == pytest.approx(longitude, abs=1e-9)
    for latitude in (box.south, box.north):
        assert unproject_y(project_y(latitude)) == pytest.approx(latitude, abs=1e-9)


def test_north_is_a_smaller_y_than_south() -> None:
    """The image convention, asserted once so the rest of the module can assume it."""
    assert project_y(40.5) < project_y(36.5)


def test_latitude_centre_is_not_the_arithmetic_mean() -> None:
    """The trap the module exists to avoid, measured on the region in use.

    If this ever starts passing with the plain average, Mercator has stopped
    being Mercator. The gap is small in degrees and large in metres, which is
    exactly why it survives a visual check.
    """
    latitude, longitude = center_of(NORCAL)
    plain_mean = (NORCAL.north + NORCAL.south) / 2.0

    assert longitude == pytest.approx(-122.0)
    assert latitude != pytest.approx(plain_mean, abs=1e-6)

    # Measured, not estimated: 3.1 km at this latitude, which is eight VIIRS
    # pixels. That is the whole reason `center_of` projects before it averages.
    metres = abs(latitude - plain_mean) * 111_320.0
    assert 2_900.0 < metres < 3_300.0


@pytest.mark.parametrize("box", BOXES)
def test_the_covered_box_contains_the_requested_box(box: BoundingBox) -> None:
    """The property. Everything else in the module serves this one."""
    _, _, _, covered = covering_image(box, width_px=640, height_px=640)

    assert covered.west <= box.west + 1e-9
    assert covered.east >= box.east - 1e-9
    assert covered.south <= box.south + 1e-9
    assert covered.north >= box.north - 1e-9


@pytest.mark.parametrize("box", BOXES)
def test_one_zoom_deeper_would_not_have_fitted(box: BoundingBox) -> None:
    """The zoom is the deepest that covers, not merely one that covers.

    Without this a correct-but-lazy implementation returning 0 everywhere would
    pass the containment test above and hand the console a picture of the whole
    planet to put a county's fires on.
    """
    latitude, longitude, zoom, _ = covering_image(box, width_px=640, height_px=640)
    if zoom == 0:
        return

    tighter = bounds_for(
        latitude=latitude, longitude=longitude, zoom=zoom + 1, width_px=640, height_px=640
    )
    covers = (
        tighter.west <= box.west
        and tighter.east >= box.east
        and tighter.south <= box.south
        and tighter.north >= box.north
    )
    assert not covers


def test_a_wider_image_can_hold_a_deeper_zoom() -> None:
    """Pixels buy zoom. A sanity check on the search, not on the projection."""
    narrow = zoom_for(NORCAL, width_px=320, height_px=320)
    wide = zoom_for(NORCAL, width_px=1280, height_px=1280)
    assert wide > narrow


def test_scale_is_not_folded_into_the_bounds() -> None:
    """`scale=2` doubles pixels over the same ground.

    Stated as a test because the mistake -- passing the scaled pixel count into
    `bounds_for` -- produces a map that is wrong by exactly a factor of two and
    still looks like a map.
    """
    ground = bounds_for(latitude=38.5, longitude=-122.0, zoom=7, width_px=640, height_px=640)
    doubled = bounds_for(latitude=38.5, longitude=-122.0, zoom=7, width_px=1280, height_px=1280)

    assert doubled.east - doubled.west == pytest.approx(2.0 * (ground.east - ground.west))


def test_zoom_zero_is_one_tile_of_world() -> None:
    world = bounds_for(latitude=0.0, longitude=0.0, zoom=0, width_px=TILE_SIZE, height_px=TILE_SIZE)
    assert world.west == pytest.approx(-180.0)
    assert world.east == pytest.approx(180.0)
    assert world.north == pytest.approx(MAX_LATITUDE, abs=1e-6)
    assert world.south == pytest.approx(-MAX_LATITUDE, abs=1e-6)


def test_latitude_is_clamped_rather_than_allowed_to_diverge() -> None:
    """The poles are infinity in Mercator, and an infinity in a box is a crash later."""
    assert math.isfinite(project_y(90.0))
    assert math.isfinite(project_y(-90.0))
    assert project_y(90.0) == pytest.approx(project_y(MAX_LATITUDE))


def test_a_box_too_tall_for_any_zoom_gets_the_world_view() -> None:
    huge = BoundingBox(west=-179.9, south=-84.0, east=179.9, north=84.0)
    assert zoom_for(huge, width_px=64, height_px=64) == 0
