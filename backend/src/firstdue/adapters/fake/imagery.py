"""A deterministic placeholder that admits, in the picture, to being one.

Not a stub returning a constant, and -- more important -- not a stock photograph
of a building. The drawing is derived from a digest of the address, so the same
address always renders the same placeholder and two addresses render different
ones, which is the property ADR 0003 asks of every fake and the reason a seeded
demo and a replay stay byte-identical.

**Why it is ugly on purpose.** Fake mode is the default and the entire test
suite, and this is the one adapter whose output a human looks at directly. A
plausible-looking facade would be a simulation nobody could see was a
simulation: an officer would count storeys off it, a screenshot of it would end
up in a deck, and the system would have quietly asserted something about a real
building that it never observed. So the placeholder is flat vector shapes, it
carries the word SYNTHETIC across its face, and it says in its own caption that
no imagery provider was contacted. A hidden simulation is worse than an admitted
one.

The address still resolves through the city adapter, so a coordinate the city
does not know refuses here exactly as it refuses live. A fake that answered for
addresses the live path could not would hide that failure until production.
"""

from __future__ import annotations

import base64
import hashlib
import math
from typing import Final

from firstdue.adapters.mercator import covering_image
from firstdue.ports.city import CityAdapter
from firstdue.ports.fireactivity import BoundingBox
from firstdue.ports.imagery import (
    PROVIDER_SYNTHETIC,
    BasemapStyle,
    BuildingImagery,
    ImageryView,
    RegionBasemap,
    unavailable,
)

#: Frame size. Matches what the live adapter asks Street View for, so the
#: console's imagery pane has one aspect ratio to lay out rather than two.
_WIDTH: Final[int] = 640
_HEIGHT: Final[int] = 480

#: The synthetic ground plane. Square, and the same pixel count the live
#: adapter asks Static Maps for, so the placement arithmetic the console runs is
#: identical in both modes -- which is the point of having a second
#: implementation rather than a stub.
_BASEMAP_PX: Final[int] = 640

#: Deliberately not photographic. Muted, obviously flat, and far from the
#: colours a real facade photograph lands in.
_WALLS: Final[tuple[str, ...]] = ("#4a5568", "#5b6478", "#6b5f52", "#4f5b52", "#5d5468")

_SYNTHETIC_CAPTION: Final[str] = (
    "SYNTHETIC PLACEHOLDER - generated from the address, not photographed"
)


def _escape(raw: str) -> str:
    """XML-escape without importing an XML parser to do it.

    Three characters can break out of SVG text, and the address text is the only
    thing in this drawing that comes from data.
    """
    return raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class FakeImageryClient:
    """Draws a placeholder from the address digest. No credentials, no network."""

    provider_label: Final[str] = PROVIDER_SYNTHETIC

    def __init__(self, *, city: CityAdapter, available: bool = True) -> None:
        #: When false every address reports the no-coverage refusal. The demo
        #: needs that: "the console has no photograph of this building" is a
        #: state the refusal panel must be able to render, and a fake that could
        #: only succeed would leave that panel untested until the first live
        #: address with no panorama.
        self._available = available
        self._city = city

    async def fetch(self, *, address_id: str, view: ImageryView = "street") -> BuildingImagery:
        address = self._city.get_address(address_id)
        if address is None:
            return BuildingImagery.refused(address_id, unavailable("address_unresolved"))
        if not self._available:
            return BuildingImagery.refused(address_id, unavailable("simulated_absence"))

        # The two views are drawn differently on purpose. A placeholder that
        # looked the same from the kerb and from above would let somebody
        # believe the aerial panel was showing them a roof.
        digest = hashlib.sha256(f"{view}:{address_id}".encode()).digest()
        svg = (
            _render_aerial(digest, display=address.display)
            if view == "aerial"
            else _render(digest, display=address.display)
        )
        encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
        return BuildingImagery(
            address_id=address_id,
            available=True,
            provider=PROVIDER_SYNTHETIC,
            content_type="image/svg+xml",
            data_url=f"data:image/svg+xml;base64,{encoded}",
            # Not a Google attribution, because no Google imagery was fetched.
            # The console renders this line under the frame either way, so in
            # fake mode the line itself says what it is looking at.
            attribution="TERSAGE synthetic placeholder - no imagery provider was contacted",
            captured_hint="generated deterministically from the address; nothing was captured",
        )

    async def fetch_region(
        self, *, bounds: BoundingBox, style: BasemapStyle = "terrain"
    ) -> RegionBasemap:
        """A graticule, drawn to the same box the live adapter would cover.

        **The bounds are computed with the real Mercator arithmetic, not faked.**
        That is the whole value of this method: the console's placement code --
        the part that puts a fire on a hillside -- runs against the same numbers
        in the demo as in production, so a projection bug shows up on a laptop
        rather than only against a billed provider.

        What is fake is the picture, and it says so across its face. There is no
        coastline in it, because inventing one would draw a shoreline a
        commander could mistake for the real one.
        """
        if not self._available:
            return RegionBasemap.refused(unavailable("simulated_absence"))

        _, _, zoom, covered = covering_image(bounds, width_px=_BASEMAP_PX, height_px=_BASEMAP_PX)
        svg = _render_region(covered, zoom=zoom, style=style)
        encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
        return RegionBasemap(
            available=True,
            provider=PROVIDER_SYNTHETIC,
            content_type="image/svg+xml",
            data_url=f"data:image/svg+xml;base64,{encoded}",
            bounds=covered,
            zoom=zoom,
            style=style,
            attribution="TERSAGE synthetic ground plane - no map provider was contacted",
        )


