"""The tile proxy: what it spends upstream, and what it refuses to spend it on.

Two properties, and both of them are about cost rather than about pixels. The
Map Tiles session and the upstream connection are the expensive parts of serving
a mesh -- a session is a round trip that every imagery tile needs, and a
connection is a TCP and a TLS handshake that every tile needs -- so a proxy that
re-established either per square would turn a screenful into a stall. Measured
against the real terrarium bucket, a client per tile took 3.0s for 48 tiles at
eight-way concurrency where one pooled client took 1.2s.

The third property is the one that stops this being an open relay, and it is
tested here beside the other two because the region check is what makes the
cheapness safe: a proxy this fast, unbounded, is a fast way to spend somebody
else's quota.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from firstdue.adapters.clock import FixedClock
from firstdue.adapters.tiles import (
    MAP_TILES_SESSION_URL,
    GoogleTerrainTileClient,
    UnconfiguredTileClient,
)
from firstdue.errors import ConfigurationError
from firstdue.ports.fireactivity import BoundingBox
from firstdue.ports.tiles import TileClient

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)

#: Distinctive enough that searching a response for it is a real test.
API_KEY = "AIza-TEST-KEY-do-not-ship-3f9c"

#: The region the console is configured for in fake mode.
REGION = BoundingBox(west=-124.5, south=36.5, east=-119.5, north=40.5)

#: A square inside it, and one nowhere near it. Zoom 10 because several of
#: these tests walk a band of neighbours, and the region is only five squares
#: wide at zoom 8.
INSIDE = (10, 160, 390)
OUTSIDE = (8, 10, 10)

PNG = b"\x89PNG\r\n\x1a\nfake-png-bytes"


class _Recorder:
    """A transport that answers everything and remembers what it was asked."""

    def __init__(self, *, expiry: datetime | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._expiry = expiry or (NOW + timedelta(hours=2))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url).startswith(MAP_TILES_SESSION_URL):
            return httpx.Response(
                200,
                json={"session": "session-token-1", "expiry": str(int(self._expiry.timestamp()))},
            )
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    @property
    def session_mints(self) -> int:
        return sum(1 for r in self.requests if str(r.url).startswith(MAP_TILES_SESSION_URL))


def _client(recorder: _Recorder, *, clock: FixedClock | None = None) -> GoogleTerrainTileClient:
    return GoogleTerrainTileClient(
        api_key=API_KEY,
        region=REGION,
        clock=clock or FixedClock(NOW),
        transport=httpx.MockTransport(recorder),
    )


def test_both_implementations_satisfy_the_port() -> None:
    assert isinstance(UnconfiguredTileClient(), TileClient)
    assert isinstance(
        GoogleTerrainTileClient(api_key=API_KEY, region=REGION, clock=FixedClock(NOW)), TileClient
    )


def test_a_live_client_cannot_be_built_without_a_key() -> None:
    with pytest.raises(ConfigurationError):
        GoogleTerrainTileClient(api_key="", region=REGION, clock=FixedClock(NOW))


# ------------------------------------------------------------------ session --


async def test_the_session_is_minted_once_and_carried_by_every_imagery_tile() -> None:
    """A handshake per square is the difference between a map and a stall."""
    recorder = _Recorder()
    client = _client(recorder)

    z, x, y = INSIDE
    for offset in range(6):
        tile = await client.fetch(layer="imagery", z=z, x=x + offset, y=y)
        assert tile.available is True
    await client.aclose()

    assert recorder.session_mints == 1
    tiles = [r for r in recorder.requests if not str(r.url).startswith(MAP_TILES_SESSION_URL)]
    assert len(tiles) == 6
    assert all(r.url.params.get("session") == "session-token-1" for r in tiles)


async def test_a_burst_of_tiles_mints_one_session_between_them() -> None:
    """A camera move arrives all at once; only the first of it should pay."""
    import asyncio

    recorder = _Recorder()
    client = _client(recorder)

    z, x, y = INSIDE
    results = await asyncio.gather(
        *(client.fetch(layer="imagery", z=z, x=x, y=y + offset) for offset in range(8))
    )
    await client.aclose()

    assert all(tile.available for tile in results)
    assert recorder.session_mints == 1


async def test_a_session_near_its_expiry_is_re_minted_before_it_dies() -> None:
    """Re-minted early on purpose: a token dying mid-move is a hole in the mesh."""
    clock = FixedClock(NOW)
    # Expires inside the refresh margin the moment the clock is advanced.
    recorder = _Recorder(expiry=NOW + timedelta(minutes=15))
    client = _client(recorder, clock=clock)

    z, x, y = INSIDE
    await client.fetch(layer="imagery", z=z, x=x, y=y)
    assert recorder.session_mints == 1

    clock.advance(timedelta(minutes=10))
    await client.fetch(layer="imagery", z=z, x=x + 1, y=y)
    await client.aclose()

    assert recorder.session_mints == 2


async def test_elevation_never_touches_the_session_or_the_key() -> None:
    """Terrarium is public domain. Minting a session for it would be a round trip
    spent to prove nothing, and the key on the URL would be the key one redirect
    away from a browser."""
    recorder = _Recorder()
    client = _client(recorder)

    z, x, y = INSIDE
    tile = await client.fetch(layer="elevation", z=z, x=x, y=y)
    await client.aclose()

    assert tile.available is True
    assert recorder.session_mints == 0
    assert all(API_KEY not in str(r.url) for r in recorder.requests)


# ---------------------------------------------------------------- pooling ----


async def test_one_upstream_client_serves_every_tile() -> None:
    """The pool is the point: a client per square pays a TCP and a TLS handshake
    per square, and a first paint is scores of them across two grids."""
    recorder = _Recorder()
    client = _client(recorder)

    z, x, y = INSIDE
    first = await client.fetch(layer="elevation", z=z, x=x, y=y)
    pooled = client._client
    for offset in range(1, 5):
        await client.fetch(layer="elevation", z=z, x=x + offset, y=y)
        await client.fetch(layer="imagery", z=z, x=x + offset, y=y)

    assert first.available is True
    assert pooled is not None
    # Same object throughout, so the connections it holds are reused rather than
    # rebuilt. A new client here would be a new pool and a cold handshake.
    assert client._client is pooled

    await client.aclose()
    assert client._client is None


async def test_the_pool_survives_a_provider_that_refuses() -> None:
    """An outage must not poison the client for the squares that come after it."""

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    client = GoogleTerrainTileClient(
        api_key=API_KEY,
        region=REGION,
        clock=FixedClock(NOW),
        transport=httpx.MockTransport(handler),
    )

    z, x, y = INSIDE
    down = await client.fetch(layer="elevation", z=z, x=x, y=y)
    recovered = await client.fetch(layer="elevation", z=z, x=x + 1, y=y)
    await client.aclose()

    assert down.available is False
    assert "outage" in down.unavailable_reason
    assert recovered.available is True


# ------------------------------------------------------------------ caching --


async def test_a_square_already_held_costs_nothing_upstream() -> None:
    """A camera sweeping a region revisits squares in bands."""
    recorder = _Recorder()
    client = _client(recorder)

    z, x, y = INSIDE
    first = await client.fetch(layer="elevation", z=z, x=x, y=y)
    second = await client.fetch(layer="elevation", z=z, x=x, y=y)
    await client.aclose()

    assert first.payload == second.payload
    assert client.cache_hits == 1
    assert client.upstream_calls == 1


# ------------------------------------------------------------------- bounds --


async def test_a_square_outside_the_region_is_refused_before_anyone_is_billed() -> None:
    """Without this the endpoint is an open relay onto a metered quota."""
    recorder = _Recorder()
    client = _client(recorder)

    z, x, y = OUTSIDE
    tile = await client.fetch(layer="imagery", z=z, x=x, y=y)
    await client.aclose()

    assert tile.available is False
    assert "outside the district's fire-activity region" in tile.unavailable_reason
    assert recorder.requests == []


async def test_a_zoom_the_view_never_uses_is_refused_before_anyone_is_billed() -> None:
    recorder = _Recorder()
    client = _client(recorder)

    tile = await client.fetch(layer="imagery", z=18, x=160, y=390)
    await client.aclose()

    assert tile.available is False
    assert recorder.requests == []


async def test_no_key_configured_refuses_both_grids_rather_than_half_a_mesh() -> None:
    """Height with no skin is a grey clay model, which reads as a broken render."""
    client = UnconfiguredTileClient()

    for layer in ("elevation", "imagery"):
        tile = await client.fetch(layer=layer, z=10, x=160, y=390)  # type: ignore[arg-type]
        assert tile.available is False
        assert "was not given" in tile.unavailable_reason


async def test_the_api_key_never_appears_in_a_refusal() -> None:
    """``httpx`` puts the signed URL in its messages, and the URL carries the key."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(MAP_TILES_SESSION_URL):
            return httpx.Response(200, json={"session": "s", "expiry": "0"})
        return httpx.Response(403, text=f"forbidden: {request.url}")

    client = GoogleTerrainTileClient(
        api_key=API_KEY,
        region=REGION,
        clock=FixedClock(NOW),
        transport=httpx.MockTransport(handler),
    )

    z, x, y = INSIDE
    tile = await client.fetch(layer="imagery", z=z, x=x, y=y)
    await client.aclose()

    assert tile.available is False
    assert API_KEY not in json.dumps(tile.model_dump(mode="json"))
