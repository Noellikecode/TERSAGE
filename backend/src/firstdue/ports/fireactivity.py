"""Regional fire activity and fire-weather context -- or a stated refusal.

**This port is regional on purpose, and that decision is the whole design.**

The obvious product is a heat map of satellite fire detections over the city.
Measured against the live feed, that product is empty: over NASA FIRMS' maximum
five-day window, San Francisco proper returns *zero* VIIRS detections, while
Northern California returns hundreds. A VIIRS pixel is about 375 m across and
the instrument is built to see wildfire; a room-and-contents fire in a dense
urban block is far too small and far too brief to raise a thermal anomaly above
that threshold. A city-only map would therefore be blank essentially always, and
a blank map is the worst possible artefact: it reads as an outage on a bad day
and as reassurance on a good one, and it is neither.

So the question this port answers is the one a city fire department actually
has: **what is burning around us.** Regional activity is what drives mutual-aid
demand, what pulls strike teams and crews out of the city, what puts smoke over
the district and moves the air-quality and red-flag posture. That is
operational, it changes day to day, and it is exactly what VIIRS is good at.
The city's own count travels beside it, reported separately and honestly --
:data:`VIIRS_RESOLUTION_NOTE` ships with every answer so that "zero inside the
city" renders as the ordinary, unalarming fact it is, and never as a dead feed
or as a claim that nothing in San Francisco is on fire.

**Fire weather is context, not conditions.** NASA POWER is *reanalysis*: it
lags real time by days, so it can say how hot, dry and windy it has *been* and
can never say what it is now. Every reading therefore carries the hour it was
observed, the block carries the window those hours span, and
:data:`POWER_IS_NOT_NOW` states the limit in words the console renders. Current
wind already reaches the fleet from the National Weather Service feed in
:mod:`firstdue.sources.catalog`; this does not duplicate it and must never be
allowed to look like it does.

**A refusal is a value, never an exception** -- the same discipline
:mod:`firstdue.ports.imagery` applies to pixels. Unconfigured, outage, blown
deadline and spent rate budget are all a :class:`FireActivity` with
``available=False`` and a sentence saying why, because a console that renders
nothing where fire activity belongs teaches an officer that there is none.

**The provider's key never crosses this boundary.** FIRMS puts its map key in
the request *path*, so no field here can carry a provider URL, and no refusal
may carry a provider's error text -- ``httpx`` reproduces the full signed URL
in the message of the exception a 403 raises.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from firstdue.errors import ValidationError

#: Provider labels the console may receive. ``""`` accompanies a refusal --
#: naming a provider that produced nothing would suggest one was reached.
PROVIDER_FIRMS: Final[str] = "nasa-firms"
PROVIDER_POWER: Final[str] = "nasa-power"
PROVIDER_SYNTHETIC: Final[str] = "synthetic"

#: NASA asks that FIRMS and POWER products be credited where they are shown.
FIRMS_ATTRIBUTION: Final[str] = (
    "Active fire detections: NASA FIRMS, VIIRS near-real-time (NASA/LANCE/EOSDIS)"
)
POWER_ATTRIBUTION: Final[str] = "Fire weather: NASA POWER hourly reanalysis (NASA Langley)"

#: What a VIIRS confidence flag means, spelled out. The feed ships ``l``/``n``/
#: ``h``; a single letter in a console is a code an officer has to look up.
Confidence = Literal["low", "nominal", "high", "unknown"]

#: Ships with every available answer. This is the sentence that stops a zero
#: from being read as a failure -- or, worse, as an all-clear.
VIIRS_RESOLUTION_NOTE: Final[str] = (
    "VIIRS pixels are about 375 m across and the instrument detects "
    "wildfire-scale heat. A structure fire is too small and too brief to "
    "register, so zero detections inside the city is the ordinary reading -- "
    "it is not a dead feed, and it is not evidence that nothing is burning "
    "here. Regional counts are what this feed is good for."
)

#: Ships with every available fire-weather block, for the same reason.
POWER_IS_NOT_NOW: Final[str] = (
    "NASA POWER is reanalysis, not observation: it runs days behind real time. "
    "These values describe how hot, dry and windy it has been over the window "
    "named beside them, never conditions now. Current wind comes from the "
    "National Weather Service feed in the source catalog."
)

#: Why there is no fire activity to show. Rendered verbatim, so each one is a
#: sentence an officer can act on rather than a code they have to look up.
#: Mirrors ``sources.catalog.UNCONFIGURED_REASONS`` in tone and shape.
UNAVAILABLE_REASONS: Final[dict[str, str]] = {
    "unconfigured": (
        "NASA FIRMS needs a map key this process was not given; no fire "
        "detection provider was contacted"
    ),
    "district_unknown": (
        "the city adapter holds no district by this name, and fire activity is "
        "reported per district so a console cannot ask about one that does not exist"
    ),
    "provider_unreachable": (
        "NASA FIRMS could not be reached; that is an outage, not an absence of fire"
    ),
    "deadline": (
        "NASA FIRMS did not answer inside this request's deadline; the map is "
        "dropped rather than the console held open"
    ),
    "rate_limited": (
        "this process has spent its FIRMS rate budget; the feed is metered per "
        "transaction, and a later request will carry the detections"
    ),
    "invalid_window": (
        "NASA FIRMS rejected the requested day range -- it accepts one to five "
        "days and answers anything else with an error page, which is not a "
        "count of zero fires"
    ),
    "malformed_response": (
        "NASA FIRMS answered with something that was not a detection table; an "
        "unreadable answer is reported as unreadable, never parsed into zero fires"
    ),
    "oversized_response": (
        "NASA FIRMS returned more data than this request will hold in memory; "
        "the bounding box is narrowed rather than the answer truncated silently"
    ),
    "weather_unconfigured": (
        "no fire-weather provider is wired into this process, so recent "
        "temperature, humidity and wind are simply absent rather than guessed"
    ),
    "weather_malformed": (
        "NASA POWER answered with something that was not an hourly series; an "
        "unreadable answer is reported as unreadable, never parsed into a reading"
    ),
    "no_usable_hours": (
        "NASA POWER returned only fill values for this point and window; "
        "reanalysis lags real time, and an unfilled hour is not a measurement"
    ),
    "simulated_absence": (
        "fake mode is simulating an unreachable fire-activity provider, so the "
        "console's refusal panel is exercised by the demo rather than only in "
        "production"
    ),
}


class FireActivityUnavailable(BaseModel):
    """Why no fire activity is being shown.

    A type rather than a bare string so that "there is no fire activity" has to
    be *constructed* -- an adapter cannot arrive at it by forgetting to set a
    field, and every path that reaches it names one of
    :data:`UNAVAILABLE_REASONS`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The stable code, for a console that wants to style an outage differently
    #: from a policy. Kept out of the wire shape; ``reason`` is what renders.
    code: str = Field(min_length=1, max_length=60)
    reason: str = Field(min_length=1, max_length=400)


