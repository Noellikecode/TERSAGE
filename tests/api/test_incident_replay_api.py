"""The replay endpoint: what the commander saw, reconstructed from the record.

``IncidentReplay`` existed as a service with tests and no route, which made the
capability unreachable over HTTP -- an audit trail nobody can request is an
audit trail that does not exist for the investigation that needs it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

PREFIX = "/api/v1"
DISTRICT = "sffd-district-03"
ADDRESS = "sf-0450-hayes"


@pytest.fixture
def incident(app_client: TestClient) -> dict[str, Any]:
    app_client.post(f"{PREFIX}/districts/{DISTRICT}/poll")
    response = app_client.post(
        f"{PREFIX}/incidents",
        json={"address": ADDRESS, "cad_ref": "CAD-REPLAY-1", "alarm_level": 2},
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    return body


def _replay(client: TestClient, incident_id: str) -> dict[str, Any]:
    response = client.get(f"{PREFIX}/internal/audit/incidents/{incident_id}/replay")
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def test_an_incident_replays_over_http(app_client: TestClient, incident: dict[str, Any]) -> None:
    body = _replay(app_client, incident["incident_id"])
    assert body["incident_id"] == incident["incident_id"]
    assert body["intact"] is True
    assert body["entries"], "an opened incident produced no log entries"
    assert body["digest"]


def test_the_replay_is_ordered_and_gapless(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """A log with a gap is a log somebody removed an entry from."""
    body = _replay(app_client, incident["incident_id"])
    sequences = [entry["sequence"] for entry in body["entries"]]
    assert sequences == sorted(sequences)
    assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))


def test_two_replays_of_an_untouched_incident_agree(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    first = _replay(app_client, incident["incident_id"])
    second = _replay(app_client, incident["incident_id"])
    assert first["digest"] == second["digest"]
    assert first["entries"] == second["entries"]


def test_the_replay_reports_recorded_versions_not_todays_build(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """A NIOSH investigation asks what ran then, not what ships now."""
    body = _replay(app_client, incident["incident_id"])
    assert body["agent_versions"], "no agent version was recorded"
    for entry in body["entries"]:
        assert entry["content_hash"]
        assert entry["intact"] is True


def test_every_entry_carries_the_snapshot_it_was_built_from(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    body = _replay(app_client, incident["incident_id"])
    assert body["profile_snapshot_id"]
    for entry in body["entries"]:
        assert entry["profile_snapshot_id"]


def test_a_closed_incident_replays_sealed(app_client: TestClient, incident: dict[str, Any]) -> None:
    incident_id = incident["incident_id"]
    closed = app_client.post(
        f"{PREFIX}/incidents/{incident_id}/close",
        json={"closed_by": "bc-2"},
    )
    assert closed.status_code in (200, 201), closed.text
    body = _replay(app_client, incident_id)
    assert body["sealed_at"] is not None
    assert body["intact"] is True


def test_an_unknown_incident_is_a_404(app_client: TestClient) -> None:
    response = app_client.get(f"{PREFIX}/internal/audit/incidents/inc-nonexistent/replay")
    assert response.status_code == 404
    assert response.json()["error"]["code"]


def test_the_replay_requires_a_caller(client_factory: Any) -> None:
    """An audit trail is not public."""
    with client_factory(None) as anonymous:
        response = anonymous.get(f"{PREFIX}/internal/audit/incidents/inc-x/replay")
    assert response.status_code in (401, 403)
