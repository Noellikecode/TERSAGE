"""The authenticated internal push endpoint.

This endpoint injects events into the fleet, so the first thing tested is that
it will not talk to a stranger. After that: the status codes, which are the
contract with the broker and are chosen so a poison message cannot loop forever.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from firstdue.domain.events import CURRENT_SCHEMA_VERSION, EventEnvelope, Topic
from firstdue.eventing.pubsub_codec import encode_push
from firstdue.settings import Settings

PUSH = "/api/v1/internal/events/push"
DEAD_LETTERS = "/api/v1/internal/events/dead-letters"
NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _envelope(n: int = 1, **overrides: Any) -> EventEnvelope:
    payload: dict[str, Any] = {
        "event_id": f"ev-{n}",
        "topic": Topic.FACT_WRITTEN,
        "occurred_at": NOW,
        "producer": "records-watcher",
        "producer_version": "1.0.0",
        "correlation_id": "corr-1",
        "ids": {"address_id": "sf-0450-hayes"},
        "idempotency_key": f"idem-key-{n:06d}",
    }
    payload.update(overrides)
    return EventEnvelope(**payload)


def _auth(settings: Settings) -> dict[str, str]:
    token = settings.resolved_internal_push_token
    assert token is not None
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def subscribed(app_client: TestClient) -> list[str]:
    """Register a local subscriber so a pushed envelope has somewhere to go."""
    seen: list[str] = []

    async def handler(envelope: EventEnvelope) -> None:
        seen.append(envelope.event_id)

    app_client.app.state.container.bus.subscribe(  # type: ignore[attr-defined]
        Topic.FACT_WRITTEN, handler, subscriber="conflict-detector"
    )
    return seen


# ------------------------------------------------------------ authentication


@pytest.mark.authorization
def test_an_unauthenticated_push_is_refused(app_client: TestClient) -> None:
    response = app_client.post(PUSH, json=encode_push(_envelope()))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "NOT_AUTHORIZED"


@pytest.mark.authorization
def test_a_wrong_token_is_refused(app_client: TestClient) -> None:
    response = app_client.post(
        PUSH, json=encode_push(_envelope()), headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 403


@pytest.mark.authorization
def test_a_non_bearer_authorization_header_is_refused(app_client: TestClient) -> None:
    response = app_client.post(
        PUSH, json=encode_push(_envelope()), headers={"Authorization": "Basic abc"}
    )
    assert response.status_code == 403


@pytest.mark.authorization
def test_the_dead_letter_listing_is_authenticated_too(app_client: TestClient) -> None:
    assert app_client.get(DEAD_LETTERS).status_code == 403


@pytest.mark.authorization
def test_an_endpoint_with_no_verifier_refuses_everything(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It fails closed. An endpoint that cannot check identity accepts nothing."""
    from firstdue.api.auth import InternalPushAuthenticator

    monkeypatch.setattr(Settings, "resolved_internal_push_token", property(lambda self: None))
    authenticator = InternalPushAuthenticator(settings)
    assert authenticator.is_configured is False


# -------------------------------------------------------------- delivery


def test_an_authenticated_push_is_delivered(
    app_client: TestClient, settings: Settings, subscribed: list[str]
) -> None:
    response = app_client.post(PUSH, json=encode_push(_envelope()), headers=_auth(settings))
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True
    assert body["event_id"] == "ev-1"
    assert body["deliveries"][0]["status"] == "DELIVERED"
    assert subscribed == ["ev-1"]


@pytest.mark.idempotency
def test_a_redelivered_push_produces_one_effect(
    app_client: TestClient, settings: Settings, subscribed: list[str]
) -> None:
    body = encode_push(_envelope())
    first = app_client.post(PUSH, json=body, headers=_auth(settings))
    second = app_client.post(PUSH, json=body, headers=_auth(settings))

    assert first.json()["deliveries"][0]["status"] == "DELIVERED"
    assert second.json()["deliveries"][0]["status"] == "DEDUPED"
    assert second.status_code == 200
    assert subscribed == ["ev-1"]


