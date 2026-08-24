"""Building imagery: the placeholder that admits it, and the key that never leaves.

Two properties matter more than the pictures. The synthetic adapter must be
impossible to mistake for a photograph, and the live adapter must never let
``GOOGLE_MAPS_API_KEY`` reach anything a browser can see -- including an error
message, which is where it would leak if anybody rendered ``str(exc)`` from
``httpx``.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from firstdue.adapters.clock import FixedClock
from firstdue.adapters.fake.imagery import FakeImageryClient
from firstdue.adapters.google.imagery import (
    STATIC_MAP_URL,
    STREET_VIEW_METADATA_URL,
    STREET_VIEW_URL,
    GoogleImageryClient,
    UnconfiguredImageryClient,
)
from firstdue.city.san_francisco import SanFranciscoAdapter
from firstdue.errors import ConfigurationError
from firstdue.ports.imagery import BuildingImagery, ImageryClient, unavailable

REPO_ROOT = Path(__file__).resolve().parents[2]
ADDRESS = "sf-0450-hayes"
OTHER_ADDRESS = "sf-1215-fell"
UNKNOWN = "sf-nowhere-at-all"
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)

#: Distinctive enough that a substring search for it in a serialized response is
#: a real test rather than a coincidence.
API_KEY = "AIza-TEST-KEY-do-not-ship-3f9c"

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
PNG = b"\x89PNG\r\n\x1a\nfake-png-bytes"


@pytest.fixture
def city() -> SanFranciscoAdapter:
    return SanFranciscoAdapter(REPO_ROOT / "fixtures")


def _decoded(imagery: BuildingImagery) -> bytes:
    header, _, payload = imagery.data_url.partition(",")
    assert header.endswith(";base64")
    return base64.b64decode(payload)


# ------------------------------------------------------------------- fake --


async def test_the_placeholder_is_stable_per_address(city: SanFranciscoAdapter) -> None:
    """Same address, same picture -- a replay of a demo is byte-identical."""
    client = FakeImageryClient(city=city)

    first = await client.fetch(address_id=ADDRESS)
    second = await client.fetch(address_id=ADDRESS)
    elsewhere = await client.fetch(address_id=OTHER_ADDRESS)

    assert first.data_url == second.data_url
    assert first.data_url != elsewhere.data_url


async def test_the_placeholder_says_in_the_picture_that_it_is_one(
    city: SanFranciscoAdapter,
) -> None:
    """A hidden simulation is worse than an admitted one -- so it is admitted."""
    imagery = await FakeImageryClient(city=city).fetch(address_id=ADDRESS)

    assert imagery.available is True
    assert imagery.provider == "synthetic"
    assert imagery.content_type == "image/svg+xml"

    svg = _decoded(imagery).decode("utf-8")
    assert svg.startswith("<svg")
    assert "SYNTHETIC" in svg
    assert "not photographed" in svg
    # The attribution line renders under the frame, so in fake mode the line
    # itself has to say what the officer is looking at.
    assert "synthetic placeholder" in imagery.attribution.lower()
    assert "no imagery provider was contacted" in imagery.attribution


async def test_the_placeholder_never_claims_to_have_been_captured(
    city: SanFranciscoAdapter,
) -> None:
    imagery = await FakeImageryClient(city=city).fetch(address_id=ADDRESS)
    assert "nothing was captured" in imagery.captured_hint


async def test_an_address_the_city_does_not_know_is_refused_not_drawn(
    city: SanFranciscoAdapter,
) -> None:
    """The fake refuses exactly where the live adapter refuses.

    A fake that answered for addresses the live path cannot resolve would hide
    that failure until the first live deployment.
    """
    imagery = await FakeImageryClient(city=city).fetch(address_id=UNKNOWN)

    assert imagery.available is False
    assert imagery.data_url == ""
    assert imagery.provider == ""
    assert "no coordinate" in imagery.unavailable_reason


async def test_fake_mode_can_simulate_a_building_with_no_coverage(
    city: SanFranciscoAdapter,
) -> None:
    """The console's refusal panel is exercised by the demo, not only in production."""
    imagery = await FakeImageryClient(city=city, available=False).fetch(address_id=ADDRESS)

    assert imagery.available is False
    assert "fake mode is simulating" in imagery.unavailable_reason


# ------------------------------------------------------------------ shape --


