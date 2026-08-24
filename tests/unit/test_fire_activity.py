"""Regional fire activity: the empty city, the error page, and the key that never leaves.

Four properties matter more than the detections themselves.

**Zero inside the city is a true answer, not a failure.** The live feed returns
no VIIRS detections over San Francisco and hundreds over Northern California, so
the tests below pin the wording that makes a zero readable rather than checking
that a number is greater than another number.

**FIRMS answers an out-of-range window with prose and an HTTP 200.** A CSV
parser reads ``Invalid day range. Expects [1..5].`` as a header and yields no
rows -- which is how this feature silently reports "no fires" forever. That path
is asserted explicitly.

**POWER is reanalysis.** Every value has to carry the hour it describes and the
block has to carry its window, or the console will show a four-day-old wind
speed as current.

**The map key is on the URL.** ``httpx`` reproduces the full signed URL in the
message of the error a 403 raises, so no response may carry provider text -- and
that is asserted across success, connect-error and non-200 alike.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from firstdue.adapters.clock import FixedClock
from firstdue.adapters.fake.fireactivity import (
    SYNTHETIC_PLATFORM,
    FakeFireActivityClient,
)
from firstdue.adapters.nasa import build_fire_activity
from firstdue.adapters.nasa.firms import (
    DEFAULT_CITY_BOUNDS,
    DEFAULT_REGION,
    NasaFirmsClient,
    UnconfiguredFireActivityClient,
)
from firstdue.adapters.nasa.power import NasaPowerClient
from firstdue.city.san_francisco import SanFranciscoAdapter
from firstdue.errors import ConfigurationError, ValidationError
from firstdue.ports.fireactivity import (
    BoundingBox,
    FireActivity,
    FireActivityClient,
    FireWeather,
    FireWeatherClient,
    summarize,
    unavailable,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DISTRICT = "sffd-district-03"
OTHER_DISTRICT = "sffd-district-05"
UNKNOWN_DISTRICT = "sffd-district-nowhere"
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)

#: Distinctive enough that a substring search for it in a serialized response is
#: a real test rather than a coincidence. Never a real key -- the live one lives
#: in ``.env`` and is not permitted anywhere near a test.
MAP_KEY = "FIRMS-TEST-KEY-do-not-ship-91af"

#: The header exactly as the live endpoint returns it.
HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
    "instrument,confidence,version,bright_ti5,frp,daynight"
)

#: Two in the region, one inside the San Francisco box. The in-city row is
#: synthetic in the sense that the live feed does not produce one -- which is
#: exactly why the split has to be tested here rather than observed in
#: production.
REGIONAL_ROW = "37.03323,-120.13359,318.85,0.45,0.47,2026-08-20,1034,N,VIIRS,n,2.0NRT,289.51,1.54,N"
HIGH_ROW = "39.51200,-121.55000,340.10,0.39,0.36,2026-08-19,0221,N,VIIRS,h,2.0NRT,301.20,18.75,N"
IN_CITY_ROW = "37.77000,-122.42000,330.00,0.40,0.40,2026-08-18,934,N,VIIRS,l,2.0NRT,295.00,3.20,D"


@pytest.fixture
def city() -> SanFranciscoAdapter:
    return SanFranciscoAdapter(REPO_ROOT / "fixtures")


def _csv(*rows: str) -> str:
    return "\n".join((HEADER, *rows)) + "\n"


# --------------------------------------------------------------------- shape --


def test_a_bounding_box_runs_west_to_east_and_south_to_north() -> None:
    """A flipped box is a query over somewhere else that answers zero."""
    with pytest.raises(ValidationError):
        BoundingBox(west=-119.5, south=36.5, east=-124.5, north=40.5)
    with pytest.raises(ValidationError):
        BoundingBox(west=-124.5, south=40.5, east=-119.5, north=36.5)


def test_a_bounding_box_is_parsed_from_configuration_rather_than_trusted() -> None:
    box = BoundingBox.parse(" -124.5, 36.5, -119.5, 40.5 ")
    assert box == DEFAULT_REGION
    assert box.as_query() == "-124.5,36.5,-119.5,40.5"

    for raw in ("-124.5,36.5,-119.5", "-124.5,36.5,-119.5,north", ""):
        with pytest.raises(ValidationError):
            BoundingBox.parse(raw)


def test_the_city_box_lies_inside_the_region_it_is_counted_against() -> None:
    """Otherwise ``in_city_count`` counts over an area nobody queried."""
    assert DEFAULT_REGION.contains(*DEFAULT_CITY_BOUNDS.center())
    assert DEFAULT_CITY_BOUNDS.contains(37.77, -122.42)
    assert not DEFAULT_CITY_BOUNDS.contains(39.512, -121.55)


def test_an_unavailable_answer_cannot_be_silent() -> None:
    """The type refuses the failure mode this whole feature exists to prevent."""
    weather = FireWeather.refused(unavailable("unconfigured"))

    with pytest.raises(ValidationError):
        FireActivity(district_id=DISTRICT, available=False, weather=weather)

    with pytest.raises(ValidationError):
        # Available, but naming neither the box nor the window it counted over.
        FireActivity(district_id=DISTRICT, available=True, weather=weather)

    with pytest.raises(ValidationError):
        FireWeather(available=False)

    with pytest.raises(ValidationError):
        # Readings with no window is exactly how reanalysis becomes "now".
        FireWeather(available=True)


def test_the_city_cannot_hold_more_detections_than_the_region() -> None:
    with pytest.raises(ValidationError):
        FireActivity(
            district_id=DISTRICT,
            available=True,
            region=DEFAULT_REGION,
            window_days=5,
            regional_count=1,
            in_city_count=2,
            summary="x",
            resolution_note="y",
            weather=FireWeather.refused(unavailable("unconfigured")),
        )


def test_an_unknown_refusal_code_still_produces_a_sentence() -> None:
    """A mis-typed code degrades to a vaguer honest answer, never to a 500."""
    assert unavailable("no-such-code").reason
    assert unavailable("unconfigured").reason != unavailable("no-such-code").reason


def test_zero_in_the_city_is_phrased_as_the_ordinary_reading() -> None:
    """The whole product decision, in one sentence a console prints."""
    sentence = summarize(regional=266, in_city=0, window_days=5)
    assert "266" in sentence
    assert "none inside the city" in sentence
    assert "ordinary" in sentence


def test_an_empty_region_says_the_feed_answered() -> None:
    """A bare zero is ambiguous between a quiet night and a dead feed."""
    sentence = summarize(regional=0, in_city=0, window_days=5)
    assert "found nothing" in sentence
    assert "The feed answered" in sentence


def test_a_detection_inside_the_city_is_called_unusual() -> None:
    sentence = summarize(regional=12, in_city=1, window_days=1)
    assert "1 detection inside the city" in sentence
    assert "unusual" in sentence
    assert "the last 24 hours" in sentence


# ---------------------------------------------------------------------- fake --


async def test_the_fake_map_is_stable_per_district(city: SanFranciscoAdapter) -> None:
    """Same district, same map -- a replay of a demo is byte-identical."""
    client = FakeFireActivityClient(city=city, clock=FixedClock(NOW))

    first = await client.fetch(district_id=DISTRICT)
    second = await client.fetch(district_id=DISTRICT)
    elsewhere = await client.fetch(district_id=OTHER_DISTRICT)

    assert first == second
    assert first.detections != elsewhere.detections


async def test_the_fake_says_in_the_data_that_it_is_invented(
    city: SanFranciscoAdapter,
) -> None:
    """A synthetic wildfire that did not admit to being one is the worst failure here."""
    activity = await FakeFireActivityClient(city=city, clock=FixedClock(NOW)).fetch(
        district_id=DISTRICT
    )

    assert activity.available is True
    assert activity.provider == "synthetic"
    assert activity.detections
    assert all(d.satellite == SYNTHETIC_PLATFORM for d in activity.detections)
    assert "no NASA endpoint was contacted" in activity.attribution
    assert "synthetic" in activity.summary
    assert "Nothing was observed" in activity.summary


async def test_the_fake_reproduces_the_empty_city_the_live_feed_returns(
    city: SanFranciscoAdapter,
) -> None:
    """A fake that sprinkled fires across the city would train the wrong expectation."""
    activity = await FakeFireActivityClient(city=city, clock=FixedClock(NOW)).fetch(
        district_id=DISTRICT
    )

    assert activity.regional_count > 0
    assert activity.in_city_count == 0
    assert "none inside the city" in activity.summary
    assert "375 m" in activity.resolution_note


async def test_the_fake_can_force_the_unusual_in_city_case(city: SanFranciscoAdapter) -> None:
    """The console's "a wildfire signature inside the city" path needs exercising."""
    activity = await FakeFireActivityClient(city=city, clock=FixedClock(NOW), in_city=2).fetch(
        district_id=DISTRICT
    )

    assert activity.in_city_count == 2
    assert all(
        DEFAULT_CITY_BOUNDS.contains(d.latitude, d.longitude)
        for d in activity.detections
        if d.in_city
    )
    assert "unusual" in activity.summary


