"""The incident API: opening, streaming, reconnecting, and closing.

The SSE tests are the ones with teeth. Ordering, resume, and the guarantee that
a frame is in the log before it is sent are the contract a tablet on a
fireground depends on.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

PREFIX = "/api/v1"
DISTRICT = "sffd-district-03"
ADDRESS = "sf-0450-hayes"

#: Words a brief must never contain. The system delivers information and
#: performs clerical execution; tactics belong to the incident commander.
FORBIDDEN_TACTICAL = (
    "offensive",
    "defensive",
    "should attack",
    "recommend",
    "recommended",
    "evacuate",
    "evacuation order",
    "assign engine",
    "vent the roof",
    "go interior",
    "pull out",
    "surround and drown",
    "will collapse",
    "is going to collapse",
)


@pytest.fixture
def incident(app_client: TestClient) -> dict[str, Any]:
    """A warm district and one open incident."""
    app_client.post(f"{PREFIX}/districts/{DISTRICT}/poll")
    response = app_client.post(
        f"{PREFIX}/incidents",
        json={"address": ADDRESS, "cad_ref": "CAD-0001", "alarm_level": 2},
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    return body


def _frames(raw: str) -> list[dict[str, Any]]:
    """Parse an SSE body into ordered frames."""
    frames: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            if current.get("data"):
                frames.append({"id": current.get("id"), "data": json.loads(current["data"])})
            current = {}
            continue
        key, _, value = line.partition(":")
        current[key.strip()] = value.strip()
    if current.get("data"):
        frames.append({"id": current.get("id"), "data": json.loads(current["data"])})
    return frames


# --------------------------------------------------------------- opening


def test_opening_returns_the_instant_brief_within_budget(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    assert incident["incident_id"]
    assert incident["address_id"] == ADDRESS
    assert incident["profile_snapshot_id"]
    assert incident["grant_id"]
    assert incident["cold_start"] is False

    brief = incident["brief"]
    assert brief["stage"] == "INSTANT"
    assert brief["version"] == 1
    assert brief["model_invoked"] is False
    assert brief["narrative"] is None
    # Persisted before it was returned, exactly as before it is streamed.
    assert brief["persisted_at"] is not None
    assert brief["content_hash"]


def test_a_cold_address_opens_and_says_the_structure_is_unknown(
    app_client: TestClient,
) -> None:
    app_client.post(f"{PREFIX}/districts/{DISTRICT}/poll")
    response = app_client.post(
        f"{PREFIX}/incidents",
        json={"address": "sf-3120-24th", "cad_ref": "CAD-0002"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["cold_start"] is True
    assert "structure.stories" in body["brief"]["unknowns"]


def test_an_unresolvable_address_is_a_404(app_client: TestClient) -> None:
    response = app_client.post(
        f"{PREFIX}/incidents", json={"address": "1 Nowhere Ave", "cad_ref": "CAD-0003"}
    )
    assert response.status_code == 404


# ------------------------------------------------------------------ intake

#: A 911 call as one arrives: prose, under stress, and partly at odds with the
#: filed record. None of it is a fact and the response has to say so.
CALL = (
    "Caller reports heavy smoke on the third floor of the apartment building. "
    "Two people are still inside. The driveway is blocked by a delivery truck. "
    "There are propane cylinders by the back door."
)


def test_a_narrative_sent_with_the_dispatch_is_read_after_the_instant_brief(
    app_client: TestClient,
) -> None:
    """The brief in the 201 is version 1 and model-free however the call reads.

    A commander gets the structural picture from the record the department
    already had; what a caller said arrives behind it as a marked amendment. If
    that order ever inverts, the instant brief inherits a model's latency.
    """
    app_client.post(f"{PREFIX}/districts/{DISTRICT}/poll")
    response = app_client.post(
        f"{PREFIX}/incidents",
        json={"address": ADDRESS, "cad_ref": "CAD-0100", "intake_narrative": CALL},
    )
    assert response.status_code == 201
    body = response.json()

    assert body["brief"]["version"] == 1
    assert body["brief"]["model_invoked"] is False

    intake = body["intake"]
    assert intake["accepted"] is True
    assert intake["brief_version"] > 1
    reported = {line["intake_key"] for line in intake["reported"]}
    assert "intake.entrapment_reported" in reported
    # Every reported value points at the words in the transcript it came from.
    for line in intake["reported"]:
        assert CALL[line["start_offset"] : line["end_offset"]] == line["quoted_text"]


def test_an_intake_arriving_later_amends_the_brief_and_names_who_it_reached(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """A callback or a CAD update goes down the same path as the first call."""
    incident_id = incident["incident_id"]
    response = app_client.post(
        f"{PREFIX}/incidents/{incident_id}/intake",
        json={"narrative": CALL, "channel": "CAD_NARRATIVE"},
    )
    assert response.status_code == 202
    body = response.json()

    assert body["channel"] == "CAD_NARRATIVE"
    woken = {line["agent_ref"].split("@")[0] for line in body["woken"]}
    assert "agency-notifier" in woken
    assert all(line["rule_ids"] for line in body["woken"])
    # The gap the incident loop has, stated on every intake that hits it.
    assert "reported-hazardous-material-is-checked-against-tier-ii" in body["unmatched_rule_ids"]


@pytest.mark.invariant
def test_a_reported_line_is_never_rendered_as_a_confirmed_one(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """The distinction has to survive serialisation, because a console reads JSON.

    A caller's "third floor" and a surveyed storey count reaching a tablet as
    indistinguishable objects is the failure this guards: on screen they would
    be two lines of the same weight.
    """
    incident_id = incident["incident_id"]
    app_client.post(f"{PREFIX}/incidents/{incident_id}/intake", json={"narrative": CALL})

    brief = app_client.get(f"{PREFIX}/incidents/{incident_id}/brief").json()
    lines = [item for section in brief["sections"] for item in section["items"]]
    reported = [item for item in lines if item.get("reported_note")]

    assert reported
    for item in reported:
        assert item["status"] != "CONFIRMED"
        assert item["fact_id"] is None
        assert item["provenance"] is None
        assert "caller report" in item["reported_note"] or "reported by" in item["reported_note"]


@pytest.mark.invariant
def test_a_brief_carrying_a_911_call_still_contains_no_tactical_language(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """The intake widened what reaches the brief; it did not widen what it says.

    Everything the interceptor writes about a call is attribution -- who said
    it, when, and what is on file instead. There is no template here that
    advises, and the routing notes name agents rather than actions.
    """
    incident_id = incident["incident_id"]
    app_client.post(f"{PREFIX}/incidents/{incident_id}/intake", json={"narrative": CALL})
    app_client.post(f"{PREFIX}/incidents/{incident_id}/brief/enrich")

    with app_client.stream("GET", f"{PREFIX}/incidents/{incident_id}/stream") as response:
        raw = response.read().decode().lower()

    for phrase in FORBIDDEN_TACTICAL:
        assert phrase not in raw, phrase


@pytest.mark.degraded
def test_an_empty_narrative_is_refused_by_the_contract_not_by_the_model(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """A request that says nothing is a bad request, not a model call."""
    response = app_client.post(
        f"{PREFIX}/incidents/{incident['incident_id']}/intake", json={"narrative": ""}
    )
    assert response.status_code == 422


# ------------------------------------------------------------------- SSE


def test_the_stream_delivers_versions_in_order(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    incident_id = incident["incident_id"]
    app_client.post(f"{PREFIX}/incidents/{incident_id}/brief/enrich")

    with app_client.stream("GET", f"{PREFIX}/incidents/{incident_id}/stream") as response:
        assert response.status_code == 200
        frames = _frames(response.read().decode())

    assert [f["data"]["version"] for f in frames] == [1, 2]
    assert [f["id"] for f in frames] == ["1", "2"]
    assert frames[0]["data"]["stage"] == "INSTANT"
    assert frames[1]["data"]["stage"] == "ENRICHED"


@pytest.mark.invariant
def test_every_streamed_frame_is_already_in_the_log(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """Persist before transmit, checked from the outside."""
    incident_id = incident["incident_id"]
    app_client.post(f"{PREFIX}/incidents/{incident_id}/brief/enrich")

    with app_client.stream("GET", f"{PREFIX}/incidents/{incident_id}/stream") as response:
        frames = _frames(response.read().decode())

    log = app_client.get(f"{PREFIX}/incidents/{incident_id}/log").json()
    logged_hashes = {
        entry["content"].get("content_hash")
        for entry in log["entries"]
        if entry["entry_type"] == "BRIEF_EMITTED"
    }
    for frame in frames:
        assert frame["data"]["persisted_at"] is not None
        assert frame["data"]["content_hash"] in logged_hashes


def test_a_reconnecting_client_resumes_after_the_version_it_saw(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    incident_id = incident["incident_id"]
    app_client.post(f"{PREFIX}/incidents/{incident_id}/brief/enrich")

    with app_client.stream(
        "GET",
        f"{PREFIX}/incidents/{incident_id}/stream",
        headers={"Last-Event-ID": "1"},
    ) as response:
        frames = _frames(response.read().decode())

    assert [f["data"]["version"] for f in frames] == [2]


def test_a_malformed_resume_point_replays_the_whole_stream(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """Showing the brief again is always safe; erroring is not."""
    incident_id = incident["incident_id"]
    with app_client.stream(
        "GET",
        f"{PREFIX}/incidents/{incident_id}/stream",
        headers={"Last-Event-ID": "not-a-number"},
    ) as response:
        frames = _frames(response.read().decode())
    assert [f["data"]["version"] for f in frames] == [1]


def test_a_replayed_stream_matches_what_the_first_one_sent(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    incident_id = incident["incident_id"]
    app_client.post(f"{PREFIX}/incidents/{incident_id}/brief/enrich")

    def read() -> list[dict[str, Any]]:
        with app_client.stream("GET", f"{PREFIX}/incidents/{incident_id}/stream") as response:
            return _frames(response.read().decode())

    first, second = read(), read()
    assert [f["data"]["content_hash"] for f in first] == [f["data"]["content_hash"] for f in second]


# ------------------------------------------------------------ the 360


def test_an_ic_resolution_produces_a_marked_amendment(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    incident_id = incident["incident_id"]
    conflict_id = incident["brief"]["conflict_ids"][0]

    before = app_client.get(f"{PREFIX}/buildings/{ADDRESS}").json()["profile_version"]
    response = app_client.post(
        f"{PREFIX}/incidents/{incident_id}/resolutions",
        json={
            "conflict_id": conflict_id,
            "observed_value": "3",
            "resolved_by": "bc-09",
            "note": "Walked the Charlie side.",
        },
    )
    assert response.status_code == 201
    body = response.json()

    after = app_client.get(f"{PREFIX}/buildings/{ADDRESS}").json()
    assert after["profile_version"] > before
    assert body["profile_version"] == after["profile_version"]

    latest = app_client.get(f"{PREFIX}/incidents/{incident_id}/brief").json()
    assert latest["stage"] == "AMENDMENT"
    assert latest["version"] == body["brief_version"]


def test_registering_thermal_marks_the_other_faces_unscanned(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    incident_id = incident["incident_id"]
    response = app_client.post(
        f"{PREFIX}/incidents/{incident_id}/thermal",
        json={"face": "ALPHA", "region_temps_c": [21.0, 24.0, 88.0], "coverage": 0.75},
    )
    assert response.status_code == 202
    body = response.json()
    assert set(body["unscanned_faces"]) == {"BRAVO", "CHARLIE", "DELTA"}
    assert body["voids"] == 1


def test_an_unknown_face_is_refused(app_client: TestClient, incident: dict[str, Any]) -> None:
    response = app_client.post(
        f"{PREFIX}/incidents/{incident['incident_id']}/thermal",
        json={"face": "ECHO", "region_temps_c": [20.0]},
    )
    assert response.status_code == 422


# --------------------------------------------------------------- resources


@pytest.mark.authorization
def test_a_commitment_waits_and_a_notification_does_not(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    incident_id = incident["incident_id"]

    told = app_client.post(
        f"{PREFIX}/incidents/{incident_id}/resources", json={"kind_id": "water-supply"}
    ).json()
    assert told["action"] == "ALLOW"
    assert told["external_ref"]

    committed = app_client.post(
        f"{PREFIX}/incidents/{incident_id}/resources", json={"kind_id": "gas-shutoff"}
    ).json()
    assert committed["action"] == "REQUIRE_APPROVAL"
    assert committed["external_ref"] is None
    assert committed["approval_id"]

    approved = app_client.post(
        f"{PREFIX}/incidents/{incident_id}/approvals/{committed['approval_id']}"
    ).json()
    assert approved["executed"] is True
    assert approved["external_ref"]


# ----------------------------------------------------------------- closing


def test_closing_seals_the_log_and_returns_the_draft(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    incident_id = incident["incident_id"]
    app_client.post(f"{PREFIX}/incidents/{incident_id}/benchmarks/ARRIVAL")

    response = app_client.post(
        f"{PREFIX}/incidents/{incident_id}/close", json={"closed_by": "bc-09"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["grant_revoked_at"]
    assert body["log_sealed_at"]
    assert body["neris_draft"]["log_entries"] == body["log_entries"]

    log = app_client.get(f"{PREFIX}/incidents/{incident_id}/log").json()
    assert log["sealed_at"]
    assert [e["sequence"] for e in log["entries"]] == list(range(len(log["entries"])))


# ------------------------------------------------ forbidden tactical language


@pytest.mark.invariant
def test_no_brief_contains_tactical_language(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """The system delivers information. Tactics belong to the commander.

    Drives a full incident and scans every rendered string in every emission --
    including the model-composed narrative, which is the one that could drift.
    """
    incident_id = incident["incident_id"]
    app_client.post(f"{PREFIX}/incidents/{incident_id}/brief/enrich")
    app_client.post(
        f"{PREFIX}/incidents/{incident_id}/thermal",
        json={"face": "ALPHA", "region_temps_c": [21.0, 90.0]},
    )
    app_client.post(
        f"{PREFIX}/incidents/{incident_id}/resolutions",
        json={
            "conflict_id": incident["brief"]["conflict_ids"][0],
            "observed_value": "3",
            "resolved_by": "bc-09",
        },
    )

    with app_client.stream("GET", f"{PREFIX}/incidents/{incident_id}/stream") as response:
        raw = response.read().decode().lower()

    for phrase in FORBIDDEN_TACTICAL:
        assert phrase not in raw, phrase


@pytest.mark.invariant
def test_the_truss_timer_is_never_a_collapse_prediction(app_client: TestClient) -> None:
    """A published material window and the clock, side by side, with a caveat."""
    app_client.post(f"{PREFIX}/districts/{DISTRICT}/poll")
    opened = app_client.post(
        f"{PREFIX}/incidents", json={"address": "sf-1215-fell", "cad_ref": "CAD-0004"}
    ).json()

    rendered = " ".join(
        item["value_render"] for section in opened["brief"]["sections"] for item in section["items"]
    )
    if "truss" in rendered.lower():
        assert "published" in rendered.lower()
        assert "not a prediction" in rendered.lower()
        assert "will collapse" not in rendered.lower()
