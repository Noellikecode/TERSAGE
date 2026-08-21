"""Request context and access logging middleware."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from firstdue.observability.context import bind_context, reset_context
from firstdue.observability.logging import get_logger

REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"
CAUSATION_ID_HEADER = "X-Causation-ID"

logger = get_logger("firstdue.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Binds request/correlation/causation ids and logs one line per request.

    An inbound ``X-Correlation-ID`` is honoured so a CAD dispatch, the brief it
    produces, and every downstream notification share one causal chain.
    """

    def __init__(self, app: Callable[..., Awaitable[None]], *, id_prefix: str = "req") -> None:
        super().__init__(app)
        self._id_prefix = id_prefix

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        ids = request.app.state.container.ids
        request_id = request.headers.get(REQUEST_ID_HEADER) or ids.new_id(self._id_prefix)
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or request_id
        causation_id = request.headers.get(CAUSATION_ID_HEADER)

        tokens = bind_context(
            request_id=request_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            reset_context(tokens)

        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[CORRELATION_ID_HEADER] = correlation_id

        # Re-bind briefly so the access line carries the ids.
        tokens = bind_context(request_id=request_id, correlation_id=correlation_id)
        logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 3),
            },
        )
        reset_context(tokens)
        return response
