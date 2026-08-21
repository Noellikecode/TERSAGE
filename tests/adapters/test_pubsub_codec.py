"""The Pub/Sub wire format.

Pure functions, so the encoding is provable without credentials, a network, or
the Google libraries. What matters: an envelope survives the round trip exactly,
ordering keys keep one building's events in order, and anything unparseable
raises rather than half-decodes.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from firstdue.domain.events import EventEnvelope, RetryState, Topic
from firstdue.errors import ValidationError
from firstdue.eventing.pubsub_codec import (
    RESERVED_ATTRIBUTE_NAMES,
    attributes_for,
    decode,
    decode_push,
    encode,
    encode_push,
    ordering_key,
    subscription_name,
    topic_name,
)

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _envelope(**overrides: object) -> EventEnvelope:
    payload: dict[str, object] = {
        "event_id": "ev-1",
        "topic": Topic.FACT_WRITTEN,
        "occurred_at": NOW,
        "producer": "records-watcher",
        "producer_version": "1.0.0",
        "correlation_id": "corr-1",
        "causation_id": "ev-0",
        "ids": {"address_id": "sf-0450-hayes", "fact_ids": ("fact-1", "fact-2")},
        "idempotency_key": "idem-key-000001",
    }
    payload.update(overrides)
    return EventEnvelope(**payload)  # type: ignore[arg-type]


def test_an_envelope_survives_the_round_trip_exactly() -> None:
    envelope = _envelope()
    assert decode(encode(envelope).data) == envelope


def test_retry_state_travels_with_the_envelope() -> None:
    envelope = _envelope().with_retry(
        RetryState(attempt=3, max_attempts=5, last_error_code="UPSTREAM_TIMEOUT", backoff_ms=400)
    )
    decoded = decode(encode(envelope).data)
    assert decoded.retry.attempt == 3
    assert decoded.retry.last_error_code == "UPSTREAM_TIMEOUT"


def test_attributes_lift_filterable_fields_out_of_the_payload() -> None:
    attributes = attributes_for(_envelope())
    assert attributes["event_topic"] == "fact.written"
    assert attributes["schema_version"] == "1"
    assert attributes["correlation_id"] == "corr-1"
    assert attributes["causation_id"] == "ev-0"
    assert attributes["attempt"] == "1"


def test_no_attribute_name_collides_with_a_client_parameter() -> None:
    """The publisher takes attributes as keyword arguments, so a name like
    ``topic`` would be swallowed as the client's own parameter and fail on the
    live path only. This is the test that would have caught that."""
    assert not (RESERVED_ATTRIBUTE_NAMES & set(attributes_for(_envelope())))


def test_the_ordering_key_is_the_entity_the_event_concerns() -> None:
    """Pub/Sub orders within a key, so one building's events stay in order."""
    assert ordering_key(_envelope()) == "sf-0450-hayes"
    assert ordering_key(_envelope(ids={"incident_id": "inc-1", "address_id": "a"})) == "inc-1"
    # An event naming no entity still keeps its causal chain ordered.
    assert ordering_key(_envelope(ids={})) == "corr-1"


def test_topic_and_subscription_names_are_derived_from_the_domain_topic() -> None:
    assert topic_name("firstdue", Topic.CONFLICT_DETECTED) == "firstdue-conflict-detected"
    assert subscription_name("firstdue", Topic.FACT_WRITTEN, "survey-ranker") == (
        "firstdue-fact-written-survey-ranker"
    )


def test_a_push_body_round_trips() -> None:
    envelope = _envelope()
    body = encode_push(envelope, subscription="projects/p/subscriptions/s")
    assert decode_push(body) == envelope


@pytest.mark.degraded
def test_a_push_body_without_data_is_refused() -> None:
    with pytest.raises(ValidationError):
        decode_push({"message": {"attributes": {}}})


@pytest.mark.degraded
def test_data_that_is_not_base64_is_refused() -> None:
    with pytest.raises(ValidationError):
        decode_push({"message": {"data": "not base64 !!"}})


@pytest.mark.degraded
def test_data_that_is_not_json_is_refused() -> None:
    body = {"message": {"data": base64.b64encode(b"not json").decode("ascii")}}
    with pytest.raises(ValidationError):
        decode_push(body)


@pytest.mark.degraded
def test_json_that_is_not_an_envelope_is_refused() -> None:
    payload = base64.b64encode(json.dumps({"hello": "world"}).encode()).decode("ascii")
    with pytest.raises(ValidationError):
        decode_push({"message": {"data": payload}})


@pytest.mark.degraded
def test_a_body_that_is_not_a_push_envelope_is_refused() -> None:
    with pytest.raises(ValidationError):
        decode_push({"not": "a push body"})


@pytest.mark.invariant
def test_a_smuggled_payload_is_still_refused_on_the_wire() -> None:
    """The envelope's id-only rule survives serialisation."""
    smuggled = json.dumps(
        {
            "event_id": "ev-1",
            "topic": "fact.written",
            "occurred_at": NOW.isoformat(),
            "producer": "records-watcher",
            "producer_version": "1.0.0",
            "schema_version": 1,
            "correlation_id": "corr-1",
            "ids": {"note": "the permit says two storeys"},
            "idempotency_key": "idem-key-000001",
        }
    )
    body = {"message": {"data": base64.b64encode(smuggled.encode()).decode("ascii")}}
    with pytest.raises(ValidationError):
        decode_push(body)
