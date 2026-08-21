"""OpenTelemetry tracing, off by default.

Six span names, fixed, because a dashboard that has to guess at span names is a
dashboard nobody maintains:

``incident`` · ``agent.{name}`` · ``gateway.policy_decision`` ·
``source.query`` · ``source.write`` · ``model.invoke``

Two properties matter more than the exporter:

**Off costs nothing.** With ``OTEL_ENABLED=false`` -- the default, and what fake
mode uses -- every helper here is a context manager that does nothing. The test
suite needs no collector and the credential-free demo stays credential-free.

**Spans carry counts and identifiers, never content.** A span attribute is
passed through the same redaction the logs use, and a test fails the build if a
value matching a sensitive pattern reaches one. A trace is not a place to put a
citizen's document, and the fastest way to leak one is a debugging attribute
somebody added at 2am.

The ``correlation_id`` already threaded through
:mod:`firstdue.observability.context` becomes a span attribute, so a trace, a
log line, and an audit record all join on one id.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from firstdue.observability.context import get_causation_id, get_correlation_id, get_request_id
from firstdue.observability.logging import get_logger
from firstdue.observability.redaction import REDACTED, is_sensitive_key, redact_text

logger = get_logger(__name__)

SPAN_INCIDENT: Final[str] = "incident"
SPAN_GATEWAY: Final[str] = "gateway.policy_decision"
SPAN_SOURCE_QUERY: Final[str] = "source.query"
SPAN_SOURCE_WRITE: Final[str] = "source.write"
SPAN_MODEL_INVOKE: Final[str] = "model.invoke"


def agent_span_name(agent_id: str) -> str:
    """``agent.records-watcher``, and so on."""
    return f"agent.{agent_id}"


#: Attribute values are redacted the same way log values are. The check is on
#: the way in, so a span that never held a document cannot leak one later.
def safe_attribute(key: str, value: Any) -> str | int | float | bool:
    """Redact one span attribute.

    Numbers and booleans pass through -- a token count is not sensitive. Strings
    go through the value-pattern redactor, and a sensitive *key* is replaced
    outright whatever its value.
    """
    if is_sensitive_key(key):
        return REDACTED
    if isinstance(value, bool | int | float):
        return value
    return redact_text(str(value))[:500]


@dataclass(slots=True)
class Span:
    """A span, or a convincing impression of one when tracing is off.

    The no-op case is the common one. It has to be cheap enough that the call
    sites do not grow an ``if tracing_enabled`` around every one of them, which
    is how tracing ends up covering only the paths somebody remembered.
    """

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)
    _otel: Any = None

    def set(self, key: str, value: Any) -> None:
        redacted = safe_attribute(key, value)
        self.attributes[key] = redacted
        if self._otel is not None:  # pragma: no cover - live mode only
            self._otel.set_attribute(key, redacted)

    def set_many(self, values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            self.set(key, value)

    def set_tokens(self, usage: Mapping[str, int]) -> None:
        """Token counts. Counts, never the text they counted.

        Named ``model.usage.*`` rather than ``model.tokens.*`` because "token"
        is a sensitive key and the redactor is right to blank it. Renaming the
        attribute is better than carving an exception into the redactor --
        an exception is the thing the next person widens.
        """
        for kind, count in usage.items():
            self.set(f"model.usage.{kind}", int(count))

    def set_retries(self, attempts: int) -> None:
        self.set("retries", max(0, attempts - 1))

    def set_rejected(self, error_code: str) -> None:
        """A stable code, never a message and never the output that was rejected."""
        self.set("rejected", True)
        self.set("error_code", error_code)

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000.0


class _Tracer:
    """Owns whether tracing is on, and the exporter if it is."""

    def __init__(self) -> None:
        self._otel_tracer: Any = None
        self._enabled = False
        self.spans: list[Span] = []
        #: Tests read this instead of standing up a collector.
        self.record_spans = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def configure(
        self,
        *,
        enabled: bool,
        service_name: str = "firstdue",
        project_id: str | None = None,
        record_spans: bool = False,
    ) -> None:
        """Turn tracing on. Called once, from the composition root.

        A failure to build the exporter disables tracing and logs it. Losing
        traces is a degradation; failing to start because the telemetry backend
        is unreachable would be an outage caused by the thing watching for
        outages.
        """
        self.record_spans = record_spans
        self._enabled = enabled
        if not enabled:
            self._otel_tracer = None
            return

        try:  # pragma: no cover - live mode only
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            resource = Resource.create({"service.name": service_name})
            provider = TracerProvider(resource=resource)

            if project_id:
                from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

                # The exporter ships without type information, so a strict
                # build sees an untyped call. Narrowed here rather than
                # globally: the rest of this module stays checked.
                exporter = CloudTraceSpanExporter(project_id=project_id)  # type: ignore[no-untyped-call]
                provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            self._otel_tracer = trace.get_tracer(service_name)
            logger.info(
                "tracing_enabled", extra={"exporter": "cloud-trace" if project_id else "none"}
            )
        except Exception as exc:
            self._enabled = False
            self._otel_tracer = None
            logger.warning("tracing_unavailable", extra={"error_type": type(exc).__name__})

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        span = Span(name=name)
        # The ids that join a trace to a log line and an audit record.
        for key, value in (
            ("correlation_id", get_correlation_id()),
            ("causation_id", get_causation_id()),
            ("request_id", get_request_id()),
        ):
            if value:
                span.set(key, value)
        span.set_many(attributes)

        if self._otel_tracer is None:
            try:
                yield span
            finally:
                if self.record_spans:
                    self.spans.append(span)
            return

        with self._otel_tracer.start_as_current_span(name) as otel:  # pragma: no cover
            span._otel = otel
            for key, value in span.attributes.items():
                otel.set_attribute(key, value)
            try:
                yield span
            finally:
                if self.record_spans:
                    self.spans.append(span)

    def current_ids(self) -> dict[str, str]:
        """The trace and span ids of whatever span is running, if any.

        Cloud Logging joins a log line to a trace by two specific fields. Without
        them, a log line and a span about the same incident sit in two consoles
        with no way to put them side by side -- which is exactly what someone
        needs at the moment they are asking what the system told a commander.
        """
        if self._otel_tracer is None:
            return {}
        try:  # pragma: no cover - live mode only
            from opentelemetry import trace

            context = trace.get_current_span().get_span_context()
            if not context.is_valid:
                return {}
            return {
                "trace": f"{context.trace_id:032x}",
                "span_id": f"{context.span_id:016x}",
            }
        except Exception:
            return {}

    def clear(self) -> None:
        self.spans.clear()


#: One tracer per process, configured at startup.
TRACER: Final[_Tracer] = _Tracer()


def configure_tracing(
    *,
    enabled: bool,
    service_name: str = "firstdue",
    project_id: str | None = None,
    record_spans: bool = False,
) -> None:
    TRACER.configure(
        enabled=enabled,
        service_name=service_name,
        project_id=project_id,
        record_spans=record_spans,
    )


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    with TRACER.span(name, **attributes) as active:
        yield active


@contextmanager
def incident_span(*, incident_id: str, address_id: str, **attributes: Any) -> Iterator[Span]:
    with TRACER.span(
        SPAN_INCIDENT, incident_id=incident_id, address_id=address_id, **attributes
    ) as active:
        yield active


@contextmanager
def agent_span(agent_id: str, *, agent_version: str, **attributes: Any) -> Iterator[Span]:
    with TRACER.span(
        agent_span_name(agent_id), agent_version=agent_version, **attributes
    ) as active:
        yield active


@contextmanager
def policy_span(*, agent_id: str, target: str, **attributes: Any) -> Iterator[Span]:
    with TRACER.span(SPAN_GATEWAY, agent_id=agent_id, target=target, **attributes) as active:
        yield active


@contextmanager
def source_query_span(*, source_id: str, **attributes: Any) -> Iterator[Span]:
    with TRACER.span(SPAN_SOURCE_QUERY, source_id=source_id, **attributes) as active:
        yield active


@contextmanager
def source_write_span(*, target: str, **attributes: Any) -> Iterator[Span]:
    with TRACER.span(SPAN_SOURCE_WRITE, target=target, **attributes) as active:
        yield active


@contextmanager
def model_invoke_span(
    *, model_ref: str, verb: str, schema_ref: str, **attributes: Any
) -> Iterator[Span]:
    """The one span that must never carry what it was given.

    Model id, verb, schema ref, token counts, latency, retries. Not the prompt,
    not the completion, not the document.
    """
    with TRACER.span(
        SPAN_MODEL_INVOKE,
        model_ref=model_ref,
        verb=verb,
        schema_ref=schema_ref,
        **attributes,
    ) as active:
        yield active
