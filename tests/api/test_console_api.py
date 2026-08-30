"""The console API, against a district the slow loop has actually run over."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from fastapi.testclient import TestClient

from firstdue.demo.scenario import DISPUTED_ADDRESS_ID

PREFIX = "/api/v1"
DISTRICT = "sffd-district-03"


@pytest.fixture
def loaded(app_client: TestClient) -> TestClient:
    """Run one slow-loop pass so the console has real state to render.

    Driven through the API rather than by calling the agents directly, so the
    whole test exercises one process and one event loop -- the same way the
    console does it.
    """
    response = app_client.post(f"{PREFIX}/districts/{DISTRICT}/poll")
    assert response.status_code == 200
    assert response.json()["queue_size"] > 0
    return app_client


def test_district_stats_report_what_the_slow_loop_found(loaded: TestClient) -> None:
    body = loaded.get(f"{PREFIX}/districts/{DISTRICT}/stats").json()

    assert body["profiles"] > 0
    assert body["facts"] > 0
    assert body["open_conflicts"] == 2
    # Only the Hayes storey disagreement is high severity. The tower's
    # floor-count ambiguity is a records-keeping difference, not a finding about
    # the building, and it must not rank alongside one.
    assert body["high_severity_conflicts"] == 1
    assert body["queued_for_survey"] + body["dispatched"] > 0
    assert body["profiles_never_surveyed"] > 0


def test_district_stats_report_source_availability_honestly(loaded: TestClient) -> None:
    """The console renders where records came from; it never assumes."""
    body = loaded.get(f"{PREFIX}/districts/{DISTRICT}/stats").json()
    sources = {s["source_id"]: s for s in body["sources"]}

    assert sources["sf-permits"]["mode"] == "FIXTURE"
    assert sources["sf-permits"]["available"] is True
    assert sources["sf-permits"]["last_snapshot_id"]
    assert all("mode" in s for s in body["sources"])


def test_the_queue_row_carries_the_reasons_that_produced_it(loaded: TestClient) -> None:
    body = loaded.get(f"{PREFIX}/districts/{DISTRICT}/queue").json()

    assert body["count"] > 0
    top = body["entries"][0]
    assert top["address_id"] == DISPUTED_ADDRESS_ID
    assert top["rank"] == 1
    assert top["reasons"]
    assert any(r["rule_id"] == "rank.open-conflict-severity" for r in top["reasons"])
    assert all(0.0 <= r["weight"] <= 1.0 for r in top["reasons"])


def test_the_profile_shows_the_disagreement_rather_than_resolving_it(
    loaded: TestClient,
) -> None:
    body = loaded.get(f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}").json()

    stories = next(f for f in body["facts"] if f["canonical_key"] == "structure.stories")
    assert stories["status"] == "DISPUTED"
    # Both facts are still addressable from the row.
    assert len(stories["all_fact_ids"]) >= 2

    conflict = body["conflicts"][0]
    assert conflict["rule_id"] == "permit-vs-lidar-story-count"
    assert conflict["severity"] == 5
    assert len(conflict["fact_ids"]) == 2


def test_an_unknown_address_is_a_404_with_the_error_envelope(loaded: TestClient) -> None:
    response = loaded.get(f"{PREFIX}/buildings/sf-9999-nowhere")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_the_timeline_is_append_only_and_ordered(loaded: TestClient) -> None:
    body = loaded.get(f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}/timeline").json()

    assert body
    assert [e["sequence"] for e in body] == sorted(e["sequence"] for e in body)
    assert any(e["type"] == "CONFLICT_DETECTED" for e in body)
    # Every entry names who wrote it, at which version.
    assert all(e["actor"] for e in body)


def test_geometry_comes_with_an_svg_fallback_that_marks_disputed_mass(
    loaded: TestClient,
) -> None:
    body = loaded.get(f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}/geometry").json()

    assert body["spec"]["address_id"] == DISPUTED_ADDRESS_ID
    assert body["has_disputed_mass"] is True
    assert body["total_height_m"] > 0
    assert body["svg"].startswith("<svg")
    # The conflict is in the data, so even the static fallback shows it.
    assert "DISPUTED" in body["svg"]


def test_geometry_for_a_structure_with_none_is_a_404(loaded: TestClient) -> None:
    assert loaded.get(f"{PREFIX}/buildings/sf-3120-24th/geometry").status_code == 404


# ------------------------------------------------------------- human taps


def _top_entry(client: TestClient) -> dict[str, Any]:
    entry: dict[str, Any] = client.get(f"{PREFIX}/districts/{DISTRICT}/queue").json()["entries"][0]
    return entry


def test_dispatch_creates_the_four_autonomous_artifacts(loaded: TestClient) -> None:
    entry = _top_entry(loaded)
    response = loaded.post(
        f"{PREFIX}/queue/{entry['entry_id']}/dispatch",
        json={"company": "E-05", "crew_email": "e05@sffd.example"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["work_order_ref"]
    assert body["calendar_event_ref"]
    assert body["notification_ref"]
    assert body["plan_object_id"]


@pytest.mark.idempotency
def test_dispatching_twice_does_not_double_book_a_company(loaded: TestClient) -> None:
    entry = _top_entry(loaded)
    payload = {"company": "E-05", "crew_email": "e05@sffd.example"}
    first = loaded.post(f"{PREFIX}/queue/{entry['entry_id']}/dispatch", json=payload).json()
    second = loaded.post(f"{PREFIX}/queue/{entry['entry_id']}/dispatch", json=payload).json()

    assert second["work_order_ref"] == first["work_order_ref"]
    assert second["calendar_event_ref"] == first["calendar_event_ref"]
    assert second["replayed"] is True


def test_a_referral_is_staged_and_waits(loaded: TestClient) -> None:
    entry = _top_entry(loaded)
    dispatch = loaded.post(
        f"{PREFIX}/queue/{entry['entry_id']}/dispatch",
        json={"company": "E-05", "crew_email": "e05@sffd.example"},
    ).json()

    referral_id = dispatch["referral_id"]
    assert referral_id

    profile = loaded.get(f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}").json()
    # Not filed: no case number until a human approves.
    assert profile["open_referrals"] == []


@pytest.mark.authorization
def test_approval_files_it_and_returns_exactly_one_case_number(loaded: TestClient) -> None:
    entry = _top_entry(loaded)
    dispatch = loaded.post(
        f"{PREFIX}/queue/{entry['entry_id']}/dispatch",
        json={"company": "E-05", "crew_email": "e05@sffd.example"},
    ).json()
    referral_id = dispatch["referral_id"]

    approved = loaded.post(
        f"{PREFIX}/referrals/{referral_id}/approve", json={"approved_by": "capt-alvarez"}
    )
    assert approved.status_code == 200
    case_number = approved.json()["case_number"]
    assert case_number

    again = loaded.post(
        f"{PREFIX}/referrals/{referral_id}/approve", json={"approved_by": "capt-alvarez"}
    ).json()
    assert again["case_number"] == case_number
    assert again["replayed"] is True

    profile = loaded.get(f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}").json()
    assert [r["case_number"] for r in profile["open_referrals"]] == [case_number]


def test_approving_an_unknown_referral_is_a_404(loaded: TestClient) -> None:
    response = loaded.post(
        f"{PREFIX}/referrals/ref_nope/approve", json={"approved_by": "capt-alvarez"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------- surveys


def test_a_recorded_survey_closes_the_conflict_it_settled(loaded: TestClient) -> None:
    """Only a human observation closes a conflict."""
    before = loaded.get(f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}").json()
    assert before["conflicts"][0]["status"] == "OPEN"

    response = loaded.post(
        f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}/surveys",
        json={
            "company": "E-05",
            "surveyor": "capt-alvarez",
            "observations": [
                {
                    "canonical_key": "structure.stories",
                    "value": {"kind": "INTEGER", "integer": 3},
                }
            ],
        },
    )
    assert response.status_code == 201
    assert len(response.json()["conflicts_resolved"]) == 1

    after = loaded.get(f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}").json()
    assert after["conflicts"][0]["status"] == "RESOLVED"
    assert after["conflicts"][0]["resolved_by"] == "capt-alvarez"
    assert after["last_human_survey"] is not None

    # The survey wins the display, and both original facts are still stored.
    stories = next(f for f in after["facts"] if f["canonical_key"] == "structure.stories")
    assert stories["human_verified"] is True
    assert stories["source_type"] == "HUMAN_SURVEY"
    assert len(stories["all_fact_ids"]) >= 3


def test_surveys_are_listed_for_the_building(loaded: TestClient) -> None:
    loaded.post(
        f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}/surveys",
        json={"company": "E-05", "surveyor": "capt-alvarez", "outcome": "NO_ACCESS"},
    )
    listed = loaded.get(f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}/surveys").json()
    assert len(listed) == 1
    assert listed[0]["outcome"] == "NO_ACCESS"


def test_a_survey_that_could_not_get_in_resolves_nothing(loaded: TestClient) -> None:
    loaded.post(
        f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}/surveys",
        json={"company": "E-05", "surveyor": "capt-alvarez", "outcome": "NO_ACCESS"},
    )
    after = loaded.get(f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}").json()
    assert after["conflicts"][0]["status"] == "OPEN"


def test_imagery_comes_back_as_bytes_and_never_as_a_signed_url(
    loaded: TestClient,
) -> None:
    """The key stays server-side. A data URL is the whole point of the endpoint."""
    response = loaded.get(f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}/imagery")
    assert response.status_code == 200

    body = response.json()
    assert body["available"] is True
    assert body["data_url"].startswith("data:image/svg+xml;base64,")
    assert body["attribution"]

    rendered = response.text
    assert "maps.googleapis.com" not in rendered
    assert "key=" not in rendered
    assert "GOOGLE_MAPS_API_KEY" not in rendered


def test_fake_mode_imagery_admits_in_the_picture_that_it_is_synthetic(
    loaded: TestClient,
) -> None:
    body = loaded.get(f"{PREFIX}/buildings/{DISPUTED_ADDRESS_ID}/imagery").json()
    assert body["provider"] == "synthetic"

    svg = base64.b64decode(body["data_url"].split(",", 1)[1]).decode("utf-8")
    assert "SYNTHETIC" in svg


def test_imagery_for_an_unknown_address_refuses_rather_than_404s(loaded: TestClient) -> None:
    """A 404 would render as a broken console; the refusal renders as a refusal."""
    response = loaded.get(f"{PREFIX}/buildings/sf-nowhere-at-all/imagery")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["data_url"] == ""
    assert body["unavailable_reason"]


def test_fire_activity_is_regional_and_reports_the_city_separately(
    loaded: TestClient,
) -> None:
    """The product decision, on the wire.

    A city-only query returns nothing from VIIRS essentially always, so the
    region is the subject and the city's own count travels beside it with the
    sentence that makes a zero readable.
    """
    response = loaded.get(f"{PREFIX}/districts/{DISTRICT}/fire-activity")
    assert response.status_code == 200

    body = response.json()
    assert body["available"] is True
    assert body["regional_count"] > 0
    assert body["in_city_count"] == 0
    assert body["window_days"] == 5
    # The box is on the answer, because a count means nothing without its area.
    assert body["region"] == {"west": -124.5, "south": 36.5, "east": -119.5, "north": 40.5}
    assert body["city"]["west"] == -122.55
    assert "none inside the city" in body["summary"]
    assert "375 m" in body["resolution_note"]
    # The note is shorter now -- it was three sentences of justification
    # under a map -- but it still carries both halves of the reading a zero
    # invites: not a broken feed, and not an all-clear.
    assert "not a dead feed" in body["resolution_note"]
    assert "not an all-clear" in body["resolution_note"]


def test_fake_mode_fire_activity_admits_in_the_data_that_it_is_synthetic(
    loaded: TestClient,
) -> None:
    """An invented wildfire that did not say so is the worst failure here."""
    body = loaded.get(f"{PREFIX}/districts/{DISTRICT}/fire-activity").json()

    assert body["provider"] == "synthetic"
    assert all(d["satellite"].startswith("SYNTHETIC") for d in body["detections"])
    assert "no NASA endpoint was contacted" in body["attribution"]
    assert "Nothing was observed" in body["summary"]


def test_fire_weather_never_presents_itself_as_current_conditions(
    loaded: TestClient,
) -> None:
    """NASA POWER is reanalysis. NWS remains the live source for wind."""
    weather = loaded.get(f"{PREFIX}/districts/{DISTRICT}/fire-activity").json()["weather"]

    assert weather["available"] is True
    assert weather["window_start"] and weather["window_end"]
    assert {r["parameter"] for r in weather["readings"]} == {"T2M", "RH2M", "WS10M"}
    # Every value names the hour it describes, not the hour it was fetched.
    assert all(r["observed_at"] for r in weather["readings"])
    assert "reanalysis, not observation" in weather["caveat"]
    assert "National Weather Service" in weather["caveat"]


def test_fire_activity_for_an_unknown_district_refuses_rather_than_404s(
    loaded: TestClient,
) -> None:
    """A 404 would render as a broken console; the refusal renders as a refusal."""
    response = loaded.get(f"{PREFIX}/districts/sffd-district-nowhere/fire-activity")

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert body["detections"] == []
    assert body["unavailable_reason"]
    assert body["weather"]["available"] is False


def test_fire_activity_never_carries_a_provider_url(loaded: TestClient) -> None:
    """FIRMS puts its map key in the request path, so a URL in the body is the key."""
    rendered = loaded.get(f"{PREFIX}/districts/{DISTRICT}/fire-activity").text

    assert "firms.modaps.eosdis.nasa.gov" not in rendered
    assert "api/area/csv" not in rendered
    assert "FIRMS_MAP_KEY" not in rendered


# ------------------------------------------------------------------ terrain --

#: A square inside the fake region, and one nowhere near it. The tile proxy
#: serves one region and the route refuses everything else before the provider
#: is contacted, so both cases are about what is *not* spent.
TILE_INSIDE = "10/160/390"

#: Past the port's ceiling. Deeper squares are a street map, which is a different
#: product and somebody else's quota, so every implementation refuses them --
#: including the synthetic one this suite runs against, which is deliberately not
#: region-bounded because it fronts nobody's meter.
TILE_TOO_DEEP = "18/160000/390000"


@pytest.mark.parametrize("layer", ["elevation", "imagery"])
def test_a_terrain_tile_is_cached_hard_enough_to_survive_a_reload(
    app_client: TestClient, layer: str
) -> None:
    """A screenful of mesh is two requests per square and a camera move re-asks
    for most of them. Without these headers the map re-downloads a hillside that
    has not moved since the last ice age, on every render and every reload."""
    response = app_client.get(f"{PREFIX}/terrain/{layer}/{TILE_INSIDE}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")

    cache_control = response.headers["cache-control"]
    assert "private" in cache_control
    # `immutable` is the load-bearing one: it is what stops a reload
    # revalidating every square on screen just to be told nothing changed.
    assert "immutable" in cache_control
    # The lifetime is the tile's own, not a number invented at the route.
    max_age = int(cache_control.split("max-age=")[1].split(",")[0])
    assert max_age >= 7 * 24 * 3600
    assert response.headers["etag"]


def test_a_tile_the_caller_already_holds_comes_back_without_its_bytes(
    app_client: TestClient,
) -> None:
    """A validator that returned the tile anyway would be a round trip that saved
    the caller nothing at all."""
    first = app_client.get(f"{PREFIX}/terrain/elevation/{TILE_INSIDE}")
    etag = first.headers["etag"]

    second = app_client.get(
        f"{PREFIX}/terrain/elevation/{TILE_INSIDE}", headers={"If-None-Match": etag}
    )

    assert second.status_code == 304
    assert second.content == b""
    # The 304 re-states the validators, so the cache can extend the entry it
    # already has rather than dropping it on the next read.
    assert second.headers["etag"] == etag
    assert "immutable" in second.headers["cache-control"]


def test_a_weakened_validator_still_earns_its_304(app_client: TestClient) -> None:
    """``If-None-Match`` uses the weak comparison function, and a proxy is
    entitled to have marked our tag weak on the way past."""
    etag = app_client.get(f"{PREFIX}/terrain/elevation/{TILE_INSIDE}").headers["etag"]

    weakened = app_client.get(
        f"{PREFIX}/terrain/elevation/{TILE_INSIDE}",
        headers={"If-None-Match": f'W/{etag}, "some-other-tile"'},
    )

    assert weakened.status_code == 304


def test_a_stale_validator_gets_the_tile_rather_than_a_304(app_client: TestClient) -> None:
    """The ETag is over the bytes, so a re-flown square is a different tile."""
    response = app_client.get(
        f"{PREFIX}/terrain/elevation/{TILE_INSIDE}", headers={"If-None-Match": '"not-this-tile"'}
    )

    assert response.status_code == 200
    assert response.content


def test_the_etag_distinguishes_one_square_from_another(app_client: TestClient) -> None:
    """A tag keyed on the address rather than the bytes would serve last year's
    imagery for a square that had been re-flown."""
    here = app_client.get(f"{PREFIX}/terrain/elevation/{TILE_INSIDE}").headers["etag"]
    next_door = app_client.get(f"{PREFIX}/terrain/elevation/10/161/390").headers["etag"]

    assert here != next_door


def test_a_refused_square_is_a_404_and_is_not_cached_as_one(app_client: TestClient) -> None:
    """deck.gl reads a non-200 as "no tile here" and draws a hole, which is the
    correct rendering of a missing square. Caching that refusal would outlive the
    reconfiguration that made the square servable again."""
    response = app_client.get(f"{PREFIX}/terrain/elevation/{TILE_TOO_DEEP}")

    assert response.status_code == 404
    assert "immutable" not in response.headers.get("cache-control", "")
    assert response.json()["error"]["details"]["reason"]


# ------------------------------------------------ the slow loop's own account


def test_the_diagnostics_name_the_pass_and_who_recorded_it(loaded: TestClient) -> None:
    """The question the fleet panel cannot answer about itself.

    An agent that did nothing, an agent cancelled before it could say what it
    did, and an agent whose work the console filtered out all render as
    "0 recorded / idle". This is what separates them, so the first thing anyone
    reaches for during a demo is a request rather than a hypothesis.
    """
    body = loaded.get(f"{PREFIX}/districts/{DISTRICT}/slow-loop/diagnostics").json()

    assert body["district_id"] == DISTRICT
    assert body["events_read"] > 0
    assert set(body["slow_loop_agents"]) == {
        "records-watcher",
        "geometry-watcher",
        "hazard-watcher",
        "structure-watch",
        "referral-clerk",
    }

    # A pass ran, and every agent in it is named with what it wrote.
    assert body["last_pass_correlation_id"]
    recorded = {line["agent_id"]: line for line in body["recorded"]}
    assert set(body["slow_loop_agents"]) <= set(recorded)
    assert all(line["events"] > 0 for line in recorded.values())
    assert all(sum(line["kinds"].values()) == line["events"] for line in recorded.values())

    # The two instants an operator compares the console's session floor
    # against, in the format the console compares strings in.
    assert body["last_pass_started_at"] <= body["last_pass_ended_at"]
    assert body["last_pass_ended_at"] <= body["server_now"]
    assert body["newest_event"]["occurred_at"] <= body["server_now"]


def test_the_diagnostics_say_so_when_the_loop_has_never_run(app_client: TestClient) -> None:
    """An empty answer, not a guess.

    A log with no pass in it is exactly the state a console that shows an idle
    fleet is *supposed* to be in, and reporting a correlation id here would be
    inventing the pass the caller is asking whether happened.
    """
    body = app_client.get(f"{PREFIX}/districts/{DISTRICT}/slow-loop/diagnostics").json()

    assert body["last_pass_correlation_id"] is None
    assert body["recorded"] == []
    assert body["server_now"]