def unavailable(code: str) -> FireActivityUnavailable:
    """The refusal for a known code.

    An unknown code still produces a sentence rather than a ``KeyError``: a
    mis-typed code should degrade to a vaguer honest answer, never to a 500 on
    the one path whose whole job is reporting that something went wrong.
    """
    return FireActivityUnavailable(
        code=code,
        reason=UNAVAILABLE_REASONS.get(
            code, "no fire activity could be retrieved for this request"
        ),
    )


class BoundingBox(BaseModel):
    """A west/south/east/north box, in degrees.

    Carried on the answer rather than assumed by the console, because the box
    *is* the claim: "266 detections" means nothing without the area it counts
    over, and an operator who widens the region through configuration must not
    silently change what a displayed number refers to.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    west: float = Field(ge=-180.0, le=180.0)
    south: float = Field(ge=-90.0, le=90.0)
    east: float = Field(ge=-180.0, le=180.0)
    north: float = Field(ge=-90.0, le=90.0)

    @model_validator(mode="after")
    def _corners_are_ordered(self) -> BoundingBox:
        if self.west >= self.east or self.south >= self.north:
            raise ValidationError(
                "a bounding box runs west to east and south to north",
                details={"box": self.as_query()},
            )
        return self

    @classmethod
    def parse(cls, raw: str) -> BoundingBox:
        """Read ``west,south,east,north`` -- the order FIRMS takes on its URL.

        Startup configuration calls this, so a box typed wrong stops the process
        with a message rather than becoming a query over the Pacific that
        answers zero and looks like a quiet night.
        """
        parts = [part.strip() for part in raw.split(",")]
        if len(parts) != 4:
            raise ValidationError(
                "a bounding box is four comma-separated degrees: west,south,east,north",
                details={"raw": raw[:120]},
            )
        try:
            west, south, east, north = (float(part) for part in parts)
        except ValueError as exc:
            raise ValidationError(
                "a bounding box corner is not a number", details={"raw": raw[:120]}
            ) from exc
        return cls(west=west, south=south, east=east, north=north)

    def as_query(self) -> str:
        """The FIRMS path segment: ``west,south,east,north``."""
        return f"{self.west},{self.south},{self.east},{self.north}"

    def contains(self, latitude: float, longitude: float) -> bool:
        """Half-open on purpose: a point on two boxes' shared edge counts once."""
        return self.west <= longitude < self.east and self.south <= latitude < self.north

    def center(self) -> tuple[float, float]:
        """``(latitude, longitude)`` -- the point fire weather is asked about."""
        return ((self.south + self.north) / 2.0, (self.west + self.east) / 2.0)


