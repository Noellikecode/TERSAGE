"""The versioned envelope: identifiers, schema version, and retry state."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from firstdue.domain.events import (
    CURRENT_SCHEMA_VERSION,
    EventEnvelope,
    RetryState,
    Topic,
)
from firstdue.errors import ValidationError

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


def _envelope(**overrides: object) -> EventEnvelope:
    payload: dict[str, object] = {
        "event_id": "ev-1",
        "topic": Topic.FACT_WRITTEN,
        "occurred_at": NOW,
        "producer": "records-watcher",
        "producer_version": "1.0.0",
        "correlation_id": "corr-1",
        "ids": {"address_id": "sf-0450-hayes"},
        "idempotency_key": "idem-key-000001",
    }
    payload.update(overrides)
    return EventEnvelope(**payload)  # type: ignore[arg-type]


def test_a_fresh_envelope_is_on_its_first_attempt() -> None:
    envelope = _envelope()
    assert envelope.schema_version == CURRENT_SCHEMA_VERSION
    assert envelope.retry.attempt == 1
    assert envelope.retry.is_first
    assert not envelope.retry.is_final
    assert envelope.retry.backoff_ms == 0


def test_retry_state_advances_and_carries_the_first_attempt_time() -> None:
    state = RetryState(attempt=1, max_attempts=3)
    second = state.next_attempt(
        error_code="UPSTREAM_TIMEOUT", backoff_ms=250, first_attempted_at=NOW
    )
    third = second.next_attempt(
        error_code="UPSTREAM_TIMEOUT", backoff_ms=500, first_attempted_at=NOW
    )

    assert (second.attempt, third.attempt) == (2, 3)
    assert third.first_attempted_at == NOW
    assert third.last_error_code == "UPSTREAM_TIMEOUT"
    assert third.is_final
    assert third.remaining == 0


def test_exhausted_retries_cannot_advance_again() -> None:
    """Exhaustion is a dead letter, not another attempt."""
    state = RetryState(attempt=2, max_attempts=2)
    with pytest.raises(ValidationError):
        state.next_attempt(error_code="X", backoff_ms=1, first_attempted_at=NOW)


def test_an_attempt_beyond_the_budget_cannot_be_constructed() -> None:
    with pytest.raises(ValidationError):
        RetryState(attempt=4, max_attempts=3)


@pytest.mark.invariant
def test_a_future_schema_version_is_not_supported() -> None:
    """A newer envelope is refused rather than half-understood."""
    envelope = _envelope(schema_version=CURRENT_SCHEMA_VERSION + 1)
    assert not envelope.is_supported_schema
    assert _envelope().is_supported_schema


@pytest.mark.idempotency
def test_the_dedupe_key_is_scoped_per_subscriber() -> None:
    """Deduping globally would starve the second consumer of a topic."""
    envelope = _envelope()
    assert envelope.dedupe_key("conflict-detector") != envelope.dedupe_key("survey-ranker")
    assert envelope.idempotency_key in envelope.dedupe_key("conflict-detector")


@pytest.mark.invariant
def test_an_envelope_still_refuses_a_payload() -> None:
    with pytest.raises(ValidationError):
        _envelope(ids={"note": "the permit says two storeys"})


def test_a_caused_envelope_inherits_the_causal_chain_and_resets_retries() -> None:
    parent = _envelope().with_retry(RetryState(attempt=3, max_attempts=5))
    child = parent.caused(
        event_id="ev-2",
        topic=Topic.CONFLICT_DETECTED,
        occurred_at=NOW,
        producer="conflict-detector",
        producer_version="1.0.0",
        ids={"address_id": "sf-0450-hayes"},
        idempotency_key="idem-key-000002",
    )
    assert child.correlation_id == parent.correlation_id
    assert child.causation_id == parent.event_id
    # A new event is a new delivery: the parent's attempt count is not its problem.
    assert child.retry.attempt == 1