def test_an_unavailable_answer_cannot_be_silent() -> None:
    """The type refuses the failure mode this whole feature exists to prevent."""
    from firstdue.errors import ValidationError

    with pytest.raises(ValidationError):
        BuildingImagery(address_id=ADDRESS, available=False)

    with pytest.raises(ValidationError):
        BuildingImagery(address_id=ADDRESS, available=True)


def test_an_unknown_refusal_code_still_produces_a_sentence() -> None:
    """A mis-typed code degrades to a vaguer honest answer, never to a 500."""
    assert unavailable("no-such-code").reason
    assert unavailable("unconfigured").reason != unavailable("no-such-code").reason


def test_both_adapters_satisfy_the_port(city: SanFranciscoAdapter) -> None:
    assert isinstance(FakeImageryClient(city=city), ImageryClient)
    assert isinstance(UnconfiguredImageryClient(), ImageryClient)
    assert isinstance(
        GoogleImageryClient(api_key=API_KEY, city=city, clock=FixedClock(NOW)), ImageryClient
    )


# ---------------------------------------------------------------- google --


def _google(
    city: SanFranciscoAdapter,
    handler: object,
    *,
    clock: FixedClock | None = None,
    **kwargs: object,
) -> GoogleImageryClient:
    return GoogleImageryClient(
        api_key=API_KEY,
        city=city,
        clock=clock or FixedClock(NOW),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _metadata_response(status: str) -> httpx.Response:
    body: dict[str, object] = {"status": status}
    if status == "OK":
        body |= {"pano_id": "PANO-1", "date": "2024-06", "copyright": "© 2024 Google"}
    return httpx.Response(200, json=body)


def _street_view_available(request: httpx.Request) -> httpx.Response:
    if str(request.url).startswith(STREET_VIEW_METADATA_URL):
        return _metadata_response("OK")
    if str(request.url).startswith(STREET_VIEW_URL):
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})
    raise AssertionError(f"unexpected call to {request.url.path}")


async def test_a_facade_photograph_is_preferred(city: SanFranciscoAdapter) -> None:
    imagery = await _google(city, _street_view_available).fetch(address_id=ADDRESS)

    assert imagery.available is True
    assert imagery.provider == "street-view"
    assert imagery.content_type == "image/jpeg"
    assert _decoded(imagery) == JPEG
    # Google's Terms require this to be visible; the console renders it.
    assert imagery.attribution == "© 2024 Google"
    assert "2024-06" in imagery.captured_hint


async def test_the_exact_panorama_the_metadata_described_is_the_one_fetched(
    city: SanFranciscoAdapter,
) -> None:
    """Otherwise the printed capture date belongs to a different photograph."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _street_view_available(request)

    await _google(city, handler).fetch(address_id=ADDRESS)

    image_request = seen[-1]
    assert image_request.url.params["pano"] == "PANO-1"


async def test_no_panorama_falls_back_to_satellite_rather_than_a_grey_tile(
    city: SanFranciscoAdapter,
) -> None:
    """Street View's "no imagery" placeholder is never rendered as the building."""
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        called.append(url.split("?")[0])
        if url.startswith(STREET_VIEW_METADATA_URL):
            return _metadata_response("ZERO_RESULTS")
        if url.startswith(STATIC_MAP_URL):
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        raise AssertionError("street view static must not be called with no panorama")

    imagery = await _google(city, handler).fetch(address_id=ADDRESS)

    assert imagery.provider == "satellite"
    assert _decoded(imagery) == PNG
    assert STREET_VIEW_URL not in called
    # Static Maps stamps no capture date, so none is invented.
    assert imagery.captured_hint == ""
    assert imagery.attribution


async def test_no_coverage_anywhere_is_a_stated_refusal(city: SanFranciscoAdapter) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(STREET_VIEW_METADATA_URL):
            return _metadata_response("ZERO_RESULTS")
        # Static Maps answering with JSON rather than an image.
        return httpx.Response(
            200, json={"error": "nope"}, headers={"content-type": "application/json"}
        )

    imagery = await _google(city, handler).fetch(address_id=ADDRESS)

    assert imagery.available is False
    assert imagery.data_url == ""
    assert "no street-level panorama" in imagery.unavailable_reason


