"""Token-streamed prose, and the contract that keeps it honest.

The PRD asks for the enriched brief to stream "token by token" so a commander
watching a brief fill in can see it filling in. The risk that creates is
obvious: prose on a screen that the record does not contain. These tests pin
the two-frame contract that resolves it --

* ``narrative`` frames are provisional, carry no facts, and can be withdrawn;
* the closing ``brief`` frame is the persisted emission, and one always arrives.

There is no path where provisional prose is left standing with nothing
authoritative behind it.
"""

from __future__ import annotations

import json
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
        json={"address": ADDRESS, "cad_ref": "CAD-STREAM-1", "alarm_level": 2},
    )
    assert response.status_code == 201
    body: dict[str, Any] = response.json()
    return body


def _events(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body into (event, data) pairs."""
    out: list[tuple[str, dict[str, Any]]] = []
    current: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            if current.get("data"):
                out.append((current.get("event", "message"), json.loads(current["data"])))
            current = {}
            continue
        key, _, value = line.partition(":")
        current[key.strip()] = value.strip()
    if current.get("data"):
        out.append((current.get("event", "message"), json.loads(current["data"])))
    return out


def _stream(client: TestClient, incident_id: str) -> list[tuple[str, dict[str, Any]]]:
    response = client.get(f"{PREFIX}/incidents/{incident_id}/brief/stream-enriched")
    assert response.status_code == 200, response.text
    return _events(response.text)


def test_prose_arrives_in_more_than_one_frame(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """If it all arrives at once, nothing was streamed."""
    events = _stream(app_client, incident["incident_id"])
    narrative = [data for name, data in events if name == "narrative"]
    assert len(narrative) > 1


def test_every_narrative_frame_is_marked_provisional(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """A console cannot render one without reading that it may be withdrawn."""
    events = _stream(app_client, incident["incident_id"])
    for name, data in events:
        if name == "narrative":
            assert data["provisional"] is True
            assert data["for_version"] >= 0


def test_a_narrative_frame_carries_no_facts(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """Provisional prose is prose. It is not part of the record."""
    events = _stream(app_client, incident["incident_id"])
    for name, data in events:
        if name == "narrative":
            assert set(data) == {"incident_id", "for_version", "text", "provisional"}
            assert "sections" not in data
            assert "content_hash" not in data
            assert "version" not in data


def test_the_stream_ends_with_a_persisted_brief(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """The authoritative frame is last, and it is in the record."""
    events = _stream(app_client, incident["incident_id"])
    name, data = events[-1]
    assert name == "brief"
    assert data["stage"] == "ENRICHED"
    assert data["persisted_at"], "the closing frame was not persisted"
    assert data["content_hash"]


def test_the_streamed_prose_matches_the_persisted_narrative(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """Every character shown ends up in the record, in the same order.

    This is what makes streaming compatible with persist-before-transmit: the
    provisional text is a prefix-by-prefix construction of the narrative the
    log stores, not a different rendering of it.
    """
    events = _stream(app_client, incident["incident_id"])
    streamed = "".join(data["text"] for name, data in events if name == "narrative")
    _, final = events[-1]
    assert final["narrative_available"] is True
    assert final["narrative"] == streamed.strip()


def test_the_closing_brief_is_a_new_version(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    events = _stream(app_client, incident["incident_id"])
    _, final = events[-1]
    narrative_frames = [d for n, d in events if n == "narrative"]
    assert final["version"] == narrative_frames[0]["for_version"]


def test_narrative_frames_carry_no_event_id(
    app_client: TestClient, incident: dict[str, Any]
) -> None:
    """Resuming from half a sentence would replay it as a brief version."""
    response = app_client.get(f"{PREFIX}/incidents/{incident['incident_id']}/brief/stream-enriched")
    body = response.text.replace("\r\n", "\n")
    blocks = [b for b in body.split("\n\n") if "event: narrative" in b]
    assert blocks
    for block in blocks:
        # An SSE field is a line prefix. "address_id:" inside the JSON payload
        # is data, not a field.
        assert not any(line.startswith("id:") for line in block.split("\n"))


def test_an_unknown_incident_is_a_clean_404(app_client: TestClient) -> None:
    """The error must land before the response starts, or it is not an error.

    A raise inside the generator has already written 200 and the SSE content
    type to the socket; the connection simply breaks and no envelope is ever
    produced.
    """
    response = app_client.get(f"{PREFIX}/incidents/inc-nope/brief/stream-enriched")
    assert response.status_code == 404
    assert response.json()["error"]["code"]


@pytest.mark.degraded
def test_a_refused_composition_still_ends_with_an_authoritative_frame(
    client_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prose is withdrawn by a brief that says the narrative is unavailable."""
    from firstdue.adapters.fake.model import FakeModelClient
    from firstdue.api.dependencies import Role

    with client_factory(Role.CHIEF) as client:
        client.post(f"{PREFIX}/districts/{DISTRICT}/poll")
        opened = client.post(
            f"{PREFIX}/incidents",
            json={"address": ADDRESS, "cad_ref": "CAD-REJECT-1", "alarm_level": 2},
        ).json()

        container = client.app.state.container
        session = None
        from firstdue.api.routes.incidents import get_session

        session = get_session(container)
        monkeypatch.setattr(session.reconciler, "_model", FakeModelClient(reject_output=True))

        events = _stream(client, opened["incident_id"])

    assert not [d for n, d in events if n == "narrative"], "refused prose was shown"
    name, final = events[-1]
    assert name == "brief"
    assert final["narrative_available"] is False
    assert final["narrative"] is None
    assert final["persisted_at"]
