"""NASA FIRMS active-fire detections, over a region rather than over the city.

The area endpoint is a CSV service with the map key in the **path**:

``/api/area/csv/{MAP_KEY}/{SOURCE}/{west,south,east,north}/{DAYS}``

Three properties of that endpoint drive everything below.

**It answers regionally or it answers nothing.** Measured against the live feed
over the maximum five-day window: San Francisco proper returns zero detections,
Northern California returns hundreds. VIIRS pixels are ~375 m and the product
exists to find wildfire, so a structure fire never crosses the threshold and a
city-only query is empty essentially always. The region is therefore the
subject, the city's own count travels beside it, and
:data:`~firstdue.ports.fireactivity.VIIRS_RESOLUTION_NOTE` ships with every
answer so a zero reads as the ordinary fact it is. See the port's module
docstring for why that is the operational product and not a compromise.

**``DAYS`` outside 1..5 is answered with prose and an HTTP 200.** The body is
``Invalid day range. Expects [1..5].`` -- and a ``csv.DictReader`` pointed at
that reads it as a one-column header and yields no rows, so the naive version of
this adapter reports "no fires" every single time somebody widens the window.
The range is refused at construction *and* the body is checked, because the two
failures are different: one is a misconfiguration a deployment should not start
with, the other is the provider changing its mind about what it accepts.

**The key is on the URL, so no failure may quote the provider.** Every error
below is reported by exception *type*: ``httpx`` reproduces the full request URL
in the message of the error ``raise_for_status`` throws, and a 403 rendered into
a response body would publish ``FIRMS_MAP_KEY`` to the browser. Nothing here
logs a URL, and the port cannot carry one.

Caching and rate limiting follow :class:`~firstdue.sources.framework.ManagedSource`
rather than being re-decided: the bucket is literally its ``RateLimiter``, driven
by the injected clock. The TTL is *tens of minutes* because FIRMS updates on
satellite passes -- a few times a day -- so a shorter one would spend metered
transactions re-fetching a file that cannot have changed. The cache is keyed on
the query, not on the district, since every district in the city shares one
regional answer and buying it once is the point.
"""

from __future__ import annotations

import asyncio
import csv
import io
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

from firstdue.adapters.nasa.power import ProviderDownError, provider_down
from firstdue.errors import ConfigurationError
from firstdue.observability.logging import get_logger
from firstdue.ports.city import CityAdapter
from firstdue.ports.clock import Clock
from firstdue.ports.fireactivity import (
    FIRMS_ATTRIBUTION,
    PROVIDER_FIRMS,
    VIIRS_RESOLUTION_NOTE,
    BoundingBox,
    Confidence,
    FireActivity,
    FireDetection,
    FireWeather,
    FireWeatherClient,
    summarize,
    unavailable,
)
from firstdue.sources.framework import RateLimiter

if TYPE_CHECKING:  # pragma: no cover - import shape only
    import httpx

logger = get_logger(__name__)

FIRMS_AREA_URL: Final[str] = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

#: Suomi NPP near-real-time. The NRT products are the only ones that answer a
#: question about today; the science-quality collections lag by months.
DEFAULT_SOURCE: Final[str] = "VIIRS_SNPP_NRT"

#: The endpoint's own limit. Not a policy of ours -- FIRMS answers anything else
#: with an error page. See the module docstring.
MIN_DAYS: Final[int] = 1
MAX_DAYS: Final[int] = 5

#: The Bay Area and Northern California: the area whose fires actually reach a
#: San Francisco company officer, through mutual aid, crew availability, smoke,
#: and red-flag posture. A configuration default, overridable per deployment --
#: which is why the box that was queried is carried on every answer.
DEFAULT_REGION: Final[BoundingBox] = BoundingBox(west=-124.5, south=36.5, east=-119.5, north=40.5)

#: San Francisco proper. Used only to split the count; it is never queried on
#: its own, because on its own it returns nothing.
DEFAULT_CITY_BOUNDS: Final[BoundingBox] = BoundingBox(
    west=-122.55, south=37.70, east=-122.35, north=37.84
)

#: FIRMS refreshes on satellite passes, a few times a day. Tens of minutes, not
#: seconds -- a shorter TTL buys metered transactions for an identical file.
DEFAULT_CACHE_TTL: Final[timedelta] = timedelta(minutes=20)

#: A commander is waiting. A provider that has not answered by now is not going
#: to be useful, and the rest of the console should not wait for it.
DEFAULT_DEADLINE_S: Final[float] = 8.0

