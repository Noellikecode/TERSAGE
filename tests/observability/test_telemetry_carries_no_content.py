"""A trace is not a place to put a citizen's document.

The fastest way to leak a record is a debugging attribute somebody added at 2am
and never removed. These tests make that a build failure rather than a code
review someone has to remember to do.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from firstdue.observability.metrics import METRICS, percentile
from firstdue.observability.redaction import REDACTED
from firstdue.observability.tracing import TRACER, safe_attribute, span


@pytest.fixture(autouse=True)
def _recording() -> Iterator[None]:
    """Record spans in memory. No collector, no credentials, no exporter."""
    TRACER.configure(enabled=False, record_spans=True)
    TRACER.clear()
    yield
    TRACER.clear()
    TRACER.configure(enabled=False, record_spans=False)


# ------------------------------------------------------------- attributes ---


@pytest.mark.parametrize(
    "key",
    [
        "prompt",
        # A span attribute named for what the model returned is the one most
        # likely to be added while debugging and least likely to be removed.
        "completion",
        "model_output",
        "document_text",
        "password",
        "api_key",
        "token",
        "authorization",
    ],
)
def test_a_sensitive_key_is_replaced_whatever_its_value(key: str) -> None:
    assert safe_attribute(key, "anything at all") == REDACTED


def test_counts_pass_through_untouched() -> None:
    """A usage count is not sensitive, and a redacted number is useless."""
    assert safe_attribute("model.usage.input", 1420) == 1420
    assert safe_attribute("retries", 2) == 2
    assert safe_attribute("model_invoked", False) is False


def test_a_long_string_is_truncated() -> None:
    """Even a permitted attribute cannot become a smuggling channel."""
    value = safe_attribute("note", "x" * 5000)

    assert isinstance(value, str)
    assert len(value) <= 500


def test_a_span_redacts_on_the_way_in_not_on_the_way_out() -> None:
    """The attribute is never *held* in the clear.

    Redacting at export would leave the value in memory, in a crash dump, and
    in whatever a debugger prints. Redacting on ``set`` means the span never
    had it.
    """
    with span("model.invoke", prompt="the building at 450 Hayes has 3 stories") as active:
        pass

    assert active.attributes["prompt"] == REDACTED


def test_the_model_span_carries_shape_not_content() -> None:
    with span(
        "model.invoke",
        model_ref="VertexModelClient",
        verb="extract",
        schema_ref="ExtractionResult",
    ) as active:
        active.set_tokens({"input": 900, "output": 120})
        active.set_retries(2)
        active.set_rejected("schema_invalid")

    assert active.attributes["model.usage.input"] == 900
    assert active.attributes["retries"] == 1
    assert active.attributes["error_code"] == "schema_invalid"
    assert not any("450 Hayes" in str(v) for v in active.attributes.values())


def test_tracing_off_still_yields_a_usable_span() -> None:
    """The no-op has to be cheap enough that call sites do not guard it.

    A call site wrapped in ``if tracing_enabled`` is a call site somebody will
    forget, which is how tracing ends up covering only the happy path.
    """
    TRACER.configure(enabled=False, record_spans=False)

    with span("incident", incident_id="incident_1") as active:
        active.set("cold_start", True)

    assert active.elapsed_ms >= 0.0


# ----------------------------------------------------------------- metrics ---


def test_percentile_is_nearest_rank_and_does_not_interpolate() -> None:
    """A p95 latency that no request actually took is a number, not a fact."""
    samples = [10.0, 20.0, 30.0, 40.0]

    assert percentile(samples, 0.5) in samples
    assert percentile(samples, 0.95) in samples
    assert percentile([], 0.5) == 0.0


def test_every_metric_the_spec_names_is_recordable() -> None:
    METRICS.reset()

    METRICS.record_time_to_first_line(120.0)
    METRICS.record_time_to_first_line(480.0)
    METRICS.record_enriched_latency(1900.0)
    METRICS.record_district(structures=3800, open_conflicts=76)
    METRICS.record_survey_outcome(confirmed_the_ranking=True)
    METRICS.record_survey_outcome(confirmed_the_ranking=False)
    METRICS.record_referral_outcome(accepted=True)
    METRICS.record_notification(delivered=True)
    METRICS.record_policy_denial()
    METRICS.record_injection_block()
    METRICS.record_model_rejection()

    snapshot = METRICS.snapshot()

    assert snapshot.time_to_first_line_p50_ms > 0
    assert snapshot.time_to_first_line_p95_ms >= snapshot.time_to_first_line_p50_ms
    assert snapshot.enriched_brief_latency_p50_ms == 1900.0
    assert snapshot.conflicts_per_1000_structures == pytest.approx(20.0)
    assert snapshot.queue_precision == pytest.approx(0.5)
    assert snapshot.referral_acceptance == 1.0
    assert snapshot.notification_delivery == 1.0
    assert snapshot.policy_denials == 1
    assert snapshot.injection_blocks == 1
    assert snapshot.model_output_rejections == 1

    METRICS.reset()


def test_a_ratio_with_no_samples_is_zero_not_one() -> None:
    """Nothing measured is not the same as everything succeeded.

    A referral acceptance of 1.0 on a system that has never filed a referral
    would be read as a perfect record.
    """
    METRICS.reset()
    snapshot = METRICS.snapshot()

    assert snapshot.referral_acceptance == 0.0
    assert snapshot.queue_precision == 0.0
    assert snapshot.notification_delivery == 0.0
