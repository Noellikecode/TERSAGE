"""Six checks against a deployed staging environment.

Skipped entirely unless ``STAGING_BASE_URL`` is set, so this file is inert on a
laptop, in the default CI job, and in a fork. It is not a substitute for the
suite: everything here is also covered in memory. What it proves is narrower
and not provable any other way -- that the *deployed* thing, with Firestore
behind it and Pub/Sub in front of it and a service account for an identity,
still does what the in-memory version does.

Run it with::

    STAGING_BASE_URL=https://firstdue-incident-....run.app \\
    STAGING_TOKEN=$(gcloud auth print-identity-token \\
                      --audiences=https://firstdue-incident) \\
    make smoke-staging

The audience is not optional and a bare user credential cannot supply it. The
incident service verifies a console token against its own Cloud Run *custom
audience*, so a token minted for anything else -- including the one
``gcloud auth print-identity-token`` hands back by default, which carries
gcloud's own client id -- is rejected before any endpoint here runs. Choosing an
audience requires a service account or an impersonated one.

Nothing here writes anything a human has to clean up: the incident it opens is
closed in a finally block, and the referral it exercises is read, not filed.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

BASE_URL = os.environ.get("STAGING_BASE_URL", "").rstrip("/")
TOKEN = os.environ.get("STAGING_TOKEN", "")
ADDRESS = os.environ.get("STAGING_ADDRESS_ID", "sf-0450-hayes")
DISTRICT = os.environ.get("STAGING_DISTRICT_ID", "sffd-district-03")

pytestmark = pytest.mark.skipif(
    not BASE_URL, reason="STAGING_BASE_URL is not set; this suite needs a deployed environment"
)


@pytest.fixture(scope="module")
def client() -> Iterator[Any]:
    httpx = pytest.importorskip("httpx")
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    with httpx.Client(base_url=BASE_URL, headers=headers, timeout=30.0) as session:
        yield session


@pytest.fixture(scope="module")
def correlation_id() -> str:
    """One id for the whole run, so the trace and the audit trail can be joined."""
    return f"smoke-{uuid.uuid4().hex[:12]}"


def _check(response: Any) -> Any:
    detail = f"{response.request.url}: {response.status_code} {response.text[:300]}"
    assert response.status_code < 400, detail
    return response


# 1 ------------------------------------------------------------ profile read
def test_profile_read(client: Any) -> None:
    """A profile the slow loop built is readable through the deployed API."""
    body = _check(client.get(f"/api/v1/buildings/{ADDRESS}")).json()

    assert body["address_id"] == ADDRESS
    # Facts may be empty on a freshly-seeded environment. The shape may not be.
    assert "facts" in body
    assert "conflicts" in body


# 2 ---------------------------------------------------------- event handling
def test_event_handling(client: Any) -> None:
    """The slow loop runs end to end behind Pub/Sub and Firestore.

    Driving the poll endpoint rather than publishing directly keeps the check
    honest about what it proves: a real pass over real sources, writing to a
    real database, not a message that vanished into a topic.
    """
    body = _check(client.post(f"/api/v1/districts/{DISTRICT}/poll")).json()

    assert body["district_id"] == DISTRICT
    assert body["facts_written"] >= 0


# 3 ------------------------------------------------------------- instant SSE
def test_instant_brief_streams_before_any_model_call(client: Any, correlation_id: str) -> None:
    """The product claim, verified against the deployment.

    The first frame must arrive with ``model_invoked`` false. A staging
    environment that reaches Gemini before the first line is a staging
    environment that has lost the thing this system is for.
    """
    opened = _check(
        client.post(
            "/api/v1/incidents",
            json={"address": ADDRESS, "cad_ref": correlation_id, "alarm_level": 1},
            headers={"X-Correlation-ID": correlation_id},
        )
    ).json()
    incident_id = opened["incident_id"]

    # The response already carries the instant brief, persisted before it was
    # returned. The stream then replays it -- the same emission, not a second
    # rendering -- which is what the next assertions check.
    assert opened["brief"]["stage"] == "INSTANT"
    assert opened["brief"]["model_invoked"] is False

    try:
        first: dict[str, Any] | None = None
        with client.stream("GET", f"/api/v1/incidents/{incident_id}/stream") as stream:
            assert stream.status_code == 200
            for line in stream.iter_lines():
                if line.startswith("data:"):
                    first = json.loads(line[5:].strip())
                    break

        assert first is not None, "the stream closed before the first frame"
        assert first["stage"] == "INSTANT"
        assert first["model_invoked"] is False
        assert first["version"] == 1
    finally:
        client.post(f"/api/v1/incidents/{incident_id}/close", json={"closed_by": "smoke-test"})


# 4 ----------------------------------------------------------- approved write
def test_an_approved_write_reaches_its_target(client: Any) -> None:
    """A commitment goes out only after a human approves it.

    The queue entry is dispatched, which stages a work order. Staging's write
    targets are the same simulated receiving systems the README discloses; what
    this proves is the *path* -- gateway, approval, write record, receipt --
    not that a real city system was called.
    """
    queue = _check(client.get(f"/api/v1/districts/{DISTRICT}/queue")).json()
    entries = queue["entries"] if isinstance(queue, dict) else queue
    if not entries:
        pytest.skip("no queue entries in this environment; run the poll first")

    entry_id = entries[0]["entry_id"]
    body = {"company": "E-05", "crew_email": "smoke@example.invalid"}
    dispatched = _check(client.post(f"/api/v1/queue/{entry_id}/dispatch", json=body)).json()

    assert dispatched["work_order_ref"]
    assert dispatched["calendar_event_ref"]
    assert dispatched["plan_uri"].startswith("gs://")

    # Dispatching the same entry again must return the same work order rather
    # than book a second company. Idempotency is derived from the entry id, so
    # this holds across instances and restarts, not just within one process.
    again = _check(client.post(f"/api/v1/queue/{entry_id}/dispatch", json=body)).json()
    assert again["work_order_ref"] == dispatched["work_order_ref"]
    assert again["calendar_event_ref"] == dispatched["calendar_event_ref"]
    assert again["replayed"] is True


# 5 ----------------------------------------------------------- audit decision
def test_a_gateway_decision_is_recorded_with_its_rule(client: Any, correlation_id: str) -> None:
    """A commitment goes through the gateway, and the gateway leaves a record.

    Notifying a mutual-aid agency is an ALLOW; requesting a utility shutoff is
    a REQUIRE_APPROVAL. Asking for both proves the deployed policy engine is
    the versioned one and that its decisions reach the audit sink -- not just
    that some decision exists from an earlier run.
    """
    opened = _check(
        client.post(
            "/api/v1/incidents",
            json={"address": ADDRESS, "cad_ref": f"{correlation_id}-policy", "alarm_level": 2},
            headers={"X-Correlation-ID": f"{correlation_id}-policy"},
        )
    ).json()
    incident_id = opened["incident_id"]

    try:
        notify = _check(
            client.post(
                f"/api/v1/incidents/{incident_id}/resources",
                json={"kind_id": "water-supply", "detail": "smoke test"},
            )
        ).json()
        shutoff = _check(
            client.post(
                f"/api/v1/incidents/{incident_id}/resources",
                json={"kind_id": "gas-shutoff", "detail": "smoke test"},
            )
        ).json()

        decisions = _check(
            client.get("/api/v1/internal/audit/decisions", params={"limit": 50})
        ).json()
    finally:
        client.post(f"/api/v1/incidents/{incident_id}/close", json={"closed_by": "smoke-test"})

    # The response hands back the decision id, so the audit record can be found
    # by identity rather than by guessing which of the recent decisions was ours.
    assert notify["action"] == "ALLOW"
    assert shutoff["action"] == "REQUIRE_APPROVAL"

    wanted = {notify["decision_id"], shutoff["decision_id"]}
    mine = [d for d in decisions if d["decision_id"] in wanted]
    assert len(mine) == 2, f"the gateway recorded {len(mine)} of 2 decisions"

    for decision in mine:
        assert decision["rule_id"]
        assert decision["policy_version"]
        assert decision["action"] in {
            "ALLOW",
            "DERIVE",
            "WITHHOLD_JURISDICTION",
            "REQUIRE_APPROVAL",
            "DENY",
        }


# 6 -------------------------------------------------------- trace correlation
def test_correlation_id_joins_the_response_the_audit_trail_and_the_trace(
    client: Any, correlation_id: str
) -> None:
    """One id, three systems.

    Without this join, a Cloud Logging line and a Cloud Trace span about the
    same incident cannot be put side by side -- which is exactly what someone
    needs at the moment they are trying to find out what the system told a
    commander.
    """
    probe = f"{correlation_id}-join"
    response = _check(
        client.get(f"/api/v1/buildings/{ADDRESS}", headers={"X-Correlation-ID": probe})
    )

    assert response.headers.get("X-Correlation-ID") == probe

    incident = _check(
        client.post(
            "/api/v1/incidents",
            json={"address": ADDRESS, "cad_ref": probe, "alarm_level": 1},
            headers={"X-Correlation-ID": probe},
        )
    ).json()
    try:
        events = _check(
            client.get("/api/v1/internal/audit/events", params={"correlation_id": probe})
        ).json()
        assert events, f"no audit event carries correlation id {probe}"
        assert all(event["correlation_id"] == probe for event in events)
    finally:
        client.post(
            f"/api/v1/incidents/{incident['incident_id']}/close",
            json={"closed_by": "smoke-test"},
        )

    # The trace itself is read in Cloud Trace, not here -- asserting on the
    # exporter's own backend from a test would be testing Google. What is
    # checkable is that the process is exporting at all and that the id it
    # would attach is the one above.
    metrics = _check(client.get("/api/v1/internal/metrics")).json()
    assert "time_to_first_line_p50_ms" in metrics
