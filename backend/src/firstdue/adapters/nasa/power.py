"""NASA POWER hourly reanalysis: how hot, dry and windy it has *been*.

Three parameters, because three are what fire weather is made of --
``T2M`` (air temperature at 2 m), ``RH2M`` (relative humidity at 2 m) and
``WS10M`` (wind speed at 10 m). Hot, dry and windy is the whole signal.

**This is reanalysis, and the adapter is built around that rather than around
it.** POWER assimilates a model run; it does not report an observation, and it
lags real time. Measured against the live endpoint, asking through today, the
newest hour carrying a value was days back. So every value that leaves here is
stamped with the hour it describes, the block names the window those hours
span, and :data:`~firstdue.ports.fireactivity.POWER_IS_NOT_NOW` rides along to
say in words that this is not current weather. Current wind already reaches the
fleet from the NWS feed in :mod:`firstdue.sources.catalog`; the failure this
adapter must not enable is a console showing a four-day-old wind speed beside a
live one and letting a commander read them as the same kind of thing.

Two parsing decisions matter:

* ``-999`` is POWER's fill value, not a temperature. It is filtered before a
  reading is constructed, and a series that is *entirely* fill is a stated
  refusal rather than a silent absence.
* The request pins ``time-standard=UTC``. POWER defaults to local solar time,
  whose hour keys are offset from UTC by the point's longitude -- parsing those
  as UTC would misdate the window by hours while looking perfectly well-formed.

The lookback is deliberately wider than the observed lag. A window that only
just reaches back far enough returns nothing the day the lag grows, and "no
usable hours" is a much worse answer than "the newest hour is older than usual".
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock
from firstdue.ports.fireactivity import (
    POWER_ATTRIBUTION,
    POWER_IS_NOT_NOW,
    PROVIDER_POWER,
    FireWeather,
    FireWeatherReading,
    unavailable,
)
from firstdue.sources.framework import RateLimiter

if TYPE_CHECKING:  # pragma: no cover - import shape only
    import httpx

logger = get_logger(__name__)

POWER_HOURLY_URL: Final[str] = "https://power.larc.nasa.gov/api/temporal/hourly/point"

#: Hot, dry, windy -- in the order a console reads them out.
PARAMETERS: Final[tuple[str, ...]] = ("T2M", "RH2M", "WS10M")

#: Used only when POWER omits its own metadata block. It normally ships one,
#: and the provider's own wording is preferred over ours.
FALLBACK_LABELS: Final[dict[str, str]] = {
    "T2M": "Temperature at 2 Meters",
    "RH2M": "Relative Humidity at 2 Meters",
    "WS10M": "Wind Speed at 10 Meters",
}
FALLBACK_UNITS: Final[dict[str, str]] = {"T2M": "C", "RH2M": "%", "WS10M": "m/s"}

#: POWER's fill value. Anything at or below this is absence, not measurement.
FILL_VALUE: Final[float] = -999.0

#: Wider than the observed lag on purpose -- see the module docstring.
DEFAULT_LOOKBACK_DAYS: Final[int] = 7

#: Reanalysis for a fixed point and a fixed date span does not change within an
#: hour, and the span only rolls over at midnight UTC.
DEFAULT_CACHE_TTL: Final[timedelta] = timedelta(hours=1)

#: Strictly under the fire-activity deadline, so a slow POWER degrades the
#: weather block on its own rather than taking the detections down with it.
DEFAULT_DEADLINE_S: Final[float] = 5.0

#: A hourly point response for one week is tens of kilobytes. Anything past this
#: is not fire weather, it is a problem, and it is not held in memory.
MAX_BODY_BYTES: Final[int] = 4 * 1024 * 1024


class ProviderDownError(Exception):
    """A NASA endpoint could not be reached or answered an error.

    Carries an exception *type name* and nothing else, deliberately. POWER takes
    no credential, but its sibling in :mod:`firstdue.adapters.nasa.firms` puts a
    map key in the request path, and ``httpx`` reproduces the full URL in the
    message of the error a non-200 raises. One error type for both clients means
    the keyed one cannot quietly grow a laxer habit than the unkeyed one.
    """

    def __init__(self, error_type: str, *, timed_out: bool = False) -> None:
        super().__init__(error_type)
        self.error_type = error_type
        #: Distinguished so a slow provider reads as a blown deadline rather
        #: than as an outage -- an officer reacts differently to the two.
        self.timed_out = timed_out


def provider_down(exc: Exception) -> ProviderDownError:
    import httpx

    return ProviderDownError(type(exc).__name__, timed_out=isinstance(exc, httpx.TimeoutException))


@dataclass(slots=True)
class _CacheEntry:
    weather: FireWeather
    expires_at: datetime


class NasaPowerClient:
    """Recent temperature, humidity and wind for one point. Never raises."""

    provider_label: Final[str] = PROVIDER_POWER

    def __init__(
        self,
        *,
        clock: Clock,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        cache_ttl: timedelta = DEFAULT_CACHE_TTL,
        deadline_s: float = DEFAULT_DEADLINE_S,
        rate_per_second: float = 0.5,
        burst: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._clock = clock
        self._lookback_days = max(1, lookback_days)
        self._cache_ttl = cache_ttl
        self._deadline_s = deadline_s
        self._limiter = RateLimiter(rate_per_second=rate_per_second, burst=burst)
        self._transport = transport
        self._cache: dict[str, _CacheEntry] = {}
        self.cache_hits = 0
        self.upstream_calls = 0

    async def fetch(self, *, latitude: float, longitude: float) -> FireWeather:
        now = self._clock.now()
        end = now.date()
        start = end - timedelta(days=self._lookback_days)
        # Rounded, because POWER's grid is coarser than six decimal places and
        # two districts a block apart must not each buy their own request.
        key = f"{latitude:.2f},{longitude:.2f}|{start:%Y%m%d}|{end:%Y%m%d}"

        cached = self._cache.get(key)
        if cached is not None and now < cached.expires_at:
            self.cache_hits += 1
            return cached.weather

        if self._limiter.take(now) > 0.0:
            # Reported, never slept through -- the same choice ManagedSource
            # makes. Holding a commander's request open to wait out a token
            # bucket is worse than saying the budget is spent.
            logger.warning("power_rate_limited")
            return FireWeather.refused(unavailable("rate_limited"))

        try:
            async with asyncio.timeout(self._deadline_s):
                weather = await self._fetch_live(latitude, longitude, start, end)
        except TimeoutError:
            logger.warning("power_deadline_exceeded")
            return FireWeather.refused(unavailable("deadline"))
        except ProviderDownError as exc:
            logger.warning("power_provider_down", extra={"error_type": exc.error_type})
            code = "deadline" if exc.timed_out else "provider_unreachable"
            return FireWeather.refused(unavailable(code))

        # A refusal is cached alongside a reading only when it is *stable*: an
        # entirely-filled window will still be entirely filled a minute from
        # now, whereas an outage will not, and caching an outage would turn one
        # bad minute into an hour with no fire weather.
        self._cache[key] = _CacheEntry(weather=weather, expires_at=now + self._cache_ttl)
        return weather

    # ------------------------------------------------------------ internals

    async def _fetch_live(
        self, latitude: float, longitude: float, start: date, end: date
    ) -> FireWeather:
        import httpx

        params = {
            "parameters": ",".join(PARAMETERS),
            "community": "RE",
            "longitude": f"{longitude:.4f}",
            "latitude": f"{latitude:.4f}",
            "start": f"{start:%Y%m%d}",
            "end": f"{end:%Y%m%d}",
            "format": "JSON",
            # See the module docstring: LST keys would misdate the window.
            "time-standard": "UTC",
        }
        self.upstream_calls += 1
        try:
            async with httpx.AsyncClient(
                timeout=self._deadline_s, transport=self._transport
            ) as client:
                response = await client.get(POWER_HOURLY_URL, params=params)
                response.raise_for_status()
                body = response.content
                if len(body) > MAX_BODY_BYTES:
                    raise ProviderDownError("PowerBodyTooLarge")
                payload = response.json()
        except ProviderDownError:
            raise
        except Exception as exc:
            raise provider_down(exc) from exc

        return _read(payload)


def _read(payload: object) -> FireWeather:
    """Turn one POWER response into readings, or into a stated refusal.

    Defensive at every hop: a response that is not the documented shape is a
    refusal, never an empty reading list, because "no fire weather" and "we
    could not read the fire weather" are different claims.
    """
    if not isinstance(payload, dict):
        return FireWeather.refused(unavailable("weather_malformed"))
    properties = payload.get("properties")
    series_by_name = properties.get("parameter") if isinstance(properties, dict) else None
    if not isinstance(series_by_name, dict):
        return FireWeather.refused(unavailable("weather_malformed"))
    metadata = payload.get("parameters")
    meta: dict[str, Any] = metadata if isinstance(metadata, dict) else {}

    readings: list[FireWeatherReading] = []
    observed: list[datetime] = []
    for name in PARAMETERS:
        series = series_by_name.get(name)
        if not isinstance(series, dict):
            continue
        usable = _usable_hours(series)
        if not usable:
            continue
        observed.extend(stamp for stamp, _ in usable)
        stamp, value = usable[-1]
        info = meta.get(name)
        info_dict: dict[str, Any] = info if isinstance(info, dict) else {}
        readings.append(
            FireWeatherReading(
                parameter=name,
                label=str(info_dict.get("longname") or FALLBACK_LABELS.get(name, name))[:120],
                value=round(value, 2),
                unit=str(info_dict.get("units") or FALLBACK_UNITS.get(name, ""))[:20],
                observed_at=stamp,
            )
        )

    if not readings or not observed:
        return FireWeather.refused(unavailable("no_usable_hours"))

    return FireWeather(
        available=True,
        provider=PROVIDER_POWER,
        window_start=min(observed),
        window_end=max(observed),
        readings=tuple(readings),
        caveat=POWER_IS_NOT_NOW,
        attribution=POWER_ATTRIBUTION,
    )


def _usable_hours(series: dict[str, Any]) -> list[tuple[datetime, float]]:
    """Every hour that carried a real value, oldest first.

    Fill values are dropped rather than clamped: ``-999`` degrees is not a cold
    hour, and a mean taken over it would be a number nobody measured.
    """
    usable: list[tuple[datetime, float]] = []
    for key in sorted(series):
        stamp = _hour(key)
        if stamp is None:
            continue
        raw = series[key]
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            continue
        value = float(raw)
        if not math.isfinite(value) or value <= FILL_VALUE:
            continue
        usable.append((stamp, value))
    return usable


def _hour(key: object) -> datetime | None:
    """``YYYYMMDDHH`` to an aware UTC datetime, or ``None`` if it is not one."""
    if not isinstance(key, str) or len(key) != 10 or not key.isdigit():
        return None
    try:
        return datetime.strptime(key, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None