async def test_the_fake_weather_window_ends_in_the_past_as_reanalysis_does(
    city: SanFranciscoAdapter,
) -> None:
    """So the console's stale-window treatment is exercised by the demo."""
    activity = await FakeFireActivityClient(city=city, clock=FixedClock(NOW)).fetch(
        district_id=DISTRICT
    )

    weather = activity.weather
    assert weather.available is True
    assert weather.window_end is not None and weather.window_end < NOW - timedelta(days=3)
    assert {r.parameter for r in weather.readings} == {"T2M", "RH2M", "WS10M"}
    assert all(r.observed_at < NOW for r in weather.readings)
    assert "reanalysis, not observation" in weather.caveat
    assert "National Weather Service" in weather.caveat
    assert "synthetic" in weather.caveat


async def test_a_district_the_city_does_not_know_is_refused_not_invented(
    city: SanFranciscoAdapter,
) -> None:
    """The fake refuses exactly where the live adapter refuses."""
    activity = await FakeFireActivityClient(city=city, clock=FixedClock(NOW)).fetch(
        district_id=UNKNOWN_DISTRICT
    )

    assert activity.available is False
    assert activity.detections == ()
    assert "no district by this name" in activity.unavailable_reason


async def test_fake_mode_can_simulate_a_dead_provider(city: SanFranciscoAdapter) -> None:
    """The refusal panel is exercised by the demo, not only by a real outage."""
    activity = await FakeFireActivityClient(
        city=city, clock=FixedClock(NOW), available=False
    ).fetch(district_id=DISTRICT)

    assert activity.available is False
    assert "fake mode is simulating" in activity.unavailable_reason
    assert activity.weather.available is False


