"""Request and causal context.

Three identifiers travel with every unit of work:

* ``request_id`` -- this HTTP request or worker invocation;
* ``correlation_id`` -- the whole causal chain, from a CAD dispatch through the
  brief, the notifications, and the log entries;
* ``causation_id`` -- the immediate parent event.

They are context variables so nothing has to thread them through every call, and
they land on every log line, audit record, and error envelope.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

_request_id: ContextVar[str | None] = ContextVar("firstdue_request_id", default=None)
_correlation_id: ContextVar[str | None] = ContextVar("firstdue_correlation_id", default=None)
_causation_id: ContextVar[str | None] = ContextVar("firstdue_causation_id", default=None)


@dataclass(slots=True)
class ContextTokens:
    request: Token[str | None]
    correlation: Token[str | None]
    causation: Token[str | None]


def bind_context(
    *,
    request_id: str | None = None,
    correlation_id: str | None = None,
    causation_id: str | None = None,
) -> ContextTokens:
    """Bind identifiers for the current context; returns tokens for reset."""
    return ContextTokens(
        request=_request_id.set(request_id),
        correlation=_correlation_id.set(correlation_id),
        causation=_causation_id.set(causation_id),
    )


def reset_context(tokens: ContextTokens) -> None:
    _request_id.reset(tokens.request)
    _correlation_id.reset(tokens.correlation)
    _causation_id.reset(tokens.causation)


def get_request_id() -> str | None:
    return _request_id.get()


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def get_causation_id() -> str | None:
    return _causation_id.get()


def current_context() -> dict[str, str]:
    """The bound identifiers, omitting any that are unset."""
    values = {
        "request_id": _request_id.get(),
        "correlation_id": _correlation_id.get(),
        "causation_id": _causation_id.get(),
    }
    return {k: v for k, v in values.items() if v is not None}
