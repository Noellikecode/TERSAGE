"""Two tile grids, fronted so the browser never reaches either provider.

The regional map's ground is a mesh, and a mesh needs height and a skin. Those
come from different places, and neither can be handed to a client:

* **Height** is AWS's public ``terrarium`` grid -- SRTM and national lidar,
  RGB-encoded, no key, public domain. It needs no credential and is proxied
  anyway, so the console has exactly one origin to talk to and one place where
  caching, rate limiting and the region check live.
* **Skin** is Google's Map Tiles API. It needs the Maps key *and* a session
  token, and both must stay in this process. A signed tile URL in a browser is
  the key in a browser.

**The session is the part with a lifetime.** Map Tiles issues a token with an
expiry and every tile request carries it. It is minted lazily, reused across
requests, and re-minted when it is close to expiring -- close, not expired,
because a token that dies mid-camera-move would put a hole in the terrain that
looks like an outage.

**The connection is the part with a cost.** A camera opening on the region asks
for two grids at once -- height and skin, one request each per square -- so a
first paint is scores of upstream fetches inside a second or two. Opening an
``httpx.AsyncClient`` per tile pays a fresh TCP handshake and a fresh TLS
handshake for every one of them, and against the real terrarium bucket that
measured 3.0s for 48 tiles at eight-way concurrency where one pooled client took
1.2s. So there is exactly one client per process, built on first use and kept:
the handshake is paid once and every square after it rides a warm connection.
That includes the session mint, which talks to the same host the imagery tiles
do and therefore reuses their connection too.

**RGB elevation is data, not a picture.** A terrarium pixel encodes metres:
``(red * 256 + green + blue / 256) - 32768``. Re-encoding, resizing or
recompressing one changes the terrain silently, so bytes go through untouched
and nothing in this module decodes an image.

**The region check is what stops this being an open relay.** A tile outside the
district's fire-activity box is refused before any upstream request is made.
Without it, anyone who can reach the console can spend the department's Map
Tiles quota on any square of the planet.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from firstdue.adapters.mercator import boxes_overlap, tile_bounds
from firstdue.errors import ConfigurationError
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock
from firstdue.ports.fireactivity import BoundingBox
from firstdue.ports.tiles import (
    MAX_ZOOM,
    MIN_ZOOM,
    MapTile,
    TileLayer,
    unavailable,
)
from firstdue.sources.framework import RateLimiter

if TYPE_CHECKING:  # pragma: no cover - import shape only
    import httpx

logger = get_logger(__name__)

#: Public-domain elevation, RGB-encoded. No key, no session, no terms to accept.
TERRARIUM_URL: Final[str] = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium"

#: Google Map Tiles: a session first, then tiles that carry it.
MAP_TILES_SESSION_URL: Final[str] = "https://tile.googleapis.com/v1/createSession"
MAP_TILES_URL: Final[str] = "https://tile.googleapis.com/v1/2dtiles"

#: Re-mint this far before the token actually dies. A session that expired
#: between the check and the request would drop a square for no reason.
SESSION_REFRESH_MARGIN: Final[timedelta] = timedelta(minutes=10)

#: Terrain does not move and imagery is re-flown on a scale of years, so both
#: are cached hard. The number the console is told, and the number this process
#: keeps its own copy for.
ELEVATION_MAX_AGE_S: Final[int] = 30 * 24 * 3600
IMAGERY_MAX_AGE_S: Final[int] = 7 * 24 * 3600

#: A camera move asks for a screenful of squares at once, so this is generous
#: where the imagery limiter is not: the ceiling exists to stop a runaway loop,
#: not to ration ordinary panning.
DEFAULT_RATE_PER_SECOND: Final[float] = 40.0
DEFAULT_BURST: Final[int] = 120

#: One tile is a quarter of a megabyte at worst. Anything past this is not a
#: 256-pixel square and is not held in memory to find out.
MAX_TILE_BYTES: Final[int] = 2 * 1024 * 1024

#: A tile is small and a camera is waiting on a screenful of them.
DEFAULT_DEADLINE_S: Final[float] = 6.0

#: How long an idle upstream connection is kept before it is dropped, seconds.
#:
#: Longer than the console's standby heartbeat, so a station left open on the
#: regional map pans onto warm sockets rather than re-handshaking every time
#: somebody touches the mouse after a quiet minute.
KEEPALIVE_EXPIRY_S: Final[float] = 300.0

#: How many squares this process keeps. A region at the zooms this serves is a
#: few hundred; past that the oldest go, because a proxy that grew without
#: bound would be a memory leak with a map on it.
DEFAULT_CACHE_ENTRIES: Final[int] = 900


class _ProviderDownError(Exception):
    """The provider could not be reached, carrying a type name and nothing else.

    The message ``httpx`` would give contains the signed URL, and therefore the
    key. Same discipline as the imagery adapter, for the same reason.
    """

    def __init__(self, error_type: str, *, timed_out: bool = False) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        self.timed_out = timed_out


def _provider_down(exc: Exception) -> _ProviderDownError:
    import httpx

    return _ProviderDownError(type(exc).__name__, timed_out=isinstance(exc, httpx.TimeoutException))


@dataclass(slots=True)
class _Session:
    token: str
    expires_at: datetime


class UnconfiguredTileClient:
    """Live mode with no Maps key: the documented state, not an error.

    Elevation would in fact work -- terrarium needs no credential -- and it is
    still refused. A terrain mesh with height and no skin is a grey clay model
    of Northern California, which reads as a rendering failure rather than as a
    missing key. Refusing both makes the console fall back to the flat basemap
    it already knows how to explain.
    """

    provider_label: Final[str] = ""

    async def fetch(self, *, layer: TileLayer, z: int, x: int, y: int) -> MapTile:
        return MapTile.refused(layer, z, x, y, unavailable("unconfigured"))


class GoogleTerrainTileClient:
    """Terrarium for height, Google Map Tiles for skin, bounded to one region."""

    provider_label: Final[str] = "google-map-tiles"

    def __init__(
        self,
        *,
        api_key: str,
        region: BoundingBox,
        clock: Clock,
        min_zoom: int = MIN_ZOOM,
        max_zoom: int = MAX_ZOOM,
        deadline_s: float = DEFAULT_DEADLINE_S,
        rate_per_second: float = DEFAULT_RATE_PER_SECOND,
        burst: int = DEFAULT_BURST,
        max_concurrency: int = 8,
        cache_entries: int = DEFAULT_CACHE_ENTRIES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ConfigurationError(
                "map tiles require GOOGLE_MAPS_API_KEY; "
                "wire UnconfiguredTileClient when there is none"
            )
        self._api_key = api_key
        self._region = region
        self._clock = clock
        self._min_zoom = min_zoom
        self._max_zoom = max_zoom
        self._deadline_s = deadline_s
        self._limiter = RateLimiter(rate_per_second=rate_per_second, burst=burst)
        self._max_concurrency = max_concurrency
        self._gate = asyncio.Semaphore(max_concurrency)
        self._cache_entries = cache_entries
        self._transport = transport
        self._cache: dict[str, MapTile] = {}
        self._session: _Session | None = None
        self._session_lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self.cache_hits = 0
        self.upstream_calls = 0

    async def fetch(self, *, layer: TileLayer, z: int, x: int, y: int) -> MapTile:
        if z < self._min_zoom or z > self._max_zoom:
            return MapTile.refused(layer, z, x, y, unavailable("out_of_zoom"))

        span = 2**z
        if not (0 <= x < span and 0 <= y < span):
            # Off the edge of the world at this zoom. Not a region question and
            # not worth constructing a box for.
            return MapTile.refused(layer, z, x, y, unavailable("out_of_region"))

        if not boxes_overlap(tile_bounds(z, x, y), self._region):
            return MapTile.refused(layer, z, x, y, unavailable("out_of_region"))

        key = f"{layer}/{z}/{x}/{y}"
        cached = self._cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached

        now = self._clock.now()
        if self._limiter.take(now) > 0.0:
            logger.warning("tile_rate_limited", extra={"layer": layer, "z": z, "x": x, "y": y})
            return MapTile.refused(layer, z, x, y, unavailable("rate_limited"))

        async with self._gate:
            try:
                async with asyncio.timeout(self._deadline_s):
                    tile = await self._fetch_live(layer, z, x, y)
            except TimeoutError:
                logger.warning("tile_deadline_exceeded", extra={"layer": layer, "z": z})
                return MapTile.refused(layer, z, x, y, unavailable("deadline"))
            except _ProviderDownError as exc:
                logger.warning("tile_provider_down", extra={"error_type": exc.error_type})
                code = "deadline" if exc.timed_out else "provider_unreachable"
                return MapTile.refused(layer, z, x, y, unavailable(code))

        if tile.available:
            self._remember(key, tile)
        return tile

    async def aclose(self) -> None:
        """Drop the pooled connections, for a process or a test that is done.

        Not required for correctness -- the client owns sockets, and a process
        exiting closes those anyway -- but a test that builds a client per case
        would otherwise leave a pool behind for the garbage collector, and this
        suite turns warnings into errors.
        """
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # ------------------------------------------------------------ internals

    async def _http(self) -> httpx.AsyncClient:
        """The one client, built on first use.

        Built lazily rather than in ``__init__`` because an ``AsyncClient`` binds
        its pool to the running loop, and the container constructs this before
        the API's loop exists. First use is always inside a request, which is
        always on the loop that will serve every later one.

        The pool is sized to *twice* the concurrency gate above it, because the
        two grids come from two different hosts and a camera move interleaves
        them. Sized to the gate exactly, a burst of height tiles would evict the
        warm connections to the imagery host and the next burst would re-handshake
        them -- which is the cost this pool exists to avoid.
        """
        if self._client is not None:
            return self._client

        import httpx

        async with self._client_lock:
            # Re-checked under the lock for the same reason the session is: a
            # camera move arrives as a burst, and every one of them found no
            # client. Only the first should build one.
            if self._client is None:
                self._client = httpx.AsyncClient(
                    timeout=self._deadline_s,
                    transport=self._transport,
                    limits=httpx.Limits(
                        max_connections=self._max_concurrency * 2,
                        max_keepalive_connections=self._max_concurrency * 2,
                        keepalive_expiry=KEEPALIVE_EXPIRY_S,
                    ),
                )
        return self._client

    def _remember(self, key: str, tile: MapTile) -> None:
        """Keep the square, and drop the oldest once the map is full.

        Insertion-ordered, so the oldest key is the first one. Crude next to an
        LRU and correct for the access pattern: a camera sweeping a region
        revisits squares in bands, and the ones it will not come back to are the
        ones it saw first.
        """
        if len(self._cache) >= self._cache_entries:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        self._cache[key] = tile

    async def _fetch_live(self, layer: TileLayer, z: int, x: int, y: int) -> MapTile:
        client = await self._http()

        params: list[tuple[str, str | int | float | bool | None]]
        if layer == "elevation":
            url = f"{TERRARIUM_URL}/{z}/{x}/{y}.png"
            params = []
            max_age = ELEVATION_MAX_AGE_S
        else:
            session = await self._session_token(client)
            url = f"{MAP_TILES_URL}/{z}/{x}/{y}"
            params = [("session", session), ("key", self._api_key)]
            max_age = IMAGERY_MAX_AGE_S

        self.upstream_calls += 1
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            payload = response.content
            content_type = response.headers.get("content-type", "")
        except Exception as exc:
            raise _provider_down(exc) from exc

        if not content_type.startswith("image/") or not payload:
            return MapTile.refused(layer, z, x, y, unavailable("not_an_image"))
        if len(payload) > MAX_TILE_BYTES:
            return MapTile.refused(layer, z, x, y, unavailable("not_an_image"))

        return MapTile(
            available=True,
            layer=layer,
            z=z,
            x=x,
            y=y,
            content_type=content_type.split(";", 1)[0].strip(),
            payload=payload,
            max_age_s=max_age,
        )

    async def _session_token(self, client: httpx.AsyncClient) -> str:
        """The Map Tiles session, minted once and reused until it is nearly out.

        Behind a lock: a camera move arrives as a burst of concurrent tile
        requests, and without one the first screenful would mint a session each.
        """
        now = self._clock.now()
        current = self._session
        if current is not None and now < current.expires_at - SESSION_REFRESH_MARGIN:
            return current.token

        async with self._session_lock:
            # Re-checked inside the lock: every waiter arrived because the token
            # was stale, and only the first of them should mint a new one.
            current = self._session
            if current is not None and now < current.expires_at - SESSION_REFRESH_MARGIN:
                return current.token

            try:
                response = await client.post(
                    MAP_TILES_SESSION_URL,
                    params={"key": self._api_key},
                    json={"mapType": "satellite", "language": "en-US", "region": "US"},
                )
                response.raise_for_status()
                body = response.json()
            except Exception as exc:
                raise _provider_down(exc) from exc

            token = str(body.get("session") or "")
            if not token:
                raise _ProviderDownError("MapTilesSessionMissing")

            # `expiry` is a unix timestamp as a string. A malformed one is
            # treated as a short-lived session rather than as a failure: the
            # token works, and the cost of re-minting early is one request.
            try:
                expires_at = datetime.fromtimestamp(int(body.get("expiry", 0)), tz=now.tzinfo)
            except (TypeError, ValueError):
                expires_at = now + timedelta(hours=1)

            self._session = _Session(token=token, expires_at=expires_at)
            logger.info("map_tiles_session_minted", extra={"expires_at": expires_at.isoformat()})
            return token