# --------------------------------------------------------------------- firms --


def _firms(
    city: SanFranciscoAdapter,
    handler: object,
    *,
    clock: FixedClock | None = None,
    weather: FireWeatherClient | None = None,
    **kwargs: object,
) -> NasaFirmsClient:
    return NasaFirmsClient(
        map_key=MAP_KEY,
        city=city,
        clock=clock or FixedClock(NOW),
        weather=weather,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _detections(body: str) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/csv"})

    return handler


async def test_the_verified_csv_header_parses_into_detections(
    city: SanFranciscoAdapter,
) -> None:
    activity = await _firms(city, _detections(_csv(REGIONAL_ROW, HIGH_ROW, IN_CITY_ROW))).fetch(
        district_id=DISTRICT
    )

    assert activity.available is True
    assert activity.provider == "nasa-firms"
    assert activity.region == DEFAULT_REGION
    assert activity.city == DEFAULT_CITY_BOUNDS
    assert activity.window_days == 5
    assert activity.regional_count == 3
    assert activity.in_city_count == 1
    assert activity.truncated is False

    # Newest first, so a truncated payload keeps what a commander would look at.
    assert [d.acquired_at.day for d in activity.detections] == [20, 19, 18]

    newest = activity.detections[0]
    assert newest.latitude == pytest.approx(37.03323)
    assert newest.longitude == pytest.approx(-120.13359)
    # The letter is spelled out; a code in a console is a code to look up.
    assert newest.confidence == "nominal"
    assert newest.frp_mw == pytest.approx(1.54)
    assert newest.acquired_at == datetime(2026, 8, 20, 10, 34, tzinfo=UTC)
    assert newest.satellite == "VIIRS (N)"
    assert newest.in_city is False

    assert {d.confidence for d in activity.detections} == {"nominal", "high", "low"}
    assert next(d for d in activity.detections if d.in_city).longitude == pytest.approx(-122.42)


async def test_a_three_digit_acquisition_time_is_an_hour_not_a_day(
    city: SanFranciscoAdapter,
) -> None:
    """``934`` is 09:34. Read as ``9340`` it would be dropped or misplaced."""
    activity = await _firms(city, _detections(_csv(IN_CITY_ROW))).fetch(district_id=DISTRICT)

    assert activity.detections[0].acquired_at == datetime(2026, 8, 18, 9, 34, tzinfo=UTC)


async def test_an_out_of_range_window_is_a_refusal_and_never_zero_fires(
    city: SanFranciscoAdapter,
) -> None:
    """The exact trap: FIRMS answers prose with a 200, and a CSV reader eats it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="Invalid day range. Expects [1..5].", headers={"content-type": "text/plain"}
        )

    activity = await _firms(city, handler).fetch(district_id=DISTRICT)

    assert activity.available is False
    assert activity.regional_count == 0
    assert activity.detections == ()
    assert "one to five days" in activity.unavailable_reason
    assert "not a count of zero fires" in activity.unavailable_reason


async def test_a_window_outside_the_endpoints_range_cannot_be_configured(
    city: SanFranciscoAdapter,
) -> None:
    """Refused at construction as well, because it is a misconfiguration."""
    for days in (0, 6, 30):
        with pytest.raises(ConfigurationError):
            _firms(city, _detections(_csv()), days=days)


async def test_a_header_only_answer_is_an_honest_zero(city: SanFranciscoAdapter) -> None:
    """A quiet region is available and empty -- not unavailable, and not alarming."""
    activity = await _firms(city, _detections(_csv())).fetch(district_id=DISTRICT)

    assert activity.available is True
    assert activity.regional_count == 0
    assert activity.in_city_count == 0
    assert "found nothing" in activity.summary
    assert activity.resolution_note


async def test_a_malformed_row_is_dropped_and_the_rest_survive(
    city: SanFranciscoAdapter,
) -> None:
    """One bad line must not delete the other detections."""
    rows = (
        REGIONAL_ROW,
        "not-a-latitude,-120.1,318,0.4,0.4,2026-08-20,1034,N,VIIRS,n,2.0NRT,289,1.5,N",
        "37.5,-121.0,318,0.4,0.4,not-a-date,1034,N,VIIRS,n,2.0NRT,289,1.5,N",
        "999.0,-121.0,318,0.4,0.4,2026-08-20,1034,N,VIIRS,n,2.0NRT,289,1.5,N",
        HIGH_ROW,
    )
    activity = await _firms(city, _detections(_csv(*rows))).fetch(district_id=DISTRICT)

    assert activity.available is True
    assert activity.regional_count == 2


async def test_a_row_with_an_unusable_radiative_power_still_counts_as_a_detection(
    city: SanFranciscoAdapter,
) -> None:
    """The detection happened; only its size is unreadable."""
    row = "38.0,-121.0,318,0.4,0.4,2026-08-20,1034,N,VIIRS,x,2.0NRT,289,,N"
    activity = await _firms(city, _detections(_csv(row))).fetch(district_id=DISTRICT)

    assert activity.regional_count == 1
    assert activity.detections[0].frp_mw == 0.0
    # An unrecognised confidence letter is never promoted to nominal.
    assert activity.detections[0].confidence == "unknown"


async def test_an_answer_that_is_not_a_detection_table_is_reported_as_unreadable(
    city: SanFranciscoAdapter,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Service unavailable</body></html>")

    activity = await _firms(city, handler).fetch(district_id=DISTRICT)

    assert activity.available is False
    assert "never parsed into zero fires" in activity.unavailable_reason


async def test_a_dead_provider_is_an_outage_not_an_absence_of_fire(
    city: SanFranciscoAdapter,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    activity = await _firms(city, handler).fetch(district_id=DISTRICT)

    assert activity.available is False
    assert "outage" in activity.unavailable_reason


async def test_a_slow_provider_degrades_rather_than_holding_the_request(
    city: SanFranciscoAdapter,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1.0)
        return httpx.Response(200, text=_csv(REGIONAL_ROW))

    client = _firms(city, handler, deadline_s=0.05)
    activity = await asyncio.wait_for(client.fetch(district_id=DISTRICT), timeout=5.0)

    assert activity.available is False
    assert "deadline" in activity.unavailable_reason


async def test_a_district_the_city_does_not_know_costs_the_provider_nothing(
    city: SanFranciscoAdapter,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the provider must not be asked about an unknown district")

    client = _firms(city, handler)
    activity = await client.fetch(district_id=UNKNOWN_DISTRICT)

    assert activity.available is False
    assert client.upstream_calls == 0


# ------------------------------------------------------------- the key rule --


async def test_the_map_key_never_appears_in_a_response(city: SanFranciscoAdapter) -> None:
    """The whole security boundary, asserted on every path a browser can see.

    FIRMS takes its key as a *path segment*, so the URL cannot be kept out of an
    ``httpx`` error message by moving it into query parameters. If any field ever
    grew a provider URL, or any handler ever rendered ``str(exc)``, this fails.
    """

    async def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def forbidden(request: httpx.Request) -> httpx.Response:
        # httpx puts the full request URL -- key and all -- into the message of
        # the HTTPStatusError this raises. Rendering str(exc) would publish it.
        return httpx.Response(403, text="Invalid MAP_KEY.")

    for handler in (_detections(_csv(REGIONAL_ROW)), unreachable, forbidden):
        client = _firms(city, handler)
        for district in (DISTRICT, UNKNOWN_DISTRICT):
            body = json.loads((await client.fetch(district_id=district)).model_dump_json())
            rendered = json.dumps(body)
            assert MAP_KEY not in rendered
            assert "firms.modaps.eosdis.nasa.gov" not in rendered
            assert "api/area/csv" not in rendered


async def test_a_rejected_key_reads_as_an_outage_not_as_a_key_problem(
    city: SanFranciscoAdapter,
) -> None:
    """An officer is told the feed is down. The key detail stays in the logs."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Invalid MAP_KEY.")

    activity = await _firms(city, handler).fetch(district_id=DISTRICT)

    assert activity.available is False
    assert "outage" in activity.unavailable_reason
    assert "MAP_KEY" not in activity.unavailable_reason


