"""The nine metrics a department would actually be judged on.

Each one exists because somebody would ask the question, and the answer should
not require reading a log:

| Metric | The question it answers |
|---|---|
| ``time_to_first_line`` (p50/p95) | How long until the commander sees anything? |
| ``enriched_brief_latency`` | How long until the prose lands, when it lands? |
| ``conflicts_per_1000_structures`` | Is the district getting better or worse? |
| ``queue_precision`` | When we sent a company, did they find what we said? |
| ``referral_acceptance`` | Does the building department act on what we file? |
| ``notification_delivery`` | Did the agency actually get told? |
| ``policy_denials`` | What is the gateway refusing, and is that rising? |
| ``injection_blocks`` | Is somebody trying? |
| ``model_output_rejections`` | Is the model drifting off contract? |

Recorded at the points that already emit the matching audit events, so a metric
and its audit record cannot disagree.

Like tracing, this is **off by default** and costs nothing when off. Percentiles
are computed from a bounded reservoir rather than an unbounded list -- a metric
that leaks memory on a long-running incident would be its own outage.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from firstdue.observability.logging import get_logger

logger = get_logger(__name__)

#: How many samples a latency series keeps. Enough for a stable p95 over a
#: working shift, bounded so a week-long process cannot grow without limit.
RESERVOIR: Final[int] = 2_048

METRIC_TIME_TO_FIRST_LINE: Final[str] = "firstdue.time_to_first_line_ms"
METRIC_ENRICHED_LATENCY: Final[str] = "firstdue.enriched_brief_latency_ms"
METRIC_CONFLICTS_PER_1K: Final[str] = "firstdue.conflicts_per_1000_structures"
METRIC_QUEUE_PRECISION: Final[str] = "firstdue.queue_precision"
METRIC_REFERRAL_ACCEPTANCE: Final[str] = "firstdue.referral_acceptance"
METRIC_NOTIFICATION_DELIVERY: Final[str] = "firstdue.notification_delivery"
METRIC_POLICY_DENIALS: Final[str] = "firstdue.policy_denials"
METRIC_INJECTION_BLOCKS: Final[str] = "firstdue.injection_blocks"
METRIC_MODEL_REJECTIONS: Final[str] = "firstdue.model_output_rejections"


def percentile(samples: list[float], fraction: float) -> float:
    """Nearest-rank percentile. Deterministic, and defined on one sample."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 3)


