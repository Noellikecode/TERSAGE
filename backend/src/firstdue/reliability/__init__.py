"""Retry classification, deterministic backoff, and circuit breaking.

Kept out of ``adapters/`` because the in-memory bus, the Pub/Sub push endpoint,
and the source adapters must all fail the same way. A failure policy that lives
in one adapter is a failure policy the other adapters get wrong.
"""

from __future__ import annotations

from firstdue.reliability.breaker import BreakerSnapshot, CircuitBreaker
from firstdue.reliability.retry import (
    DEFAULT_POLICY,
    FailureClass,
    RetryPolicy,
    backoff_ms,
    classify,
)

__all__ = [
    "DEFAULT_POLICY",
    "BreakerSnapshot",
    "CircuitBreaker",
    "FailureClass",
    "RetryPolicy",
    "backoff_ms",
    "classify",
]