def test_a_live_client_cannot_be_built_without_a_key(city: SanFranciscoAdapter) -> None:
    with pytest.raises(ConfigurationError):
        NasaFirmsClient(map_key="", city=city, clock=FixedClock(NOW))


def test_a_city_box_outside_the_region_cannot_be_configured(
    city: SanFranciscoAdapter,
) -> None:
    """Otherwise the city count is taken over an area the query never covered."""
    with pytest.raises(ConfigurationError):
        NasaFirmsClient(
            map_key=MAP_KEY,
            city=city,
            clock=FixedClock(NOW),
            region=BoundingBox(west=-124.5, south=36.5, east=-123.0, north=38.0),
            city_bounds=DEFAULT_CITY_BOUNDS,
        )


async def test_no_key_configured_is_a_documented_state() -> None:
    activity = await UnconfiguredFireActivityClient().fetch(district_id=DISTRICT)

    assert activity.available is False
    assert "was not given" in activity.unavailable_reason
    assert "NASA FIRMS" in activity.unavailable_reason
    assert activity.weather.available is False


# ------------------------------------------------------- metering discipline --


async def test_one_regional_answer_serves_every_district(city: SanFranciscoAdapter) -> None:
    """FIRMS is metered per transaction and the region does not vary by district."""
    client = _firms(city, _detections(_csv(REGIONAL_ROW, IN_CITY_ROW)))

    first = await client.fetch(district_id=DISTRICT)
    second = await client.fetch(district_id=OTHER_DISTRICT)

    assert client.upstream_calls == 1
    assert client.cache_hits == 1
    assert first.detections == second.detections
    # The district each answer was asked about is still its own.
    assert first.district_id == DISTRICT
    assert second.district_id == OTHER_DISTRICT


