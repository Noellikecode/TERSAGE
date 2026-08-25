"""Street View first, satellite second, both through plain ``httpx``.

Two Google Maps Platform endpoints, tried in that order, because they answer
different questions and only one of them answers the commander's:

* **Street View Static** is a facade photograph. It shows the door, the window
  arrangement, the security bars, the storeys you can count from the street --
  the things a company officer sizes up from the sidewalk.
* **Static Maps satellite** is a roof. It is the fallback, and it is genuinely
  worse for this job: it settles footprint and roof furniture and nothing about
  how anyone gets in.

**The metadata call is not an optimisation.** Street View Static answers a
location with no panorama by returning a grey "no imagery available" tile, with
HTTP 200, which a console would render inside the building pane as though it
were the building. ``streetview/metadata`` is free and says ``ZERO_RESULTS``
first, so a location with no coverage becomes a clean miss and then a satellite
fallback -- never a grey rectangle presented as a photograph.

**The key stays here.** This adapter is the only thing in the process that puts
``key=`` on a URL. It returns bytes; the port carries a data URL; the browser
never sees a signed URL, a redirect to one, or an error message containing one.
That last one is why every failure below is logged and reported by exception
*type*: ``httpx`` puts the full request URL in ``HTTPStatusError``'s message,
and a 403 rendered into a response body would publish the key.

Caching and rate limiting follow :class:`~firstdue.sources.framework.ManagedSource`
rather than re-deciding them: the token bucket is literally its ``RateLimiter``,
driven by the injected clock, and the cache is the same expiring-entry map with
a much longer TTL -- what a building looks like is not an hourly question, and
Street View Static is metered per request.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from firstdue.errors import ConfigurationError
from firstdue.observability.logging import get_logger
from firstdue.ports.city import CityAdapter
from firstdue.ports.clock import Clock
from firstdue.ports.imagery import (
    GOOGLE_ATTRIBUTION,
    PROVIDER_SATELLITE,
    PROVIDER_STREET_VIEW,
    BuildingImagery,
    ImageryView,
    unavailable,
)
from firstdue.sources.framework import RateLimiter

if TYPE_CHECKING:  # pragma: no cover - import shape only
    import httpx

logger = get_logger(__name__)

STREET_VIEW_URL: Final[str] = "https://maps.googleapis.com/maps/api/streetview"
STREET_VIEW_METADATA_URL: Final[str] = "https://maps.googleapis.com/maps/api/streetview/metadata"
STATIC_MAP_URL: Final[str] = "https://maps.googleapis.com/maps/api/staticmap"

#: Imagery does not change hour to hour, and every miss is a billed request.
DEFAULT_CACHE_TTL: Final[timedelta] = timedelta(hours=24)

#: A commander is waiting. A provider that has not answered by now is not going
#: to be useful, and the rest of the building pane should not wait for it.
DEFAULT_DEADLINE_S: Final[float] = 6.0

#: 640 is the Street View Static ceiling without a premium plan, and it is
#: already more resolution than the console's imagery pane uses.
DEFAULT_SIZE: Final[str] = "640x480"

#: High enough to see one structure rather than a block.
DEFAULT_SATELLITE_ZOOM: Final[int] = 20

#: A data URL is held in memory and shipped inside a JSON body. Anything past
#: this is not a building photograph, it is a problem.
MAX_IMAGE_BYTES: Final[int] = 4 * 1024 * 1024


class _ProviderDownError(Exception):
    """The provider could not be reached or answered an error.

    Carries an exception *type name* and nothing else, deliberately: the message
    ``httpx`` would give us contains the signed URL, and therefore the key.
    """

    def __init__(self, error_type: str, *, timed_out: bool = False) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        #: Distinguished so a slow provider reads as a blown deadline rather
        #: than as an outage -- an officer reacts differently to the two.
        self.timed_out = timed_out


def _provider_down(exc: Exception) -> _ProviderDownError:
    import httpx

    return _ProviderDownError(type(exc).__name__, timed_out=isinstance(exc, httpx.TimeoutException))


@dataclass(slots=True)
class _CacheEntry:
    imagery: BuildingImagery
    expires_at: datetime


class UnconfiguredImageryClient:
    """Live mode with no Maps key: the documented state, not an error.

    The twin of :class:`~firstdue.sources.framework.UnconfiguredFetcher`. A
    deployment without a key gets a console that says imagery was never
    configured, rather than one where the photograph pane is quietly absent and
    an officer concludes the building has no photograph. It never falls back to
    the synthetic placeholder: a live process serving a drawing would be lying
    about where its picture came from.
    """

    provider_label: Final[str] = ""

    async def fetch(self, *, address_id: str, view: ImageryView = "street") -> BuildingImagery:
        # Every view is equally unconfigured without a key.
        return BuildingImagery.refused(address_id, unavailable("unconfigured"))


class GoogleImageryClient:
    """Street View Static, with a Static Maps satellite fallback."""

    #: The provider this client prefers. What a given answer actually came from
    #: is on the answer, because the fallback is not an implementation detail --
    #: a roof and a facade are different evidence.
    provider_label: Final[str] = PROVIDER_STREET_VIEW

    def __init__(
        self,
        *,
        api_key: str,
        city: CityAdapter,
        clock: Clock,
        cache_ttl: timedelta = DEFAULT_CACHE_TTL,
        deadline_s: float = DEFAULT_DEADLINE_S,
        rate_per_second: float = 1.0,
        burst: int = 5,
        max_concurrency: int = 4,
        size: str = DEFAULT_SIZE,
        satellite_zoom: int = DEFAULT_SATELLITE_ZOOM,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            # Refusing to construct is what keeps "unconfigured" a single
            # decision made at wiring time: see UnconfiguredImageryClient.
            raise ConfigurationError(
                "Google imagery requires GOOGLE_MAPS_API_KEY; "
                "wire UnconfiguredImageryClient when there is none"
            )
        self._api_key = api_key
        self._city = city
        self._clock = clock
        self._cache_ttl = cache_ttl
        self._deadline_s = deadline_s
        self._size = size
        self._satellite_zoom = satellite_zoom
        self._limiter = RateLimiter(rate_per_second=rate_per_second, burst=burst)
        # Street View Static is metered and a district pane can open several
        # buildings at once; the gate bounds what one process can spend at a
        # time regardless of how many requests arrive together.
        self._gate = asyncio.Semaphore(max_concurrency)
        self._transport = transport
        self._cache: dict[str, _CacheEntry] = {}
        self.cache_hits = 0
        self.upstream_calls = 0

    async def fetch(self, *, address_id: str, view: ImageryView = "street") -> BuildingImagery:
        now = self._clock.now()
        cache_key = address_id if view == "street" else f"{address_id}#{view}"
        cached = self._cache.get(cache_key)
        if cached is not None and now < cached.expires_at:
            self.cache_hits += 1
            return cached.imagery

        address = self._city.get_address(address_id)
        if address is None:
            # Cached: the city adapter will not learn this address mid-shift,
            # and a repeated miss should not repeatedly cost anything.
            refusal = BuildingImagery.refused(address_id, unavailable("address_unresolved"))
            return self._remember(cache_key, refusal, now)

        if self._limiter.take(now) > 0.0:
            # The debt is reported, never slept through -- the same choice
            # ManagedSource makes. Holding a commander's request open to wait
            # out a token bucket is worse than saying the budget is spent.
            logger.warning("imagery_rate_limited", extra={"address_id": address_id})
            return BuildingImagery.refused(address_id, unavailable("rate_limited"))

        async with self._gate:
            try:
                async with asyncio.timeout(self._deadline_s):
                    imagery = await self._fetch_live(
                        address_id, address.latitude, address.longitude, view=view
                    )
            except TimeoutError:
                logger.warning("imagery_deadline_exceeded", extra={"address_id": address_id})
                return BuildingImagery.refused(address_id, unavailable("deadline"))
            except _ProviderDownError as exc:
                logger.warning("imagery_provider_down", extra={"error_type": exc.error_type})
                code = "deadline" if exc.timed_out else "provider_unreachable"
                return BuildingImagery.refused(address_id, unavailable(code))

        return self._remember(cache_key, imagery, now)

    # ------------------------------------------------------------ internals

    def _remember(
        self, address_id: str, imagery: BuildingImagery, now: datetime
    ) -> BuildingImagery:
        """Cache an answer that will still be true tomorrow.

        A photograph and a genuine absence of coverage both keep; an outage and
        a spent rate budget do not reach here, because caching those would turn
        one bad minute into a day of a building with no picture.
        """
        self._cache[address_id] = _CacheEntry(imagery=imagery, expires_at=now + self._cache_ttl)
        return imagery

    async def _fetch_live(
        self,
        address_id: str,
        latitude: float,
        longitude: float,
        *,
        view: ImageryView = "street",
    ) -> BuildingImagery:
        import httpx

        location = f"{latitude:.6f},{longitude:.6f}"
        async with httpx.AsyncClient(timeout=self._deadline_s, transport=self._transport) as client:
            # An aerial is asked for straight down and nothing else. Falling
            # back to Street View here would hand a commander a kerb while the
            # panel above it said "aerial", which is the one thing this must
            # not do: the free metadata probe is skipped with it.
            metadata = None if view == "aerial" else await self._panorama_metadata(client, location)
            if metadata is not None:
                found = await self._image(
                    client,
                    STREET_VIEW_URL,
                    {
                        "size": self._size,
                        # Pin the exact panorama the metadata described, so the
                        # picture is the one whose capture date we are about to
                        # print beside it.
                        "pano": str(metadata.get("pano_id") or ""),
                        "location": location,
                        "return_error_code": "true",
                    },
                )
                if found is not None:
                    payload, content_type = found
                    date = str(metadata.get("date") or "")
                    return BuildingImagery(
                        address_id=address_id,
                        available=True,
                        provider=PROVIDER_STREET_VIEW,
                        content_type=content_type,
                        data_url=_data_url(payload, content_type),
                        attribution=str(metadata.get("copyright") or GOOGLE_ATTRIBUTION),
                        captured_hint=f"Street View panorama captured {date}" if date else "",
                    )

            found = await self._image(
                client,
                STATIC_MAP_URL,
                {
                    "center": location,
                    "zoom": str(self._satellite_zoom),
                    "size": self._size,
                    "maptype": "satellite",
                },
            )

        if found is None:
            return BuildingImagery.refused(address_id, unavailable("no_coverage"))
        payload, content_type = found
        return BuildingImagery(
            address_id=address_id,
            available=True,
            provider=PROVIDER_SATELLITE,
            content_type=content_type,
            data_url=_data_url(payload, content_type),
            attribution=GOOGLE_ATTRIBUTION,
            # Static Maps says nothing about when the tile was flown, and a
            # guess is exactly the field a commander would over-trust.
            captured_hint="",
        )

    async def _panorama_metadata(
        self, client: httpx.AsyncClient, location: str
    ) -> dict[str, Any] | None:
        """The free coverage check. ``None`` means no panorama, not a failure."""
        self.upstream_calls += 1
        try:
            response = await client.get(
                STREET_VIEW_METADATA_URL, params=self._params({"location": location})
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise _provider_down(exc) from exc

        if not isinstance(payload, dict):
            raise _ProviderDownError("MetadataShape")
        status = str(payload.get("status") or "")
        if status != "OK":
            # ZERO_RESULTS is the ordinary answer for an alley, a private drive,
            # or a building set back from any street a car has driven.
            logger.info("imagery_no_panorama", extra={"provider_status": status})
            return None
        typed: dict[str, Any] = payload
        return typed

    async def _image(
        self, client: httpx.AsyncClient, url: str, params: dict[str, str]
    ) -> tuple[bytes, str] | None:
        """Fetch image bytes, or ``None`` when the answer was not an image."""
        self.upstream_calls += 1
        try:
            response = await client.get(url, params=self._params(params))
            response.raise_for_status()
            payload = response.content
        except Exception as exc:
            raise _provider_down(exc) from exc

        content_type = str(response.headers.get("content-type", "")).split(";")[0].strip()
        if not content_type.startswith("image/") or not payload:
            logger.warning("imagery_not_an_image", extra={"content_type": content_type})
            return None
        if len(payload) > MAX_IMAGE_BYTES:
            logger.warning("imagery_too_large", extra={"bytes": len(payload)})
            return None
        return payload, content_type

    def _params(self, params: dict[str, str]) -> dict[str, str]:
        """The only place ``key=`` is ever attached to anything."""
        return {**params, "key": self._api_key}


def _data_url(payload: bytes, content_type: str) -> str:
    """Inline the bytes, so the browser is handed pixels and never a signed URL."""
    return f"data:{content_type};base64,{base64.b64encode(payload).decode('ascii')}"