def _render(digest: bytes, *, display: str) -> str:
    """The placeholder itself.

    Storey bands come from the digest rather than from the profile on purpose:
    this drawing must never look like it agrees or disagrees with the measured
    geometry, because it is not evidence about the building at all.
    """
    wall = _WALLS[digest[0] % len(_WALLS)]
    storeys = 2 + digest[1] % 4
    bays = 2 + digest[2] % 3
    roof_y = 120 + digest[3] % 60

    body_height = _HEIGHT - roof_y - 60
    band_height = body_height / storeys
    windows: list[str] = []
    for storey in range(storeys):
        top = roof_y + storey * band_height + band_height * 0.25
        for bay in range(bays):
            left = 150 + bay * (340 / bays)
            windows.append(
                f'<rect x="{left:.1f}" y="{top:.1f}" width="{240 / bays:.1f}" '
                f'height="{band_height * 0.45:.1f}" fill="#1b2028" opacity="0.75"/>'
            )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" '
        f'viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" '
        f'aria-label="Synthetic placeholder for {_escape(display)}. Not a photograph.">'
        "<defs>"
        '<pattern id="hatch" width="16" height="16" patternUnits="userSpaceOnUse" '
        'patternTransform="rotate(45)">'
        '<rect width="16" height="16" fill="#11151c"/>'
        '<rect width="8" height="16" fill="#161b24"/>'
        "</pattern>"
        "</defs>"
        f'<rect width="{_WIDTH}" height="{_HEIGHT}" fill="url(#hatch)"/>'
        f'<rect x="120" y="{roof_y}" width="400" height="{body_height}" fill="{wall}"/>'
        f'<rect x="120" y="{roof_y}" width="400" height="10" fill="#222831"/>'
        + "".join(windows)
        + f'<rect x="300" y="{_HEIGHT - 120:.0f}" width="70" height="60" fill="#1b2028"/>'
        '<text x="320" y="250" font-family="monospace" font-size="64" font-weight="bold" '
        'fill="#ffffff" opacity="0.30" text-anchor="middle" '
        'transform="rotate(-24 320 250)">SYNTHETIC</text>'
        f'<text x="320" y="34" font-family="monospace" font-size="17" fill="#f6c453" '
        f'text-anchor="middle">{_escape(_SYNTHETIC_CAPTION)}</text>'
        f'<text x="320" y="{_HEIGHT - 18}" font-family="monospace" font-size="16" '
        f'fill="#cbd5e1" text-anchor="middle">{_escape(display)}</text>'
        "</svg>"
    )