async def test_a_dead_provider_is_an_outage_not_an_absent_building(
    city: SanFranciscoAdapter,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    imagery = await _google(city, handler).fetch(address_id=ADDRESS)

    assert imagery.available is False
    assert "outage" in imagery.unavailable_reason


async def test_a_slow_provider_returns_unavailable_rather_than_holding_the_request(
    city: SanFranciscoAdapter,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1.0)
        return _metadata_response("OK")

    client = _google(city, handler, deadline_s=0.05)
    imagery = await asyncio.wait_for(client.fetch(address_id=ADDRESS), timeout=5.0)

    assert imagery.available is False
    assert "deadline" in imagery.unavailable_reason


# ----------------------------------------------------------- the key rule --


async def test_the_api_key_never_appears_in_a_response(city: SanFranciscoAdapter) -> None:
    """The whole security boundary, asserted on every path a browser can see.

    The response carries bytes, not a signed URL. If any field ever grew a
    provider URL, this fails.
    """

    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def forbidden(request: httpx.Request) -> httpx.Response:
        # httpx puts the full request URL -- key and all -- into the message of
        # the HTTPStatusError this raises. Rendering str(exc) would publish it.
        return httpx.Response(403, json={"error_message": "The provided API key is invalid."})

    for handler in (_street_view_available, unreachable, forbidden):
        client = _google(city, handler)
        for address in (ADDRESS, UNKNOWN):
            body = json.loads((await client.fetch(address_id=address)).model_dump_json())
            rendered = json.dumps(body)
            assert API_KEY not in rendered
            assert "key=" not in rendered
            assert "maps.googleapis.com" not in rendered


async def test_a_forbidden_key_reads_as_an_outage_not_as_a_key_problem(
    city: SanFranciscoAdapter,
) -> None:
    """An officer is told imagery is down. The key detail stays in the logs."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error_message": "The provided API key is invalid."})

    imagery = await _google(city, handler).fetch(address_id=ADDRESS)

    assert imagery.available is False
    assert "outage" in imagery.unavailable_reason


def test_a_live_client_cannot_be_built_without_a_key(city: SanFranciscoAdapter) -> None:
    with pytest.raises(ConfigurationError):
        GoogleImageryClient(api_key="", city=city, clock=FixedClock(NOW))


async def test_no_key_configured_is_a_documented_state() -> None:
    imagery = await UnconfiguredImageryClient().fetch(address_id=ADDRESS)

    assert imagery.available is False
    assert "not given" in imagery.unavailable_reason
    assert "Street View" in imagery.unavailable_reason


# ------------------------------------------------------ metering discipline --


async def test_imagery_is_fetched_once_per_address(city: SanFranciscoAdapter) -> None:
    """Street View Static is metered; the same building is not billed twice."""
    client = _google(city, _street_view_available)

    first = await client.fetch(address_id=ADDRESS)
    second = await client.fetch(address_id=ADDRESS)

    assert first == second
    assert client.cache_hits == 1
    assert client.upstream_calls == 2  # metadata + image, once


async def test_the_cache_expires_rather_than_never_refreshing(
    city: SanFranciscoAdapter,
) -> None:
    clock = FixedClock(NOW)
    client = _google(city, _street_view_available, clock=clock, cache_ttl=timedelta(hours=1))

    await client.fetch(address_id=ADDRESS)
    clock.set(NOW + timedelta(hours=2))
    await client.fetch(address_id=ADDRESS)

    assert client.cache_hits == 0
    assert client.upstream_calls == 4


async def test_an_unresolvable_address_costs_the_provider_nothing(
    city: SanFranciscoAdapter,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the provider must not be asked about an unknown address")

    client = _google(city, handler)
    imagery = await client.fetch(address_id=UNKNOWN)

    assert imagery.available is False
    assert client.upstream_calls == 0


async def test_a_spent_rate_budget_is_reported_rather_than_slept_through(
    city: SanFranciscoAdapter,
) -> None:
    """A commander's request is not held open waiting out a token bucket."""
    client = _google(city, _street_view_available, rate_per_second=1.0, burst=1)

    first = await client.fetch(address_id=ADDRESS)
    # A different address, so the cache cannot answer it, at the same instant.
    second = await client.fetch(address_id=OTHER_ADDRESS)

    assert first.available is True
    assert second.available is False
    assert "metered" in second.unavailable_reason
