"""Retry classification and deterministic jittered backoff.

Two decisions live here, and only here.

**What is worth retrying.** A timeout is worth retrying; a validation error is
not. Retrying a permanently-malformed message forever is how a queue stops
moving, so :class:`FailureClass` splits failures into four outcomes and the
dispatcher acts on the class rather than on the exception type.

**How long to wait.** Exponential backoff with jitter, because a fleet that
retries in lockstep is a fleet that stampedes its own recovering dependency.
The jitter is **derived, not random**: it is a hash of the envelope id and the
attempt number, so a replayed delivery produces the same schedule and a NIOSH
replay two years later reproduces the timing it recorded. Nothing here reads a
clock or a random number generator.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.errors import (
    AppendOnlyViolationError,
    ClassificationViolationError,
    ConfigurationError,
    FirstDueError,
    GrantExpiredError,
    IdempotencyMismatchError,
    MissingIdempotencyKeyError,
    NotAuthorizedError,
    NotFoundError,
    ProvenanceRequiredError,
    SourceUnavailableError,
    StaleVersionError,
    UpstreamTimeoutError,
    ValidationError,
)


class FailureClass(StrEnum):
    """What kind of failure a handler raised, and therefore what to do next."""

    #: Will plausibly succeed later. Retry with backoff.
    TRANSIENT = "TRANSIENT"
    #: Contention, not breakage. Retry, and do not count against the breaker.
    CONTENDED = "CONTENDED"
    #: Correct to refuse, and refusing again changes nothing. Dead-letter now.
    PERMANENT = "PERMANENT"
    #: The message itself is unprocessable. Dead-letter now, never redeliver.
    POISON = "POISON"


#: Failures that mean "the message is wrong", not "the world is busy".
_POISON: Final[tuple[type[Exception], ...]] = (
    ValidationError,
    ProvenanceRequiredError,
    MissingIdempotencyKeyError,
    AppendOnlyViolationError,
    IdempotencyMismatchError,
)

#: Failures that are correct refusals. Retrying re-refuses.
_PERMANENT: Final[tuple[type[Exception], ...]] = (
    NotAuthorizedError,
    GrantExpiredError,
    ClassificationViolationError,
    ConfigurationError,
    NotFoundError,
)

#: Failures that mean another writer got there first.
_CONTENDED: Final[tuple[type[Exception], ...]] = (StaleVersionError,)

#: Failures that mean the dependency is unwell.
_TRANSIENT: Final[tuple[type[Exception], ...]] = (
    SourceUnavailableError,
    UpstreamTimeoutError,
    TimeoutError,
    ConnectionError,
    OSError,
)


def classify(exc: BaseException) -> FailureClass:
    """Classify a handler failure.

    An exception this module has never seen is treated as ``TRANSIENT``: the
    conservative default is to try again and then dead-letter, because giving up
    immediately on an unrecognised failure loses work that might have succeeded.
    Dead-lettering still happens -- it just happens after the retries.
    """
    # Checked before the generic FirstDueError branches: StaleVersionError is
    # contention and must not be mistaken for a permanent refusal.
    if isinstance(exc, _CONTENDED):
        return FailureClass.CONTENDED
    if isinstance(exc, _POISON):
        return FailureClass.POISON
    if isinstance(exc, _PERMANENT):
        return FailureClass.PERMANENT
    if isinstance(exc, _TRANSIENT):
        return FailureClass.TRANSIENT
    if isinstance(exc, FirstDueError):
        return FailureClass.TRANSIENT
    return FailureClass.TRANSIENT


def is_retryable(failure: FailureClass) -> bool:
    return failure in (FailureClass.TRANSIENT, FailureClass.CONTENDED)


def error_code_of(exc: BaseException) -> str:
    """A stable, redacted code for an exception. Never its message."""
    if isinstance(exc, FirstDueError):
        return str(exc.code)
    return type(exc).__name__


class RetryPolicy(BaseModel):
    """How many times, how long, and how much spread."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=5, ge=1, le=100)
    base_delay_ms: int = Field(default=250, ge=1, le=600_000)
    max_delay_ms: int = Field(default=60_000, ge=1, le=3_600_000)
    multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    #: Fraction of the computed delay that jitter may subtract. 0.0 disables it.
    jitter_ratio: float = Field(default=0.25, ge=0.0, le=1.0)


DEFAULT_POLICY: Final[RetryPolicy] = RetryPolicy()


def _jitter_fraction(seed: str) -> float:
    """A stable float in ``[0.0, 1.0)`` derived from ``seed``.

    Derived rather than drawn so two processes replaying the same envelope wait
    the same amount, and so a test can assert the schedule instead of tolerating
    it.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / float(1 << 32)


def backoff_ms(attempt: int, *, policy: RetryPolicy = DEFAULT_POLICY, seed: str = "") -> int:
    """Delay before ``attempt`` (1-based), capped and jittered.

    Attempt 1 is the first delivery and always waits zero.
    """
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    if attempt == 1:
        return 0
    raw = policy.base_delay_ms * (policy.multiplier ** (attempt - 2))
    capped = min(float(policy.max_delay_ms), raw)
    if policy.jitter_ratio == 0.0:
        return int(capped)
    # Subtractive jitter: never longer than the cap, never zero-length.
    spread = capped * policy.jitter_ratio * _jitter_fraction(f"{seed}:{attempt}")
    return max(1, int(capped - spread))


def backoff_schedule(*, policy: RetryPolicy = DEFAULT_POLICY, seed: str = "") -> tuple[int, ...]:
    """Every delay the policy will impose, in order. Used by tests and docs."""
    return tuple(backoff_ms(n, policy=policy, seed=seed) for n in range(1, policy.max_attempts + 1))