def _render_aerial(digest: bytes, *, display: str) -> str:
    """The overhead placeholder.

    Looking down rather than at a facade: a roof outline, a ridge, and a couple
    of plant boxes. It carries the same SYNTHETIC face as the elevation, for the
    same reason -- an aerial is the view a commander trusts most about a roof
    they are about to put people on, so a drawing standing in for one has to be
    unmistakable.

    Shapes come from the digest, never from the measured geometry. This drawing
    must not look like it agrees or disagrees with the massing model, because it
    is not evidence about the building at all.
    """
    roof = _WALLS[digest[0] % len(_WALLS)]
    inset = 60 + digest[1] % 40
    ridge = _HEIGHT // 2 + (digest[2] % 40) - 20
    units = 2 + digest[3] % 3

    plant = "".join(
        f'<rect x="{inset + 40 + i * 70}" y="{ridge - 46}" width="44" height="30" '
        f'rx="3" fill="#2f3743" stroke="#8b97a8" stroke-width="1.5"/>'
        for i in range(units)
    )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" '
        f'viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" '
        f'aria-label="Synthetic overhead placeholder for {_escape(display)}">'
        f'<rect width="{_WIDTH}" height="{_HEIGHT}" fill="#161b22"/>'
        # The parcel, then the roof sitting inside it.
        f'<rect x="{inset - 28}" y="{inset - 28}" width="{_WIDTH - 2 * inset + 56}" '
        f'height="{_HEIGHT - 2 * inset + 56}" fill="none" stroke="#2f3743" '
        f'stroke-width="2" stroke-dasharray="6 5"/>'
        f'<rect x="{inset}" y="{inset}" width="{_WIDTH - 2 * inset}" '
        f'height="{_HEIGHT - 2 * inset}" fill="{roof}" stroke="#8b97a8" stroke-width="2"/>'
        # A ridge line, so it reads as a roof rather than a filled box.
        f'<line x1="{inset}" y1="{ridge}" x2="{_WIDTH - inset}" y2="{ridge}" '
        f'stroke="#8b97a8" stroke-width="1.5" stroke-dasharray="8 6"/>'
        f"{plant}"
        f'<text x="{_WIDTH / 2}" y="{_HEIGHT / 2 + 10}" text-anchor="middle" '
        f'font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="46" '
        f'fill="#8b97a8" fill-opacity="0.5" letter-spacing="10">SYNTHETIC</text>'
        f'<text x="16" y="{_HEIGHT - 18}" '
        f'font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="15" '
        f'fill="#8b97a8">{_escape(_SYNTHETIC_CAPTION)}</text>'
        f"</svg>"
    )


def _render_region(bounds: BoundingBox, *, zoom: int, style: BasemapStyle) -> str:
    """The synthetic ground plane: a graticule, and an admission.

    Whole-degree lines only. A finer grid would suggest the drawing knows
    something about the ground between them, and it knows nothing at all -- it
    is a coordinate reference and a statement that no map provider was reached.

    Latitude lines are placed by *Mercator* fraction rather than by linear
    interpolation, for the same reason the centre is projected before it is
    averaged: at five degrees of height the two differ visibly, and a grid drawn
    the wrong way would make the projection look broken when the detections on
    top of it are placed correctly.
    """
    from firstdue.adapters.mercator import project_y

    top = project_y(bounds.north)
    bottom = project_y(bounds.south)
    span_y = bottom - top or 1.0
    span_x = (bounds.east - bounds.west) or 1.0

    lines: list[str] = []
    labels: list[str] = []

    first_lat = math.ceil(bounds.south)
    for latitude in range(first_lat, int(math.floor(bounds.north)) + 1):
        y = (project_y(float(latitude)) - top) / span_y * _BASEMAP_PX
        lines.append(
            f'<line x1="0" y1="{y:.1f}" x2="{_BASEMAP_PX}" y2="{y:.1f}" '
            'stroke="#2a323d" stroke-width="1"/>'
        )
        labels.append(
            f'<text x="6" y="{y - 4:.1f}" fill="#5b6478" font-size="11" '
            f'font-family="monospace">{latitude}°N</text>'
        )

    first_lon = math.ceil(bounds.west)
    for longitude in range(first_lon, int(math.floor(bounds.east)) + 1):
        x = (longitude - bounds.west) / span_x * _BASEMAP_PX
        lines.append(
            f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{_BASEMAP_PX}" '
            'stroke="#2a323d" stroke-width="1"/>'
        )
        labels.append(
            f'<text x="{x + 4:.1f}" y="{_BASEMAP_PX - 8}" fill="#5b6478" font-size="11" '
            f'font-family="monospace">{abs(longitude)}°W</text>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_BASEMAP_PX}" '
        f'height="{_BASEMAP_PX}" viewBox="0 0 {_BASEMAP_PX} {_BASEMAP_PX}" role="img" '
        f'aria-label="Synthetic {style} ground plane at zoom {zoom}. '
        'No map provider was contacted; there is no coastline in this drawing.">'
        f'<rect width="{_BASEMAP_PX}" height="{_BASEMAP_PX}" fill="#0f141a"/>'
        f"{''.join(lines)}"
        f"{''.join(labels)}"
        f'<text x="{_BASEMAP_PX / 2:.0f}" y="{_BASEMAP_PX / 2:.0f}" fill="#242c37" '
        'font-size="46" font-family="monospace" text-anchor="middle" '
        'letter-spacing="10">SYNTHETIC</text>'
        f'<text x="{_BASEMAP_PX / 2:.0f}" y="{_BASEMAP_PX / 2 + 26:.0f}" fill="#242c37" '
        'font-size="13" font-family="monospace" text-anchor="middle">'
        "no map provider was contacted</text>"
        "</svg>"
    )