class FireDetection(BaseModel):
    """One satellite thermal anomaly.

    Not "a fire". VIIRS reports a pixel that was hotter than its neighbours at
    the moment of a satellite pass, which is usually a wildfire and is sometimes
    a flare, a kiln, or a hot roof. The console renders these as detections
    because calling them fires would assert something the instrument did not
    observe.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    #: The VIIRS flag, spelled out. ``unknown`` when the feed sent something
    #: this build does not recognise -- never silently promoted to ``nominal``.
    confidence: Confidence
    #: Fire radiative power, megawatts. Roughly how much energy the pixel was
    #: radiating; the closest thing in the feed to "how big".
    frp_mw: float = Field(ge=0.0)
    #: When the satellite passed, UTC. The feed reports the pass, not the
    #: ignition, and the two can be hours apart.
    acquired_at: datetime
    #: Instrument and platform as the feed named them, e.g. ``VIIRS (N)``.
    satellite: str = Field(min_length=1, max_length=60)
    #: Channel-I4 brightness temperature, kelvin, as the feed reported it.
    #:
    #: **This is a temperature, not an anomaly.** VIIRS reports how hot the
    #: pixel radiated; it does not ship a background to subtract, and this
    #: product carries nothing that would let one be derived. A console that
    #: printed "+8 °C above normal" would be inventing the normal. ``None``
    #: where the feed omitted it -- never zero, which is 273 degrees of claim.
    brightness_k: float | None = Field(default=None, ge=0.0)
    #: Whether the pass was in daylight. Bears on how to read the reading: a
    #: night detection is a thermal source rather than a sunlit surface.
    daynight: Literal["day", "night", "unknown"] = "unknown"
    #: Whether this detection fell inside the city box. Computed once here so a
    #: console and a count cannot disagree about the same pixel.
    in_city: bool = False


class FireWeatherReading(BaseModel):
    """One fire-weather parameter at one observed hour."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The POWER parameter name, e.g. ``T2M``.
    parameter: str = Field(min_length=1, max_length=20)
    #: What it is in words, taken from the provider's own metadata.
    label: str = Field(min_length=1, max_length=120)
    value: float
    unit: str = Field(default="", max_length=20)
    #: The hour this value describes -- **not** the hour it was fetched. This
    #: field is the whole defence against reanalysis being read as now.
    observed_at: datetime

    @model_validator(mode="after")
    def _a_value_is_a_number(self) -> FireWeatherReading:
        if not math.isfinite(self.value):
            raise ValidationError(
                "a fire-weather reading has to be a finite number",
                details={"parameter": self.parameter},
            )
        return self