#: How many detections travel to the browser. The count is always the true one;
#: this bounds the payload, and ``truncated`` says when the two differ.
DEFAULT_MAX_DETECTIONS: Final[int] = 500

#: A five-day regional CSV is tens of kilobytes. Anything past this is not a
#: detection table, and it is not held in memory to find out.
MAX_BODY_BYTES: Final[int] = 8 * 1024 * 1024

#: The marker in FIRMS' plain-text rejection of a day range. Matched
#: case-insensitively against the head of the body, never against a whole file.
INVALID_DAY_RANGE_MARKER: Final[str] = "invalid day range"

#: The columns this adapter cannot do without. Verified against the live header:
#: ``latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,
#: instrument,confidence,version,bright_ti5,frp,daynight``. Checked as a subset
#: so FIRMS adding a column is not an outage.
REQUIRED_COLUMNS: Final[frozenset[str]] = frozenset(
    {"latitude", "longitude", "acq_date", "acq_time"}
)

#: VIIRS ships a letter. A letter in a console is a code an officer looks up.
CONFIDENCE_CODES: Final[dict[str, Confidence]] = {"l": "low", "n": "nominal", "h": "high"}

#: VIIRS ships one letter for the half of the orbit the pass was on.
DAYNIGHT_CODES: Final[dict[str, Literal["day", "night", "unknown"]]] = {"D": "day", "N": "night"}


