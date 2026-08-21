"""The console API, against a district the slow loop has actually run over."""

from __future__ import annotations

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
    assert body["open_conflicts"] == 1
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
    assert conflict["severity"] == 4
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