class FireWeather(BaseModel):
    """Recent temperature, humidity and wind -- with the window they cover.

    Never "current conditions". :data:`POWER_IS_NOT_NOW` travels with every
    available block and the console renders it; the window bounds say exactly
    how far behind the values are.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    available: bool
    provider: str = Field(default="", max_length=40)
    #: The span of hours that actually carried usable values, UTC. ``None`` on
    #: a refusal -- an empty window is not a window of length zero.
    window_start: datetime | None = None
    window_end: datetime | None = None
    readings: tuple[FireWeatherReading, ...] = ()
    #: :data:`POWER_IS_NOT_NOW`, or a fake adapter's equivalent. Non-empty
    #: whenever there is anything to caveat.
    caveat: str = Field(default="", max_length=600)
    attribution: str = Field(default="", max_length=200)
    unavailable_reason: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _absence_says_why(self) -> FireWeather:
        if not self.available:
            if not self.unavailable_reason:
                raise ValidationError("fire weather that is unavailable has to say why")
            if self.readings:
                raise ValidationError("fire weather that is unavailable cannot carry readings")
            return self
        if not self.readings:
            raise ValidationError("fire weather reported available has to carry a reading")
        if self.window_start is None or self.window_end is None:
            raise ValidationError("fire weather has to name the window its values came from")
        if self.window_end < self.window_start:
            raise ValidationError("a fire-weather window cannot end before it starts")
        if not self.caveat:
            raise ValidationError("fire weather has to carry the reanalysis caveat")
        return self

    @classmethod
    def refused(cls, refusal: FireActivityUnavailable) -> FireWeather:
        """The honest empty answer, with the reason attached."""
        return cls(available=False, unavailable_reason=refusal.reason)


class FireActivity(BaseModel):
    """Regional fire activity for one district, or a stated refusal.

    ``regional_count`` and ``in_city_count`` are reported separately and are
    both meaningful. The first is the mutual-aid and air-quality picture; the
    second is very nearly always zero, and :data:`VIIRS_RESOLUTION_NOTE` is
    attached so that a console can say why without inventing the explanation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    district_id: str = Field(min_length=1, max_length=120)
    available: bool
    #: ``nasa-firms``, ``synthetic``, or ``""`` on a refusal.
    provider: str = Field(default="", max_length=40)
    #: The box actually queried, and the box the city count was taken over.
    region: BoundingBox | None = None
    city: BoundingBox | None = None
    #: FIRMS accepts one to five days and nothing else.
    window_days: int = Field(default=0, ge=0, le=5)
    #: The detections the console draws. Capped; ``regional_count`` is the full
    #: figure and ``truncated`` says when the two differ.
    detections: tuple[FireDetection, ...] = ()
    regional_count: int = Field(default=0, ge=0)
    in_city_count: int = Field(default=0, ge=0)
    truncated: bool = False
    #: One sentence a console can print above the map.
    summary: str = Field(default="", max_length=400)
    #: :data:`VIIRS_RESOLUTION_NOTE`. Non-empty on every available answer.
    resolution_note: str = Field(default="", max_length=600)
    attribution: str = Field(default="", max_length=200)
    unavailable_reason: str = Field(default="", max_length=400)
    #: Always present. Fire weather can refuse on its own while detections
    #: succeed, and a commander should still get the half that worked.
    weather: FireWeather

    @model_validator(mode="after")
    def _absence_says_why(self) -> FireActivity:
        if not self.available:
            if not self.unavailable_reason:
                raise ValidationError("fire activity that is unavailable has to say why")
            if self.detections or self.regional_count or self.in_city_count:
                raise ValidationError("fire activity that is unavailable cannot carry detections")
            return self
        if self.region is None or self.window_days < 1:
            raise ValidationError("fire activity has to name the box and window it counted over")
        if not self.summary or not self.resolution_note:
            raise ValidationError(
                "fire activity has to say what it found and what the instrument can see"
            )
        if self.in_city_count > self.regional_count:
            raise ValidationError("the city cannot hold more detections than the region")
        if len(self.detections) > self.regional_count:
            raise ValidationError("more detections were drawn than were counted")
        return self

    @classmethod
    def refused(
        cls,
        district_id: str,
        refusal: FireActivityUnavailable,
        *,
        weather: FireWeather | None = None,
    ) -> FireActivity:
        """The honest empty answer, with the reason attached."""
        return cls(
            district_id=district_id,
            available=False,
            unavailable_reason=refusal.reason,
            weather=weather or FireWeather.refused(refusal),
        )


def summarize(*, regional: int, in_city: int, window_days: int) -> str:
    """The sentence above the map, phrased once so every adapter says it alike.

    The zero cases are written out deliberately. "0 detections" beside a blank
    map is ambiguous between a quiet region, a broken feed, and an instrument
    that was never going to see this kind of fire; the wording below leaves only
    the true reading.
    """
    span = "the last 24 hours" if window_days == 1 else f"the last {window_days} days"
    if regional == 0:
        return (
            f"No wildfire-scale satellite detections anywhere in the region over {span}. "
            "The feed answered; it found nothing."
        )
    counted = "detection" if regional == 1 else "detections"
    if in_city == 0:
        return (
            f"{regional} satellite fire {counted} across the region over {span}, "
            "and none inside the city -- which is the ordinary reading for an "
            "instrument built to see wildfire."
        )
    inside = "detection" if in_city == 1 else "detections"
    return (
        f"{regional} satellite fire {counted} across the region over {span}, "
        f"including {in_city} {inside} inside the city -- a wildfire-scale "
        "signature within the city boundary is unusual and worth looking at."
    )


@runtime_checkable
class FireWeatherClient(Protocol):
    """Recent temperature, humidity and wind for one point."""

    @property
    def provider_label(self) -> str: ...

    async def fetch(self, *, latitude: float, longitude: float) -> FireWeather:
        """Return recent fire weather, or say why it is absent. Never raises."""
        ...


@runtime_checkable
class FireActivityClient(Protocol):
    """Regional fire activity for one district, or a stated refusal."""

    @property
    def provider_label(self) -> str:
        """What this implementation is, reported so the console can say so.

        ``synthetic`` here is not decoration: fake mode is the default and the
        whole test suite, and simulated detections that did not admit to being
        simulated would put invented fires on a commander's map.
        """
        ...

    async def fetch(self, *, district_id: str) -> FireActivity:
        """Return fire activity around one district.

        Never raises for an unconfigured key, an unknown district, a dead
        provider, or a blown deadline: each of those is a :class:`FireActivity`
        with ``available=False``. A raise here would turn a missing map into a
        broken console.
        """
        ...
