"""Deterministic regional fire activity that admits, in the data, to being invented.

Not a stub returning a constant, and -- more important -- not a plausible one.
Every detection is derived from a digest of the district id and the box it is
scattered in, so the same district always produces the same map, two districts
produce different ones, and a seeded demo replays byte-identically. That is the
property ADR 0003 asks of every fake.

**Why it announces itself.** Fake mode is the default and the entire test suite,
and this is data a human reads as a map of fires. A synthetic detection that did
not admit to being synthetic would be an invented wildfire on a commander's
display -- so the platform on every detection is literally ``SYNTHETIC``, the
attribution says no NASA endpoint was contacted, and the summary says it in the
sentence a console prints above the map. A hidden simulation is worse than an
admitted one.

**It reproduces the real shape, including the awkward part.** The regional count
is large and the city count is zero, because that is what the live feed does:
VIIRS cannot see a structure fire and San Francisco is dense urban. A fake that
sprinkled fires across the city would train an officer -- and a reviewer -- to
expect something the live adapter never produces. ``in_city`` exists to force
the unusual case on purpose, so the console's "a wildfire signature inside the
city" path can be exercised without waiting for one.

The fire-weather block is synthetic too, and it keeps NASA POWER's defining
property: the window it reports ends days in the past, so the console's
"this is not current conditions" treatment is exercised in fake mode rather than
discovered in production.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Final

from firstdue.ports.city import CityAdapter
from firstdue.ports.clock import Clock
from firstdue.ports.fireactivity import (
    POWER_IS_NOT_NOW,
    PROVIDER_SYNTHETIC,
    VIIRS_RESOLUTION_NOTE,
    BoundingBox,
    Confidence,
    FireActivity,
    FireDetection,
    FireWeather,
    FireWeatherReading,
    summarize,
    unavailable,
)

#: Matches the live adapter's defaults, so a console laid out against fake data
#: is laid out against the same coordinate space live data arrives in.
DEFAULT_REGION: Final[BoundingBox] = BoundingBox(west=-124.5, south=36.5, east=-119.5, north=40.5)
DEFAULT_CITY_BOUNDS: Final[BoundingBox] = BoundingBox(
    west=-122.55, south=37.70, east=-122.35, north=37.84
)

#: Never a real platform name. This string is what a map popup shows.
SYNTHETIC_PLATFORM: Final[str] = "SYNTHETIC (no satellite)"

SYNTHETIC_ATTRIBUTION: Final[str] = (
    "TERSAGE synthetic fire activity - generated from the district id; no NASA "
    "endpoint was contacted"
)

SYNTHETIC_ADMISSION: Final[str] = (
    "These detections are synthetic, generated deterministically from the "
    "district id. Nothing was observed."
)

SYNTHETIC_WEATHER_CAVEAT: Final[str] = (
    f"{POWER_IS_NOT_NOW} These particular values are synthetic and were not " "measured by anyone."
)

#: The live product's observed lag, reproduced so the console's stale-window
#: treatment is exercised by the demo rather than only against production.
SIMULATED_LAG: Final[timedelta] = timedelta(days=4)

_CONFIDENCES: Final[tuple[Confidence, ...]] = ("low", "nominal", "high")

#: Parameter, label, unit, and the range the digest is mapped onto. The ranges
#: are plausible Northern California fire weather; the values inside them are
#: not measurements and say so.
_WEATHER: Final[tuple[tuple[str, str, str, float, float], ...]] = (
    ("T2M", "Temperature at 2 Meters", "C", 11.0, 34.0),
    ("RH2M", "Relative Humidity at 2 Meters", "%", 12.0, 88.0),
    ("WS10M", "Wind Speed at 10 Meters", "m/s", 0.4, 11.0),
)


class FakeFireActivityClient:
    """Scatters synthetic detections from a digest. No credentials, no network."""

    provider_label: Final[str] = PROVIDER_SYNTHETIC

    def __init__(
        self,
        *,
        city: CityAdapter,
        clock: Clock,
        region: BoundingBox = DEFAULT_REGION,
        city_bounds: BoundingBox = DEFAULT_CITY_BOUNDS,
        window_days: int = 5,
        available: bool = True,
        in_city: int = 0,
    ) -> None:
        self._city = city
        self._clock = clock
        self._region = region
        self._city_bounds = city_bounds
        self._window_days = window_days
        #: When false every district reports a simulated outage. The demo needs
        #: that: "the fire-activity feed is down" is a state the refusal panel
        #: must render, and a fake that could only succeed would leave that
        #: panel untested until the first real outage.
        self._available = available
        #: Forced in-city detections. Zero by default because zero is what the
        #: live feed returns; see the module docstring.
        self._in_city = max(0, in_city)

    async def fetch(self, *, district_id: str) -> FireActivity:
        if district_id not in set(self._city.list_districts()):
            # The fake refuses exactly where the live adapter refuses. A fake
            # that answered for districts the live path cannot resolve would
            # hide that failure until the first live deployment.
            return FireActivity.refused(district_id, unavailable("district_unknown"))
        if not self._available:
            return FireActivity.refused(district_id, unavailable("simulated_absence"))

        seed = f"{district_id}|{self._region.as_query()}|{self._window_days}"
        digest = hashlib.sha256(seed.encode("utf-8")).digest()

        regional = 6 + digest[0] % 30
        detections = [
            self._detection(seed, index, box=self._region, in_city=False)
            for index in range(regional)
        ] + [
            self._detection(seed, regional + index, box=self._city_bounds, in_city=True)
            for index in range(self._in_city)
        ]
        detections.sort(key=lambda detection: detection.acquired_at, reverse=True)

        in_city = sum(1 for detection in detections if detection.in_city)
        counted = summarize(
            regional=len(detections), in_city=in_city, window_days=self._window_days
        )
        return FireActivity(
            district_id=district_id,
            available=True,
            provider=PROVIDER_SYNTHETIC,
            region=self._region,
            city=self._city_bounds,
            window_days=self._window_days,
            detections=tuple(detections),
            regional_count=len(detections),
            in_city_count=in_city,
            truncated=False,
            summary=f"{counted} {SYNTHETIC_ADMISSION}",
            resolution_note=VIIRS_RESOLUTION_NOTE,
            attribution=SYNTHETIC_ATTRIBUTION,
            weather=self._weather(digest),
        )

    # ------------------------------------------------------------ internals

    def _detection(
        self, seed: str, index: int, *, box: BoundingBox, in_city: bool
    ) -> FireDetection:
        """One detection, fully determined by the seed and its position.

        Re-digested per detection rather than walking one 32-byte digest, so the
        count can grow past what a single hash provides without the scatter
        starting to repeat.
        """
        raw = hashlib.sha256(f"{seed}|{index}".encode()).digest()
        latitude = box.south + (raw[0] / 255.0) * (box.north - box.south) * 0.999
        longitude = box.west + (raw[1] / 255.0) * (box.east - box.west) * 0.999
        minutes_back = ((raw[2] << 8) | raw[3]) % max(1, self._window_days * 24 * 60)
        return FireDetection(
            latitude=round(latitude, 5),
            longitude=round(longitude, 5),
            confidence=_CONFIDENCES[raw[4] % len(_CONFIDENCES)],
            frp_mw=round(0.5 + (raw[5] / 255.0) * 40.0, 2),
            acquired_at=self._clock.now() - timedelta(minutes=minutes_back),
            satellite=SYNTHETIC_PLATFORM,
            in_city=in_city,
        )

    def _weather(self, digest: bytes) -> FireWeather:
        """Synthetic fire weather whose window ends days in the past, as POWER's does."""
        observed_end = self._clock.now() - SIMULATED_LAG
        observed_start = observed_end - timedelta(days=3)
        readings = tuple(
            FireWeatherReading(
                parameter=parameter,
                label=label,
                value=round(low + (digest[8 + index] / 255.0) * (high - low), 2),
                unit=unit,
                observed_at=observed_end,
            )
            for index, (parameter, label, unit, low, high) in enumerate(_WEATHER)
        )
        return FireWeather(
            available=True,
            provider=PROVIDER_SYNTHETIC,
            window_start=observed_start,
            window_end=observed_end,
            readings=readings,
            caveat=SYNTHETIC_WEATHER_CAVEAT,
            attribution=SYNTHETIC_ATTRIBUTION,
        )