class MetricsSnapshot(BaseModel):
    """What the console and the smoke test read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    time_to_first_line_p50_ms: float = 0.0
    time_to_first_line_p95_ms: float = 0.0
    enriched_brief_latency_p50_ms: float = 0.0
    enriched_brief_latency_p95_ms: float = 0.0
    conflicts_per_1000_structures: float = 0.0
    #: Surveys that found what the ranking said they would, over surveys done.
    queue_precision: float = 0.0
    #: Referrals the receiving department accepted, over referrals filed.
    referral_acceptance: float = 0.0
    #: Notifications acknowledged, over notifications sent.
    notification_delivery: float = 0.0
    policy_denials: int = Field(default=0, ge=0)
    injection_blocks: int = Field(default=0, ge=0)
    model_output_rejections: int = Field(default=0, ge=0)
    samples: int = Field(default=0, ge=0)


@dataclass(slots=True)
class _Ratio:
    """A numerator over a denominator, reported as zero until it has both."""

    hits: int = 0
    total: int = 0

    def record(self, *, hit: bool) -> None:
        self.total += 1
        if hit:
            self.hits += 1

    @property
    def value(self) -> float:
        return round(self.hits / self.total, 4) if self.total else 0.0


@dataclass(slots=True)
class _Metrics:
    """Process-wide metrics. Cheap when disabled, bounded when enabled."""

    enabled: bool = False
    _meter: Any = None
    time_to_first_line: deque[float] = field(default_factory=lambda: deque(maxlen=RESERVOIR))
    enriched_latency: deque[float] = field(default_factory=lambda: deque(maxlen=RESERVOIR))
    conflicts: int = 0
    structures: int = 0
    queue: _Ratio = field(default_factory=_Ratio)
    referrals: _Ratio = field(default_factory=_Ratio)
    notifications: _Ratio = field(default_factory=_Ratio)
    policy_denials: int = 0
    injection_blocks: int = 0
    model_rejections: int = 0

    # ------------------------------------------------------------- recording

    def record_time_to_first_line(self, ms: float) -> None:
        """The instant brief. The number the 500 ms budget is about."""
        self.time_to_first_line.append(ms)
        self._emit(METRIC_TIME_TO_FIRST_LINE, ms)

    def record_enriched_latency(self, ms: float) -> None:
        self.enriched_latency.append(ms)
        self._emit(METRIC_ENRICHED_LATENCY, ms)

    def record_district(self, *, structures: int, open_conflicts: int) -> None:
        """Set, not incremented: this is a level, and levels are re-measured."""
        self.structures = structures
        self.conflicts = open_conflicts
        self._emit(METRIC_CONFLICTS_PER_1K, self.conflicts_per_1000)

    def record_survey_outcome(self, *, confirmed_the_ranking: bool) -> None:
        self.queue.record(hit=confirmed_the_ranking)
        self._emit(METRIC_QUEUE_PRECISION, self.queue.value)

    def record_referral_outcome(self, *, accepted: bool) -> None:
        self.referrals.record(hit=accepted)
        self._emit(METRIC_REFERRAL_ACCEPTANCE, self.referrals.value)

    def record_notification(self, *, delivered: bool) -> None:
        self.notifications.record(hit=delivered)
        self._emit(METRIC_NOTIFICATION_DELIVERY, self.notifications.value)

    def record_policy_denial(self) -> None:
        self.policy_denials += 1
        self._emit(METRIC_POLICY_DENIALS, self.policy_denials)

    def record_injection_block(self) -> None:
        self.injection_blocks += 1
        self._emit(METRIC_INJECTION_BLOCKS, self.injection_blocks)

    def record_model_rejection(self) -> None:
        self.model_rejections += 1
        self._emit(METRIC_MODEL_REJECTIONS, self.model_rejections)

    # ---------------------------------------------------------------- output

    @property
    def conflicts_per_1000(self) -> float:
        if not self.structures:
            return 0.0
        return round(self.conflicts / self.structures * 1000.0, 3)

    def snapshot(self) -> MetricsSnapshot:
        first = list(self.time_to_first_line)
        enriched = list(self.enriched_latency)
        return MetricsSnapshot(
            time_to_first_line_p50_ms=percentile(first, 0.50),
            time_to_first_line_p95_ms=percentile(first, 0.95),
            enriched_brief_latency_p50_ms=percentile(enriched, 0.50),
            enriched_brief_latency_p95_ms=percentile(enriched, 0.95),
            conflicts_per_1000_structures=self.conflicts_per_1000,
            queue_precision=self.queue.value,
            referral_acceptance=self.referrals.value,
            notification_delivery=self.notifications.value,
            policy_denials=self.policy_denials,
            injection_blocks=self.injection_blocks,
            model_output_rejections=self.model_rejections,
            samples=len(first) + len(enriched),
        )

    def _emit(self, name: str, value: float) -> None:
        if not self.enabled or self._meter is None:  # pragma: no cover - live only
            return
        try:
            self._meter.create_gauge(name).set(value)
        except Exception as exc:
            logger.warning("metric_emit_failed", extra={"error_type": type(exc).__name__})

    def configure(self, *, enabled: bool, service_name: str = "firstdue") -> None:
        """Attach an OTel meter, or stay a plain in-process counter set.

        A metrics backend that will not start must not stop the process. Losing
        a gauge is a degradation; refusing to serve incidents because the
        monitoring exporter is unreachable would be an outage caused by
        monitoring.
        """
        self.enabled = enabled
        if not enabled:
            self._meter = None
            return
        try:  # pragma: no cover - live mode only
            from opentelemetry import metrics as otel_metrics

            self._meter = otel_metrics.get_meter(service_name)
        except Exception as exc:
            self.enabled = False
            self._meter = None
            logger.warning("metrics_unavailable", extra={"error_type": type(exc).__name__})

    def reset(self) -> None:
        self.time_to_first_line.clear()
        self.enriched_latency.clear()
        self.conflicts = 0
        self.structures = 0
        self.queue = _Ratio()
        self.referrals = _Ratio()
        self.notifications = _Ratio()
        self.policy_denials = 0
        self.injection_blocks = 0
        self.model_rejections = 0


#: One set per process.
METRICS: Final[_Metrics] = _Metrics()


def configure_metrics(*, enabled: bool, service_name: str = "firstdue") -> None:
    METRICS.configure(enabled=enabled, service_name=service_name)
