"""Failure classification, derived backoff, and circuit breaking."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.domain.enums import CircuitState
from firstdue.errors import (
    ClassificationViolationError,
    ConfigurationError,
    NotAuthorizedError,
    SourceUnavailableError,
    StaleVersionError,
    UpstreamTimeoutError,
    ValidationError,
)
from firstdue.reliability.breaker import CircuitBreaker
from firstdue.reliability.retry import (
    FailureClass,
    RetryPolicy,
    backoff_ms,
    backoff_schedule,
    classify,
    error_code_of,
    is_retryable,
)

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)


@pytest.mark.degraded
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (SourceUnavailableError("down"), FailureClass.TRANSIENT),
        (UpstreamTimeoutError("slow"), FailureClass.TRANSIENT),
        (TimeoutError(), FailureClass.TRANSIENT),
        (ConnectionError(), FailureClass.TRANSIENT),
        (StaleVersionError(expected=1, actual=2, entity="profile"), FailureClass.CONTENDED),
        (ValidationError("malformed"), FailureClass.POISON),
        (NotAuthorizedError("no"), FailureClass.PERMANENT),
        (ClassificationViolationError("phi"), FailureClass.PERMANENT),
        (ConfigurationError("missing"), FailureClass.PERMANENT),
        (RuntimeError("unknown"), FailureClass.TRANSIENT),
    ],
)
def test_failures_are_classified_by_what_retrying_would_achieve(
    error: Exception, expected: FailureClass
) -> None:
    assert classify(error) is expected


@pytest.mark.degraded
def test_only_transient_and_contended_failures_are_retried() -> None:
    assert is_retryable(FailureClass.TRANSIENT)
    assert is_retryable(FailureClass.CONTENDED)
    # Retrying a message that is wrong is how a queue stops moving.
    assert not is_retryable(FailureClass.POISON)
    assert not is_retryable(FailureClass.PERMANENT)


def test_an_error_code_is_stable_and_never_a_message() -> None:
    assert error_code_of(NotAuthorizedError("grant does not carry write:rms")) == "NOT_AUTHORIZED"
    assert error_code_of(RuntimeError("boom")) == "RuntimeError"


def test_the_first_attempt_never_waits() -> None:
    assert backoff_ms(1, seed="ev-1") == 0


def test_backoff_grows_and_is_capped() -> None:
    policy = RetryPolicy(max_attempts=8, base_delay_ms=100, max_delay_ms=1000, jitter_ratio=0.0)
    schedule = backoff_schedule(policy=policy, seed="ev-1")
    assert schedule[0] == 0
    assert schedule[1] == 100
    assert schedule[2] == 200
    assert schedule[3] == 400
    assert max(schedule) <= policy.max_delay_ms
    assert schedule[-1] == policy.max_delay_ms


def test_jitter_is_derived_so_a_replay_waits_the_same_amount() -> None:
    """Derived, not drawn: a NIOSH replay must reproduce the timing it recorded."""
    first = backoff_schedule(seed="ev-42")
    second = backoff_schedule(seed="ev-42")
    assert first == second


def test_jitter_differs_between_events_so_the_fleet_does_not_stampede() -> None:
    a = backoff_schedule(seed="ev-1")
    b = backoff_schedule(seed="ev-2")
    assert a != b
    # Jitter only ever subtracts, so it can never exceed the un-jittered delay.
    plain = backoff_schedule(policy=RetryPolicy(jitter_ratio=0.0), seed="ev-1")
    assert all(j <= p for j, p in zip(a, plain, strict=True))


@pytest.mark.degraded
def test_a_breaker_opens_after_the_threshold_and_refuses_calls() -> None:
    breaker = CircuitBreaker("sf-permits", failure_threshold=3, cooldown=timedelta(seconds=30))
    assert breaker.allow(NOW)

    assert breaker.record_failure(NOW, error_code="UPSTREAM_TIMEOUT") is False
    assert breaker.record_failure(NOW, error_code="UPSTREAM_TIMEOUT") is False
    opened = breaker.record_failure(NOW, error_code="UPSTREAM_TIMEOUT")

    assert opened is True
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow(NOW) is False


@pytest.mark.degraded
def test_after_the_cooldown_exactly_one_probe_is_allowed() -> None:
    breaker = CircuitBreaker("sf-permits", failure_threshold=1, cooldown=timedelta(seconds=30))
    breaker.record_failure(NOW, error_code="UPSTREAM_TIMEOUT")

    later = NOW + timedelta(seconds=31)
    assert breaker.allow(later) is True
    assert breaker.state is CircuitState.HALF_OPEN

    # The probe failed: the breaker re-opens immediately rather than allowing
    # a second probe in the same cooldown.
    breaker.record_failure(later, error_code="UPSTREAM_TIMEOUT")
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow(later) is False


@pytest.mark.degraded
def test_a_successful_probe_closes_the_breaker() -> None:
    breaker = CircuitBreaker("sf-permits", failure_threshold=1, cooldown=timedelta(seconds=30))
    breaker.record_failure(NOW, error_code="UPSTREAM_TIMEOUT")
    later = NOW + timedelta(seconds=31)
    breaker.allow(later)
    breaker.record_success(later)

    snapshot = breaker.snapshot()
    assert snapshot.state is CircuitState.CLOSED
    assert snapshot.consecutive_failures == 0
    assert snapshot.last_success_at == later
    assert snapshot.last_error_code is None
