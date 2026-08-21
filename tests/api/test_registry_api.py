"""The registry API: publish, discover, subscribe, and stay pinned."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from firstdue.registry.descriptors import FLEET_VERSION, RECORDS_WATCHER

PREFIX = "/api/v1/registry"


def _descriptor_payload(version: str, **overrides: Any) -> dict[str, Any]:
    payload = RECORDS_WATCHER.model_copy(update={"version": version}).model_dump(mode="json")
    payload.update(overrides)
    return payload


def test_the_whole_fleet_is_published_at_startup(app_client: TestClient) -> None:
    from firstdue.registry.descriptors import FLEET

    response = app_client.get(f"{PREFIX}/agents")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(FLEET)
    assert {a["agent_id"] for a in body["agents"]} >= {
        "records-watcher",
        "incident-controller",
        "incident-recorder",
    }


def test_a_descriptor_serialises_its_whole_contract(app_client: TestClient) -> None:
    response = app_client.get(f"{PREFIX}/agents/records-watcher/{FLEET_VERSION}")
    assert response.status_code == 200
    body = response.json()
    assert body["ref"] == f"records-watcher@{FLEET_VERSION}"
    assert body["publisher_department"] == "building"
    assert body["required_scopes"] == sorted(body["required_scopes"])
    assert body["classifications_accessed"]
    assert body["latency_target_ms"] > 0


def test_agents_can_be_discovered_by_loop_and_capability(app_client: TestClient) -> None:
    response = app_client.get(
        f"{PREFIX}/agents", params={"loop": "INCIDENT", "capability": "WRITE"}
    )
    assert response.status_code == 200
    assert {a["agent_id"] for a in response.json()["agents"]} == {
        "agency-notifier",
        "incident-recorder",
    }


def test_agents_can_be_discovered_by_publisher(app_client: TestClient) -> None:
    response = app_client.get(f"{PREFIX}/agents", params={"publisher_department": "building"})
    assert [a["agent_id"] for a in response.json()["agents"]] == ["records-watcher"]


def test_an_unknown_version_is_a_404_with_the_error_envelope(app_client: TestClient) -> None:
    response = app_client.get(f"{PREFIX}/agents/records-watcher/9.9.9")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_publishing_a_new_version_creates_it(app_client: TestClient) -> None:
    response = app_client.post(f"{PREFIX}/agents", json=_descriptor_payload("1.1.0"))
    assert response.status_code == 201
    assert response.json()["version"] == "1.1.0"


@pytest.mark.idempotency
def test_republishing_an_identical_descriptor_is_a_no_op(app_client: TestClient) -> None:
    payload = _descriptor_payload("1.2.0")
    assert app_client.post(f"{PREFIX}/agents", json=payload).status_code == 201
    again = app_client.post(f"{PREFIX}/agents", json=payload)
    assert again.status_code == 200
    assert again.json()["version"] == "1.2.0"


@pytest.mark.invariant
def test_republishing_a_changed_descriptor_at_the_same_version_is_refused(
    app_client: TestClient,
) -> None:
    """A version somebody pinned must not turn into different code underneath them."""
    payload = _descriptor_payload("1.3.0")
    assert app_client.post(f"{PREFIX}/agents", json=payload).status_code == 201

    changed = _descriptor_payload("1.3.0", role_summary="Now also files referrals.")
    response = app_client.post(f"{PREFIX}/agents", json=changed)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPEND_ONLY_VIOLATION"


@pytest.mark.invariant
def test_a_pinned_version_survives_a_newer_publication(app_client: TestClient) -> None:
    """The NIOSH requirement: what ran two years ago must still resolve."""
    before = app_client.get(f"{PREFIX}/subscriptions/fire/records-watcher/resolved").json()
    assert before["version"] == FLEET_VERSION

    assert app_client.post(f"{PREFIX}/agents", json=_descriptor_payload("2.0.0")).status_code == 201

    after = app_client.get(f"{PREFIX}/subscriptions/fire/records-watcher/resolved").json()
    assert after["version"] == FLEET_VERSION
    assert after == before


def test_a_department_can_move_its_pin_deliberately(app_client: TestClient) -> None:
    """Upgrading is a decision, not something that happens to a department."""
    app_client.post(f"{PREFIX}/agents", json=_descriptor_payload("3.0.0"))
    response = app_client.post(
        f"{PREFIX}/subscriptions",
        json={
            "subscriber_department": "police",
            "agent_id": "records-watcher",
            "pinned_version": "3.0.0",
        },
    )
    assert response.status_code == 201
    assert response.json()["ref"] == "records-watcher@3.0.0"

    resolved = app_client.get(f"{PREFIX}/subscriptions/police/records-watcher/resolved")
    assert resolved.json()["version"] == "3.0.0"
    # The fire department's pin is untouched.
    fire = app_client.get(f"{PREFIX}/subscriptions/fire/records-watcher/resolved")
    assert fire.json()["version"] == FLEET_VERSION


def test_subscribing_to_an_unpublished_version_is_a_404(app_client: TestClient) -> None:
    response = app_client.post(
        f"{PREFIX}/subscriptions",
        json={
            "subscriber_department": "police",
            "agent_id": "records-watcher",
            "pinned_version": "8.8.8",
        },
    )
    assert response.status_code == 404


def test_a_version_range_cannot_be_pinned(app_client: TestClient) -> None:
    """Never a range, never "latest" -- the type refuses both."""
    for bad in ("^1.0.0", "latest", "1.x"):
        response = app_client.post(
            f"{PREFIX}/subscriptions",
            json={
                "subscriber_department": "police",
                "agent_id": "records-watcher",
                "pinned_version": bad,
            },
        )
        assert response.status_code == 422


def test_subscriptions_are_listed_per_department(app_client: TestClient) -> None:
    response = app_client.get(f"{PREFIX}/subscriptions", params={"subscriber_department": "fire"})
    assert response.status_code == 200
    from firstdue.registry.descriptors import FLEET

    body = response.json()
    assert body["count"] == len(FLEET)
    assert all(s["subscriber_department"] == "fire" for s in body["subscriptions"])
    assert all(s["pinned_version"] == FLEET_VERSION for s in body["subscriptions"])


def test_resolving_an_unsubscribed_agent_is_a_404(app_client: TestClient) -> None:
    response = app_client.get(f"{PREFIX}/subscriptions/water/records-watcher/resolved")
    assert response.status_code == 404