async def test_the_cache_expires_rather_than_never_refreshing(
    city: SanFranciscoAdapter,
) -> None:
    """Satellite passes are a few times a day, not never."""
    clock = FixedClock(NOW)
    client = _firms(
        city, _detections(_csv(REGIONAL_ROW)), clock=clock, cache_ttl=timedelta(minutes=20)
    )

    await client.fetch(district_id=DISTRICT)
    clock.set(NOW + timedelta(minutes=25))
    await client.fetch(district_id=DISTRICT)

    assert client.cache_hits == 0
    assert client.upstream_calls == 2


async def test_a_spent_rate_budget_is_reported_rather_than_slept_through(
    city: SanFranciscoAdapter,
) -> None:
    """A commander's request is not held open waiting out a token bucket."""
    clock = FixedClock(NOW)
    client = _firms(
        city, _detections(_csv(REGIONAL_ROW)), clock=clock, rate_per_second=0.5, burst=1
    )

    first = await client.fetch(district_id=DISTRICT)
    # A different query, so the cache cannot answer it, at the same instant.
    client.clear_cache()
    second = await client.fetch(district_id=DISTRICT)

    assert first.available is True
    assert second.available is False
    assert "metered" in second.unavailable_reason


async def test_a_large_answer_is_capped_and_says_so(city: SanFranciscoAdapter) -> None:
    """The count stays true; only the payload is bounded."""
    rows = [
        f"3{7 + index % 3}.{index:04d},-121.{index:04d},318,0.4,0.4,2026-08-20,1034,"
        "N,VIIRS,n,2.0NRT,289,1.5,N"
        for index in range(40)
    ]
    client = _firms(city, _detections(_csv(*rows)), max_detections=10)
    activity = await client.fetch(district_id=DISTRICT)

    assert activity.regional_count == 40
    assert len(activity.detections) == 10
    assert activity.truncated is True


