"""Structured JSON logging with redaction on by default.

Every line carries the bound request/correlation/causation identifiers. Every
value passes through :mod:`firstdue.observability.redaction` on the way out, so
a stray ``logger.info("...", extra={"document_text": ...})`` cannot leak a
citizen's record even when someone writes it by accident.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from firstdue.observability.context import current_context
from firstdue.observability.redaction import redact_text, redact_value

#: Attributes the stdlib puts on every record; not part of our structured extra.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Renders a log record as one redacted JSON object."""

    def formatTime(  # noqa: N802 - stdlib logging API
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        """ISO-8601 UTC with milliseconds."""
        return (
            datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        payload.update(current_context())
        # Cloud Logging reads these two to link a line to its trace. Imported
        # here rather than at module scope because tracing logs through this
        # module -- at import time the cycle would be real, at call time it is
        # not. Empty unless tracing is on, so the demo's logs are unchanged.
        from firstdue.observability.tracing import TRACER

        payload.update(TRACER.current_ids())

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = redact_value(key, value)

        if record.exc_info:
            # Type and message only. A traceback can carry record contents.
            exc_type = record.exc_info[0]
            exc_value = record.exc_info[1]
            payload["error_type"] = exc_type.__name__ if exc_type else "Exception"
            payload["error_message"] = redact_text(str(exc_value)) if exc_value else ""

        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Install the root logging configuration. Idempotent."""
    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s :: %(message)s")
        )
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn duplicates access logs through its own handlers; route them here.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
