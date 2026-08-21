"""One error envelope for the whole API.

Every failure -- domain error, validation error, unhandled exception -- leaves
the process in the same shape, carrying the request and correlation identifiers
so an operator can find the matching log line and audit chain.

Unhandled exceptions never leak their message. Source internals, bucket names,
and record contents stay out of user-facing text by construction.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from firstdue.errors import ErrorCode, FirstDueError
from firstdue.observability.context import get_correlation_id, get_request_id
from firstdue.observability.logging import get_logger
from firstdue.observability.redaction import redact_mapping, redact_text

logger = get_logger(__name__)


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None
    correlation_id: str | None = None


class ErrorEnvelope(BaseModel):
    """The only error shape this API emits."""

    error: ErrorBody


def build_envelope(
    code: ErrorCode, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=redact_text(message),
            details=redact_mapping(details or {}),
            request_id=get_request_id(),
            correlation_id=get_correlation_id(),
        )
    )
    return envelope.model_dump(mode="json")


def install_error_handlers(app: FastAPI) -> None:
    """Register handlers so no code path returns a non-enveloped error."""

    @app.exception_handler(FirstDueError)
    async def _domain_error(request: Request, exc: FirstDueError) -> JSONResponse:
        logger.warning(
            "domain_error",
            extra={"error_code": str(exc.code), "path": request.url.path},
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=build_envelope(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted({".".join(str(p) for p in err["loc"][1:]) for err in exc.errors()})
        return JSONResponse(
            status_code=422,
            content=build_envelope(
                ErrorCode.VALIDATION_ERROR,
                "request failed validation",
                {"fields": fields},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.VALIDATION_ERROR
        if exc.status_code >= 500:
            code = ErrorCode.INTERNAL_ERROR
        return JSONResponse(
            status_code=exc.status_code,
            content=build_envelope(code, str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the type for triage; never return the message to the caller.
        logger.error(
            "unhandled_error",
            extra={"path": request.url.path, "error_type": type(exc).__name__},
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content=build_envelope(
                ErrorCode.INTERNAL_ERROR,
                "an internal error occurred; the incident was logged",
            ),
        )