# --------------------------------------------------------------------- power --


def _power_payload(series: dict[str, dict[str, float]]) -> dict[str, object]:
    return {
        "properties": {"parameter": series},
        "parameters": {
            "T2M": {"units": "C", "longname": "Temperature at 2 Meters"},
            "RH2M": {"units": "%", "longname": "Relative Humidity at 2 Meters"},
            "WS10M": {"units": "m/s", "longname": "Wind Speed at 10 Meters"},
        },
        "header": {"fill_value": -999.0, "time_standard": "UTC"},
    }


#: The tail of the window is fill, exactly as the live product returns it: POWER
#: is reanalysis and the newest hours are not filled yet.
LAGGING_SERIES: dict[str, dict[str, float]] = {
    "T2M": {"2026081600": 14.0, "2026081623": 22.91, "2026081700": -999.0},
    "RH2M": {"2026081600": 70.0, "2026081623": 41.5, "2026081700": -999.0},
    "WS10M": {"2026081600": 1.2, "2026081623": 6.4, "2026081700": -999.0},
}


def _power(handler: object, **kwargs: object) -> NasaPowerClient:
    return NasaPowerClient(
        clock=FixedClock(NOW),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def _power_ok(payload: dict[str, object]) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return handler


async def test_fire_weather_reports_the_latest_hour_that_carried_a_value() -> None:
    """``-999`` is POWER's fill value, not a cold hour."""
    weather = await _power(_power_ok(_power_payload(LAGGING_SERIES))).fetch(
        latitude=37.77, longitude=-122.42
    )

    assert weather.available is True
    assert weather.provider == "nasa-power"
    readings = {r.parameter: r for r in weather.readings}
    assert readings["T2M"].value == pytest.approx(22.91)
    assert readings["T2M"].unit == "C"
    # The provider's own wording, not ours.
    assert readings["T2M"].label == "Temperature at 2 Meters"
    assert readings["WS10M"].value == pytest.approx(6.4)


async def test_every_value_carries_the_hour_it_describes() -> None:
    """The whole defence against reanalysis being read as current conditions."""
    weather = await _power(_power_ok(_power_payload(LAGGING_SERIES))).fetch(
        latitude=37.77, longitude=-122.42
    )

    assert weather.window_start == datetime(2026, 8, 16, 0, tzinfo=UTC)
    assert weather.window_end == datetime(2026, 8, 16, 23, tzinfo=UTC)
    assert all(r.observed_at == weather.window_end for r in weather.readings)
    # And the words, because a timestamp alone is not a caveat.
    assert "reanalysis, not observation" in weather.caveat
    assert "never conditions now" in weather.caveat
    assert "National Weather Service" in weather.caveat


async def test_the_request_pins_utc_rather_than_local_solar_time() -> None:
    """POWER defaults to LST, whose hour keys would misdate the window."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_power_payload(LAGGING_SERIES))

    await _power(handler).fetch(latitude=37.77, longitude=-122.42)

    params = seen[0].url.params
    assert params["time-standard"] == "UTC"
    assert params["parameters"] == "T2M,RH2M,WS10M"
    assert params["community"] == "RE"
    assert params["start"] == "20260813"
    assert params["end"] == "20260820"


async def test_a_window_of_only_fill_values_is_a_stated_refusal() -> None:
    """No usable hours, and a wind speed of -999, are very different claims."""
    empty = {name: {"2026081600": -999.0} for name in ("T2M", "RH2M", "WS10M")}
    weather = await _power(_power_ok(_power_payload(empty))).fetch(
        latitude=37.77, longitude=-122.42
    )

    assert weather.available is False
    assert weather.readings == ()
    assert "unfilled hour is not a measurement" in weather.unavailable_reason


async def test_an_unreadable_power_answer_is_reported_as_unreadable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    weather = await _power(handler).fetch(latitude=37.77, longitude=-122.42)

    assert weather.available is False
    assert "not an hourly series" in weather.unavailable_reason


async def test_power_never_raises_at_its_caller() -> None:
    """It is context beside the map; it must never be able to take the map down."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    weather = await _power(handler).fetch(latitude=37.77, longitude=-122.42)

    assert isinstance(weather, FireWeather)
    assert weather.available is False
    assert "outage" in weather.unavailable_reason


async def test_a_slow_power_degrades_the_weather_and_not_the_detections(
    city: SanFranciscoAdapter,
) -> None:
    """The panel a commander loses is the one that was slow."""

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1.0)
        return httpx.Response(200, json=_power_payload(LAGGING_SERIES))

    activity = await asyncio.wait_for(
        _firms(
            city,
            _detections(_csv(REGIONAL_ROW)),
            weather=_power(slow, deadline_s=0.05),
        ).fetch(district_id=DISTRICT),
        timeout=5.0,
    )

    assert activity.available is True
    assert activity.regional_count == 1
    assert activity.weather.available is False
    assert "deadline" in activity.weather.unavailable_reason