class _FirmsRefusedError(Exception):
    """FIRMS answered, but not with detections.

    Distinct from :class:`ProviderDownError` because the two are different
    operational facts: one is an unreachable provider, the other is a reachable
    provider whose answer this build cannot honestly parse. Carries a refusal
    *code* from the port's table and never provider text.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class _CacheEntry:
    """Parsed detections, shared by every district that asks about this region."""

    detections: tuple[FireDetection, ...]
    expires_at: datetime


class UnconfiguredFireActivityClient:
    """Live mode with no FIRMS map key: the documented state, not an error.

    The twin of :class:`~firstdue.sources.framework.UnconfiguredFetcher`. A
    deployment without a key gets a console that says the fire-activity feed was
    never configured, rather than one where the regional map is quietly absent
    and an officer concludes the region is quiet. It never falls back to the
    synthetic adapter: a live process drawing invented fires on a commander's
    map is the worst failure this component can have.
    """

    provider_label: Final[str] = ""

    async def fetch(self, *, district_id: str) -> FireActivity:
        return FireActivity.refused(district_id, unavailable("unconfigured"))


class NasaFirmsClient:
    """Regional VIIRS detections, with NASA POWER fire weather beside them."""

    provider_label: Final[str] = PROVIDER_FIRMS

    def __init__(
        self,
        *,
        map_key: str,
        city: CityAdapter,
        clock: Clock,
        region: BoundingBox = DEFAULT_REGION,
        city_bounds: BoundingBox = DEFAULT_CITY_BOUNDS,
        weather: FireWeatherClient | None = None,
        source: str = DEFAULT_SOURCE,
        days: int = MAX_DAYS,
        cache_ttl: timedelta = DEFAULT_CACHE_TTL,
        deadline_s: float = DEFAULT_DEADLINE_S,
        rate_per_second: float = 0.5,
        burst: int = 3,
        max_detections: int = DEFAULT_MAX_DETECTIONS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not map_key:
            # Refusing to construct is what keeps "unconfigured" a single
            # decision made at wiring time: see UnconfiguredFireActivityClient.
            raise ConfigurationError(
                "NASA FIRMS requires FIRMS_MAP_KEY; "
                "wire UnconfiguredFireActivityClient when there is none"
            )
        if not MIN_DAYS <= days <= MAX_DAYS:
            # A wider window is not a bigger answer, it is an error page that
            # parses as zero fires. Better to refuse to start.
            raise ConfigurationError(
                "NASA FIRMS accepts a window of one to five days",
                details={"days": str(days)},
            )
        if not _encloses(region, city_bounds):
            # Otherwise ``in_city_count`` counts over an area the query never
            # covered, and would read as "no fires in the city" for a city the
            # request never asked about.
            raise ConfigurationError(
                "the city box has to lie inside the region the detections are counted over",
                details={"region": region.as_query(), "city": city_bounds.as_query()},
            )
        self._map_key = map_key
        self._city = city
        self._clock = clock
        self._region = region
        self._city_bounds = city_bounds
        self._weather = weather
        self._source = source
        self._days = days
        self._cache_ttl = cache_ttl
        self._deadline_s = deadline_s
        self._limiter = RateLimiter(rate_per_second=rate_per_second, burst=burst)
        self._max_detections = max_detections
        self._transport = transport
        self._cache: dict[str, _CacheEntry] = {}
        self.cache_hits = 0
        self.upstream_calls = 0

    async def fetch(self, *, district_id: str) -> FireActivity:
        now = self._clock.now()

        if district_id not in set(self._city.list_districts()):
            # Cached by nobody and costing nothing: the city adapter will not
            # learn a new district mid-shift, and a typo must not buy a
            # metered transaction.
            return FireActivity.refused(district_id, unavailable("district_unknown"))

        key = f"{self._source}|{self._region.as_query()}|{self._days}"
        cached = self._cache.get(key)
        if cached is not None and now < cached.expires_at:
            self.cache_hits += 1
            return await self._assemble(district_id, cached.detections)

        if self._limiter.take(now) > 0.0:
            # Reported, never slept through -- the same choice ManagedSource
            # makes. Holding a commander's request open to wait out a token
            # bucket is worse than saying the budget is spent.
            logger.warning("firms_rate_limited", extra={"district_id": district_id})
            return FireActivity.refused(district_id, unavailable("rate_limited"))

        try:
            async with asyncio.timeout(self._deadline_s):
                detections = await self._fetch_detections()
        except TimeoutError:
            logger.warning("firms_deadline_exceeded", extra={"district_id": district_id})
            return FireActivity.refused(district_id, unavailable("deadline"))
        except ProviderDownError as exc:
            # Type only. The message would carry the signed URL, and the URL is
            # the key.
            logger.warning("firms_provider_down", extra={"error_type": exc.error_type})
            code = "deadline" if exc.timed_out else "provider_unreachable"
            return FireActivity.refused(district_id, unavailable(code))
        except _FirmsRefusedError as exc:
            logger.warning("firms_unusable_answer", extra={"refusal_code": exc.code})
            return FireActivity.refused(district_id, unavailable(exc.code))

        # An empty region keeps: "the feed answered and found nothing" is a
        # true, cacheable fact. Outages and spent budgets never reach here,
        # because caching those would turn one bad minute into twenty.
        self._cache[key] = _CacheEntry(detections=detections, expires_at=now + self._cache_ttl)
        return await self._assemble(district_id, detections)

    # ------------------------------------------------------------ internals

    async def _assemble(
        self, district_id: str, detections: tuple[FireDetection, ...]
    ) -> FireActivity:
        """Attach the city split and the fire-weather block to a parsed region.

        Fire weather is fetched here rather than in ``fetch`` so a cache hit on
        the detections still gets it, and it can refuse on its own: POWER being
        down must cost the commander the weather panel, not the map.
        """
        shown = detections[: self._max_detections]
        in_city = sum(1 for detection in detections if detection.in_city)
        latitude, longitude = self._city_bounds.center()
        weather = (
            await self._weather.fetch(latitude=latitude, longitude=longitude)
            if self._weather is not None
            else FireWeather.refused(unavailable("weather_unconfigured"))
        )
        return FireActivity(
            district_id=district_id,
            available=True,
            provider=PROVIDER_FIRMS,
            region=self._region,
            city=self._city_bounds,
            window_days=self._days,
            detections=shown,
            regional_count=len(detections),
            in_city_count=in_city,
            truncated=len(shown) < len(detections),
            summary=summarize(regional=len(detections), in_city=in_city, window_days=self._days),
            resolution_note=VIIRS_RESOLUTION_NOTE,
            attribution=FIRMS_ATTRIBUTION,
            weather=weather,
        )

    async def _fetch_detections(self) -> tuple[FireDetection, ...]:
        import httpx

        self.upstream_calls += 1
        try:
            async with httpx.AsyncClient(
                timeout=self._deadline_s, transport=self._transport
            ) as client:
                response = await client.get(self._url())
                response.raise_for_status()
                raw = response.content
        except Exception as exc:
            # Deliberately not `str(exc)` and deliberately not the response
            # body: both contain the signed URL on the path that matters most.
            raise provider_down(exc) from exc

        if len(raw) > MAX_BODY_BYTES:
            raise _FirmsRefusedError("oversized_response")
        return self._parse(raw.decode("utf-8", errors="replace"))

    def _url(self) -> str:
        """The only place the map key is ever attached to anything.

        FIRMS takes it as a path segment rather than a query parameter, so it
        cannot be separated out into ``params`` and kept off the URL string --
        which is exactly why nothing in this module logs or returns a URL.
        """
        return (
            f"{FIRMS_AREA_URL}/{self._map_key}/{self._source}/"
            f"{self._region.as_query()}/{self._days}"
        )

    def _parse(self, body: str) -> tuple[FireDetection, ...]:
        """CSV to detections, refusing anything that is not a detection table.

        Three answers this must survive without raising, all of them observed or
        documented: the plain-text day-range rejection, a file with only a
        header, and a row with a field that will not parse.
        """
        text = body.strip()
        if INVALID_DAY_RANGE_MARKER in text[:200].lower():
            raise _FirmsRefusedError("invalid_window")

        reader = csv.DictReader(io.StringIO(text))
        columns = frozenset(reader.fieldnames or ())
        if not columns >= REQUIRED_COLUMNS:
            # Covers an empty body, an HTML error page, and any future prose
            # rejection: none of those is a count of zero fires.
            raise _FirmsRefusedError("malformed_response")

        detections: list[FireDetection] = []
        skipped = 0
        for row in reader:
            detection = _detection(row, self._city_bounds)
            if detection is None:
                skipped += 1
                continue
            detections.append(detection)

        if skipped:
            # Worth knowing about and not worth failing over: one unparseable
            # row must not delete the other 265.
            logger.info("firms_rows_skipped", extra={"skipped": skipped})

        # Newest first, so a truncated payload keeps the detections a commander
        # would look at rather than an arbitrary slice.
        detections.sort(key=lambda detection: detection.acquired_at, reverse=True)
        return tuple(detections)

    # -------------------------------------------------------- test controls

    def clear_cache(self) -> None:
        """Drop the cached region, as ``ManagedSource.clear_cache`` does.

        Exists so a test can prove the rate limiter and the deadline behave
        without reaching into a private attribute to get past the cache first.
        """
        self._cache.clear()


def _encloses(region: BoundingBox, city: BoundingBox) -> bool:
    """Whether the region fully contains the city box."""
    return (
        region.west <= city.west
        and city.east <= region.east
        and region.south <= city.south
        and city.north <= region.north
    )


def _detection(row: dict[str, str | None], city: BoundingBox) -> FireDetection | None:
    """One CSV row, or ``None`` if it was not a usable detection.

    Never raises. A row this cannot read is dropped and counted, because the
    alternative -- letting one malformed line abort the parse -- turns a
    cosmetic feed defect into "no fire activity".
    """
    latitude = _number(row.get("latitude"))
    longitude = _number(row.get("longitude"))
    if latitude is None or longitude is None:
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None

    acquired_at = _acquired_at(row.get("acq_date"), row.get("acq_time"))
    if acquired_at is None:
        return None

    frp = _number(row.get("frp"))
    return FireDetection(
        latitude=latitude,
        longitude=longitude,
        confidence=CONFIDENCE_CODES.get((row.get("confidence") or "").strip().lower(), "unknown"),
        # A negative or absent radiative power is reported as zero rather than
        # dropped: the detection happened, only its size is unusable.
        frp_mw=max(0.0, frp) if frp is not None else 0.0,
        acquired_at=acquired_at,
        satellite=_platform(row),
        # I4 is the fire channel. I5 is in the feed too and is not read: two
        # brightness temperatures on one detection invite being differenced into
        # an "anomaly", which is not what either of them means.
        brightness_k=_positive(_number(row.get("bright_ti4"))),
        daynight=DAYNIGHT_CODES.get((row.get("daynight") or "").strip().upper(), "unknown"),
        in_city=city.contains(latitude, longitude),
    )


def _positive(value: float | None) -> float | None:
    """A brightness temperature, or nothing. Kelvin cannot be negative."""
    return value if value is not None and value > 0.0 else None


def _number(raw: str | None) -> float | None:
    try:
        value = float((raw or "").strip())
    except ValueError:
        return None
    # NaN and the infinities survive float(); none of them is a coordinate.
    return value if math.isfinite(value) else None


def _acquired_at(raw_date: str | None, raw_time: str | None) -> datetime | None:
    """``2026-08-20`` plus ``1034`` -- and ``934``, which is 09:34, not 93:40."""
    day = (raw_date or "").strip()
    if not day:
        return None
    digits = "".join(char for char in (raw_time or "") if char.isdigit())[:4]
    try:
        return datetime.strptime(f"{day}{digits.rjust(4, '0')}", "%Y-%m-%d%H%M").replace(tzinfo=UTC)
    except ValueError:
        return None


def _platform(row: dict[str, str | None]) -> str:
    """``VIIRS (N)`` -- instrument and platform as the feed named them."""
    instrument = (row.get("instrument") or "").strip()
    satellite = (row.get("satellite") or "").strip()
    if instrument and satellite:
        return f"{instrument} ({satellite})"[:60]
    return (instrument or satellite or "unidentified platform")[:60]
