"""Map tiles -- the seam a three-dimensional regional map is built on.

The regional heat map draws detections over the ground they were seen on. A
single flat picture answers "where"; it does not answer "which side of the
ridge", and at a five-degree box that is most of what a fire officer wants from
terrain. Ridgelines are what wind follows, what a fire runs up, and what a crew
has to drive around.

So the ground is a **mesh**, not a plate, and a mesh needs two tiled inputs:

* ``elevation`` -- height, RGB-encoded, one value per pixel.
* ``imagery`` -- what that ground looks like, draped over the mesh.

**Why this is a port and not two more verbs on the imagery one.**
:mod:`firstdue.ports.imagery` answers *what does this thing look like* and
returns one finished picture with the box it covers. A tile is not a picture: it
is one addressed square of an infinite grid, it is meaningless without its
neighbours, and the caller asks for hundreds of them as a camera moves. The two
have different cache lifetimes, different failure granularity -- one tile
missing is a hole, not an outage -- and different shapes on the wire.

**The browser talks only to us, and that is the point.** Both upstreams need
something the client must never hold: Google's Map Tiles API needs the Maps key
and a session token, and a proxy is the only place either can live. The console
addresses tiles at this system's own origin; the key stays in the process, and
the tile the browser receives carries no provenance a devtools tab could follow
back to a signed URL.

**Bounded on purpose.** An implementation is given the region it serves and
refuses anything outside it, at any zoom outside its range. Without that this
port is an open tile relay pointed at somebody else's quota, reachable by anyone
who can reach the console.

**A refusal is a value, never an exception** -- the same discipline
:mod:`firstdue.ports.imagery` applies to pixels. A missing tile is a hole in the
ground plane; the detections, the rings and the key are all drawn regardless,
because a map that refused to render because one square was late would be worse
than a map with a gap in it.
"""

from __future__ import annotations

from typing import Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.errors import ValidationError

#: Which of the two grids is being asked for.
#:
#: ``elevation`` is height data, not a picture: its pixels are an RGB encoding
#: and resampling them the way an image viewer would corrupts the terrain. It is
#: passed through byte for byte, never re-encoded.
TileLayer = Literal["elevation", "imagery"]

#: Web Mercator zooms this port will serve.
#:
#: The floor is the whole world and the ceiling is about 150 m per pixel, which
#: at a regional box is already finer than the 375 m detections drawn on it.
#: Deeper zooms are a different product -- a street map -- and serving them here
#: would spend a metered quota on ground this panel never shows.
MIN_ZOOM: Final[int] = 0
MAX_ZOOM: Final[int] = 12

#: Why there is no tile. Rendered by nothing -- a hole in a mesh has no caption
#: -- but logged, and carried so a caller can tell a refusal from an outage.
UNAVAILABLE_REASONS: Final[dict[str, str]] = {
    "unconfigured": (
        "map tiles need a Google Maps key this process was not given; no tile "
        "provider was contacted"
    ),
    "out_of_region": (
        "this tile lies outside the district's fire-activity region; the tile "
        "proxy serves one region and is not a general relay"
    ),
    "out_of_zoom": (
        "this zoom is outside the range the terrain view uses; deeper tiles are "
        "a street map and are not served here"
    ),
    "provider_unreachable": (
        "the tile provider could not be reached; that is an outage, and the "
        "ground renders with a hole rather than not at all"
    ),
    "deadline": (
        "the tile provider did not answer inside this request's deadline; one "
        "late square is dropped rather than the map held open"
    ),
    "rate_limited": (
        "this process has spent its tile rate budget; map tiles are metered and "
        "a camera move must not be able to spend a day's quota"
    ),
    "not_an_image": (
        "the tile provider answered with something that was not an image, which "
        "is never decoded as terrain"
    ),
}


class TileUnavailable(BaseModel):
    """Why a tile is not being returned.

    A type rather than a bare string, so an implementation cannot arrive at
    "no tile" by forgetting to set a field.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=60)
    reason: str = Field(min_length=1, max_length=400)


def unavailable(code: str) -> TileUnavailable:
    """The refusal for a known code, or a vaguer honest one for an unknown."""
    return TileUnavailable(
        code=code,
        reason=UNAVAILABLE_REASONS.get(code, "no tile could be retrieved for this request"),
    )


class MapTile(BaseModel):
    """One square of the grid, or a stated refusal.

    ``payload`` is the provider's bytes, unaltered. Elevation tiles encode
    height in their RGB channels, so re-encoding, resizing or recompressing one
    changes the terrain rather than the file size -- and it would do it
    invisibly, which is the failure this project refuses everywhere else.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    available: bool
    layer: TileLayer
    z: int = Field(ge=MIN_ZOOM, le=MAX_ZOOM)
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    content_type: str = Field(default="", max_length=80)
    payload: bytes = b""
    #: What the console may cache this for, seconds. Terrain does not move;
    #: imagery is re-flown on a scale of years.
    max_age_s: int = Field(default=0, ge=0)
    unavailable_reason: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _absence_says_why(self) -> MapTile:
        if not self.available:
            if not self.unavailable_reason:
                raise ValidationError("a tile that is unavailable has to say why")
            if self.payload:
                raise ValidationError("a tile that is unavailable cannot carry bytes")
            return self
        if not self.payload:
            raise ValidationError("a tile reported available has to carry bytes")
        if not self.content_type:
            raise ValidationError("a tile reported available has to name its content type")
        return self

    @classmethod
    def refused(cls, layer: TileLayer, z: int, x: int, y: int, refusal: TileUnavailable) -> MapTile:
        """The honest empty answer, with the reason attached."""
        return cls(
            available=False,
            layer=layer,
            # A refusal still names the square it is about: an out-of-region
            # tile is a fact about coordinates, and dropping them would make the
            # log useless for finding out which camera move asked for it.
            z=max(MIN_ZOOM, min(MAX_ZOOM, z)),
            x=max(0, x),
            y=max(0, y),
            unavailable_reason=refusal.reason,
        )


@runtime_checkable
class TileClient(Protocol):
    """Serves the two grids a terrain mesh is built from, or says why it cannot."""

    @property
    def provider_label(self) -> str:
        """What this implementation is, so the console can say so.

        ``synthetic`` here is load-bearing: fake mode is the default and the
        whole test suite, and a generated hillside that did not admit to being
        one would be a landscape nobody could see was invented.
        """
        ...

    async def fetch(self, *, layer: TileLayer, z: int, x: int, y: int) -> MapTile:
        """Return one tile, or a refusal.

        Never raises for an out-of-region square, an unconfigured key, a dead
        provider or a blown deadline. A raise here would turn one missing square
        into a broken console.
        """
        ...