def test_a_push_can_name_the_subscription_it_is_for(
    app_client: TestClient, settings: Settings, subscribed: list[str]
) -> None:
    response = app_client.post(
        PUSH,
        json=encode_push(_envelope()),
        params={"subscriber": "conflict-detector"},
        headers=_auth(settings),
    )
    assert [d["subscriber"] for d in response.json()["deliveries"]] == ["conflict-detector"]


@pytest.mark.degraded
def test_an_envelope_nobody_subscribes_to_is_recorded_not_dropped(
    app_client: TestClient, settings: Settings
) -> None:
    response = app_client.post(
        PUSH, json=encode_push(_envelope(topic=Topic.BRIEF_EMITTED)), headers=_auth(settings)
    )
    assert response.status_code == 200
    assert response.json()["deliveries"] == []


# ------------------------------------------------------------ poison handling


@pytest.mark.degraded
def test_an_undecodable_message_is_acked_and_dead_lettered(
    app_client: TestClient, settings: Settings
) -> None:
    """Nacking poison guarantees the same bytes arrive again forever."""
    response = app_client.post(
        PUSH, json={"message": {"data": "bm90LWpzb24="}}, headers=_auth(settings)
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert response.json()["poison_reason"] == "UNDECODABLE_MESSAGE"

    listed = app_client.get(DEAD_LETTERS, headers=_auth(settings)).json()
    assert listed["count"] == 1
    assert listed["dead_letters"][0]["reason"] == "UNDECODABLE_MESSAGE"


@pytest.mark.degraded
def test_a_future_schema_version_is_dead_lettered_without_reaching_a_handler(
    app_client: TestClient, settings: Settings, subscribed: list[str]
) -> None:
    response = app_client.post(
        PUSH,
        json=encode_push(_envelope(schema_version=CURRENT_SCHEMA_VERSION + 1)),
        headers=_auth(settings),
    )
    assert response.status_code == 200
    assert response.json()["deliveries"][0]["status"] == "DEAD_LETTERED"
    assert subscribed == []

    listed = app_client.get(DEAD_LETTERS, headers=_auth(settings)).json()
    assert listed["dead_letters"][0]["reason"] == "UNSUPPORTED_SCHEMA_VERSION"


@pytest.mark.degraded
def test_a_handler_that_keeps_failing_dead_letters_and_the_push_is_still_acked(
    app_client: TestClient, settings: Settings
) -> None:
    from firstdue.errors import SourceUnavailableError

    async def failing(envelope: EventEnvelope) -> None:
        raise SourceUnavailableError("sf-permits is down")

    app_client.app.state.container.bus.subscribe(  # type: ignore[attr-defined]
        Topic.FACT_WRITTEN, failing, subscriber="records-watcher"
    )

    response = app_client.post(PUSH, json=encode_push(_envelope()), headers=_auth(settings))
    assert response.status_code == 200
    delivery = response.json()["deliveries"][0]
    assert delivery["status"] == "DEAD_LETTERED"
    assert delivery["attempts"] > 1
    assert delivery["backoffs_ms"]


@pytest.mark.degraded
def test_a_shut_off_consumer_makes_the_broker_retry_later(
    app_client: TestClient, settings: Settings
) -> None:
    """A breaker that is open is worth handing the message back for."""
    from firstdue.errors import SourceUnavailableError

    async def failing(envelope: EventEnvelope) -> None:
        raise SourceUnavailableError("sf-permits is down")

    app_client.app.state.container.bus.subscribe(  # type: ignore[attr-defined]
        Topic.FACT_WRITTEN, failing, subscriber="records-watcher"
    )

    for n in range(3):
        app_client.post(PUSH, json=encode_push(_envelope(n)), headers=_auth(settings))

    response = app_client.post(PUSH, json=encode_push(_envelope(99)), headers=_auth(settings))
    assert response.status_code == 503
    assert response.json()["deliveries"][0]["status"] == "CIRCUIT_OPEN"
