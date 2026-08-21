"""A circuit breaker with no clock of its own.

The breaker exists so a dependency that is down stops being asked, and so the
answer the caller gets is ``UNAVAILABLE`` rather than a silence that reads like
"nothing there". That distinction is the whole reason this class is not simply a
retry loop.

Time arrives as an argument. Nothing here reads a clock, so a breaker's
behaviour over a simulated hour is exactly its behaviour over a real one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.domain.enums import CircuitState

DEFAULT_FAILURE_THRESHOLD: Final[int] = 3
DEFAULT_COOLDOWN: Final[timedelta] = timedelta(seconds=30)


class BreakerSnapshot(BaseModel):
    """The breaker's state, safe to log and to render."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    state: CircuitState
    consecutive_failures: int = Field(ge=0)
    opened_at: datetime | None = None
    open_until: datetime | None = None
    last_success_at: datetime | None = None
    #: Stable error code, never a message.
    last_error_code: str | None = Field(default=None, max_length=80)


class CircuitBreaker:
    """Closed, open, half-open -- with exactly one probe per cooldown."""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown: timedelta = DEFAULT_COOLDOWN,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self._name = name
        self._failure_threshold = failure_threshold
        self._cooldown = cooldown
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: datetime | None = None
        self._open_until: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error_code: str | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> CircuitState:
        return self._state

    def allow(self, now: datetime) -> bool:
        """Whether a call may proceed, promoting to half-open when due.

        Calling this is what moves an open breaker to half-open, so exactly one
        caller per cooldown gets through to probe the dependency.
        """
        if self._state is not CircuitState.OPEN:
            return True
        if self._open_until is not None and now < self._open_until:
            return False
        self._state = CircuitState.HALF_OPEN
        return True

    def record_success(self, now: datetime) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = None
        self._open_until = None
        self._last_error_code = None
        self._last_success_at = now

    def record_failure(self, now: datetime, *, error_code: str) -> bool:
        """Record a failure. Returns True if this failure opened the breaker.

        A failure during a half-open probe re-opens immediately: the probe was
        the test, and it failed.
        """
        self._failures += 1
        self._last_error_code = error_code
        was_open = self._state is CircuitState.OPEN
        if self._state is CircuitState.HALF_OPEN or self._failures >= self._failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = now
            self._open_until = now + self._cooldown
            return not was_open
        return False

    def snapshot(self) -> BreakerSnapshot:
        return BreakerSnapshot(
            name=self._name,
            state=self._state,
            consecutive_failures=self._failures,
            opened_at=self._opened_at,
            open_until=self._open_until,
            last_success_at=self._last_success_at,
            last_error_code=self._last_error_code,
        )

    def reset(self) -> None:
        """Force closed. Used by operators and by the demo reset, never by retry logic."""
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = None
        self._open_until = None
        self._last_error_code = None
