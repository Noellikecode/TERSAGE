"""Error taxonomy for FIRST DUE.

Every failure the system can express has a stable ``code``. The API layer renders
these into a single error envelope; nothing else in the codebase raises bare
``Exception`` for an expected condition.

Messages are operator-facing and must never contain source internals, bucket
names, record contents, or citizen data -- redaction happens at construction
time, not at the log sink alone.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable, wire-visible error codes."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    STALE_VERSION = "STALE_VERSION"
    APPEND_ONLY_VIOLATION = "APPEND_ONLY_VIOLATION"
    IDEMPOTENCY_MISMATCH = "IDEMPOTENCY_MISMATCH"
    MISSING_IDEMPOTENCY_KEY = "MISSING_IDEMPOTENCY_KEY"
    CLASSIFICATION_VIOLATION = "CLASSIFICATION_VIOLATION"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    GRANT_EXPIRED = "GRANT_EXPIRED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    PROVENANCE_REQUIRED = "PROVENANCE_REQUIRED"
    HUMAN_VERIFICATION_FORBIDDEN = "HUMAN_VERIFICATION_FORBIDDEN"
    BRIEF_NOT_PERSISTED = "BRIEF_NOT_PERSISTED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.STALE_VERSION: 409,
    ErrorCode.APPEND_ONLY_VIOLATION: 409,
    ErrorCode.IDEMPOTENCY_MISMATCH: 409,
    ErrorCode.MISSING_IDEMPOTENCY_KEY: 400,
    ErrorCode.CLASSIFICATION_VIOLATION: 403,
    ErrorCode.NOT_AUTHORIZED: 403,
    ErrorCode.GRANT_EXPIRED: 403,
    ErrorCode.APPROVAL_REQUIRED: 202,
    ErrorCode.PROVENANCE_REQUIRED: 422,
    ErrorCode.HUMAN_VERIFICATION_FORBIDDEN: 422,
    ErrorCode.BRIEF_NOT_PERSISTED: 500,
    ErrorCode.SOURCE_UNAVAILABLE: 503,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
    ErrorCode.CONFIGURATION_ERROR: 500,
    ErrorCode.INTERNAL_ERROR: 500,
}


class FirstDueError(Exception):
    """Base class for every expected failure in the system."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = details or {}

    @property
    def http_status(self) -> int:
        return _STATUS_BY_CODE[self.code]


class ValidationError(FirstDueError):
    code = ErrorCode.VALIDATION_ERROR


class NotFoundError(FirstDueError):
    code = ErrorCode.NOT_FOUND


class StaleVersionError(FirstDueError):
    """Optimistic concurrency failure on a profile write. Renders as HTTP 409."""

    code = ErrorCode.STALE_VERSION

    def __init__(self, *, expected: int, actual: int, entity: str) -> None:
        super().__init__(
            f"{entity} was modified concurrently; expected version {expected}, found {actual}",
            details={"expected_version": expected, "actual_version": actual, "entity": entity},
        )
        self.expected = expected
        self.actual = actual


class WriteContentionError(StaleVersionError):
    """Too many concurrent writers to one document; this one never committed.

    A subclass of :class:`StaleVersionError` on purpose. From the caller's side
    the two are the same event -- somebody else got there first -- and every
    ``except StaleVersionError`` in the codebase should already handle it.

    Kept distinct because the cause differs: a stale write read an old version,
    while this one never got a version at all. Firestore surfaces it as an
    exhausted transaction rather than a version mismatch, and without this the
    client's bare ``ValueError`` would escape as a 500 for what is a 409.
    """

    def __init__(self, *, entity: str, attempts: int) -> None:
        FirstDueError.__init__(
            self,
            f"{entity} is under write contention; the write did not commit "
            f"after {attempts} attempts",
            details={"entity": entity, "attempts": attempts},
        )
        #: Unknown by construction: the transaction never read a committed version.
        self.expected = -1
        self.actual = -1


class AppendOnlyViolationError(FirstDueError):
    """An attempt to mutate, reorder, or gap an append-only sequence."""

    code = ErrorCode.APPEND_ONLY_VIOLATION


class IdempotencyMismatchError(FirstDueError):
    """The same idempotency key was replayed with a different request body."""

    code = ErrorCode.IDEMPOTENCY_MISMATCH


class MissingIdempotencyKeyError(FirstDueError):
    """An external write was attempted without an idempotency key."""

    code = ErrorCode.MISSING_IDEMPOTENCY_KEY


class ClassificationViolationError(FirstDueError):
    """A sensitive fact was routed somewhere its classification forbids."""

    code = ErrorCode.CLASSIFICATION_VIOLATION


class NotAuthorizedError(FirstDueError):
    code = ErrorCode.NOT_AUTHORIZED


class GrantExpiredError(FirstDueError):
    code = ErrorCode.GRANT_EXPIRED


class ProvenanceRequiredError(FirstDueError):
    """A structural fact was constructed without usable provenance."""

    code = ErrorCode.PROVENANCE_REQUIRED


class HumanVerificationForbiddenError(FirstDueError):
    """Something other than a survey tried to set ``human_verified``."""

    code = ErrorCode.HUMAN_VERIFICATION_FORBIDDEN


class BriefNotPersistedError(FirstDueError):
    """A brief emission was handed to a transport before it was written to the log."""

    code = ErrorCode.BRIEF_NOT_PERSISTED


class SourceUnavailableError(FirstDueError):
    """A source is unreachable or its circuit breaker is open.

    Callers must render this as ``UNAVAILABLE`` -- never as an absence of hazard.
    """

    code = ErrorCode.SOURCE_UNAVAILABLE


class UpstreamTimeoutError(FirstDueError):
    code = ErrorCode.UPSTREAM_TIMEOUT


class ConfigurationError(FirstDueError):
    code = ErrorCode.CONFIGURATION_ERROR
