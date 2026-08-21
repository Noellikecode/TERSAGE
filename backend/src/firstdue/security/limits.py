"""Request limits: body size and rate.

Both exist for the same reason, and it is not abuse from strangers -- the API is
authenticated. It is that a misconfigured retry loop, a runaway scheduler, or a
single caller with a large upload can take the incident loop down, and the
incident loop is the part that has to answer in 500 ms.

The limiter is a token bucket per caller, driven by the injected clock, so it
behaves identically over a simulated hour and a real one. Rejection is ``429``
with ``Retry-After``: a client that is told when to come back does, and one that
is only told "no" retries immediately.

Health endpoints are exempt. A rate-limited ``/readyz`` makes a load balancer
pull a healthy instance out of rotation during exactly the traffic spike the
limit was meant to survive.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from firstdue.errors import ErrorCode
from firstdue.observability.logging import get_logger
from firstdue.ports.clock import Clock

logger = get_logger(__name__)

#: An id-only event envelope is a few hundred bytes; a pre-plan is a few tens of
#: kilobytes. A megabyte is generous and still bounds what one request can cost.
DEFAULT_MAX_BODY_BYTES: Final[int] = 1_048_576
#: Paths a limiter must never touch, or it takes the instance out of rotation.
EXEMPT_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/readyz"})


class TokenBucketLimiter:
    """Per-caller token bucket with no clock of its own."""

    def __init__(self, *, rate_per_second: float, burst: int) -> None:
        if rate_per_second <= 0 or burst < 1:
            raise ValueError("rate_per_second must be positive and burst at least 1")
        self._rate = rate_per_second
        self._burst = float(burst)
        self._tokens: dict[str, float] = {}
        self._last: dict[str, datetime] = {}

    def check(self, key: str, now: datetime) -> float:
        """Consume a token. Returns seconds to wait, or 0.0 to proceed."""
        tokens = self._tokens.get(key, self._burst)
        last = self._last.get(key)
        if last is not None:
            tokens = min(self._burst, tokens + max(0.0, (now - last).total_seconds()) * self._rate)
        self._last[key] = now

        if tokens >= 1.0:
            self._tokens[key] = tokens - 1.0
            return 0.0
        self._tokens[key] = tokens
        return (1.0 - tokens) / self._rate

    def reset(self) -> None:
        self._tokens.clear()
        self._last.clear()


class RequestLimitsMiddleware(BaseHTTPMiddleware):
    """Bounds request size and rate, in that order.

    Size first: a body too large is refused before it is read into memory, which
    is the point of checking it at all.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        clock: Clock,
        limiter: TokenBucketLimiter,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        super().__init__(app)
        self._clock = clock
        self._limiter = limiter
        self._max_body_bytes = max_body_bytes

    @staticmethod
    def _caller_key(request: Request) -> str:
        """Who to charge. The authenticated caller, or the peer address.

        Keyed on the bearer token's fingerprint rather than the token, so the
        limiter's state can never be a place credentials are stored.
        """
        header = request.headers.get("Authorization", "")
        if header:
            import hashlib

            return hashlib.sha256(header.encode("utf-8")).hexdigest()[:16]
        client = request.client
        return client.host if client else "anonymous"

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                length = 0
            if length > self._max_body_bytes:
                logger.warning(
                    "request_too_large",
                    extra={"path": request.url.path, "declared_bytes": length},
                )
                return self._error(
                    413,
                    ErrorCode.VALIDATION_ERROR,
                    "request body exceeds the maximum accepted size",
                    {"max_bytes": str(self._max_body_bytes)},
                )

        wait = self._limiter.check(self._caller_key(request), self._clock.now())
        if wait > 0.0:
            logger.warning("rate_limited", extra={"path": request.url.path})
            response = self._error(
                429,
                ErrorCode.VALIDATION_ERROR,
                "too many requests; retry after the interval given",
                {"retry_after_seconds": f"{wait:.3f}"},
            )
            response.headers["Retry-After"] = str(max(1, int(wait + 0.999)))
            return response

        return await call_next(request)

    @staticmethod
    def _error(
        status_code: int, code: ErrorCode, message: str, details: dict[str, str]
    ) -> JSONResponse:
        """The same envelope every other failure uses."""
        from firstdue.api.errors import build_envelope

        return JSONResponse(status_code=status_code, content=build_envelope(code, message, details))