async def test_fire_weather_is_absent_rather_than_guessed_when_nothing_is_wired(
    city: SanFranciscoAdapter,
) -> None:
    activity = await _firms(city, _detections(_csv(REGIONAL_ROW))).fetch(district_id=DISTRICT)

    assert activity.available is True
    assert activity.weather.available is False
    assert "rather than guessed" in activity.weather.unavailable_reason


async def test_fire_weather_is_bought_once_for_a_point(city: SanFranciscoAdapter) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.host))
        return httpx.Response(200, json=_power_payload(LAGGING_SERIES))

    weather = _power(handler)
    await weather.fetch(latitude=37.77, longitude=-122.42)
    await weather.fetch(latitude=37.77, longitude=-122.42)

    assert len(calls) == 1
    assert weather.cache_hits == 1


# ------------------------------------------------------------------- wiring --


def test_every_client_satisfies_the_port(city: SanFranciscoAdapter) -> None:
    assert isinstance(FakeFireActivityClient(city=city, clock=FixedClock(NOW)), FireActivityClient)
    assert isinstance(UnconfiguredFireActivityClient(), FireActivityClient)
    assert isinstance(
        NasaFirmsClient(map_key=MAP_KEY, city=city, clock=FixedClock(NOW)), FireActivityClient
    )
    assert isinstance(NasaPowerClient(clock=FixedClock(NOW)), FireWeatherClient)


def test_live_mode_without_a_key_refuses_rather_than_drawing_synthetic_fires(
    city: SanFranciscoAdapter,
) -> None:
    """The branch that matters. A live console must never show invented wildfires."""
    clock = FixedClock(NOW)

    fake = build_fire_activity(use_fake=True, map_key=None, city=city, clock=clock)
    assert isinstance(fake, FakeFireActivityClient)

    unconfigured = build_fire_activity(use_fake=False, map_key=None, city=city, clock=clock)
    assert isinstance(unconfigured, UnconfiguredFireActivityClient)

    live = build_fire_activity(use_fake=False, map_key=MAP_KEY, city=city, clock=clock)
    assert isinstance(live, NasaFirmsClient)
