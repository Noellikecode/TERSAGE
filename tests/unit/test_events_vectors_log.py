"""Events carry ids; sensitive facts never reach vectors; logs are append-only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from firstdue.domain.enums import Classification, LogEntryType
from firstdue.domain.events import EventEnvelope, Topic
from firstdue.domain.logentries import AppendOnlyLog, IncidentLogEntry
from firstdue.domain.vectors import VectorPayload, build_vector_payload
from firstdue.errors import (
    AppendOnlyViolationError,
    ClassificationViolationError,
    ValidationError,
)

NOW = datetime(2026, 8, 20, 8, 0, tzinfo=UTC)
pytestmark = pytest.mark.invariant


def _envelope(**overrides) -> EventEnvelope:
    payload = {
        "event_id": "ev-1",
        "topic": Topic.FACT_WRITTEN,
        "occurred_at": NOW,
        "producer": "records-watcher",
        "producer_version": "2.4.1",
        "correlation_id": "corr-1",
        "ids": {"address_id": "sf-0450-hayes", "fact_ids": ("fact_a", "fact_b")},
        "idempotency_key": "abcdef1234567890",
    }
    payload.update(overrides)
    return EventEnvelope(**payload)  # type: ignore[arg-type]


def test_envelope_accepts_identifiers() -> None:
    envelope = _envelope()
    assert envelope.ids["address_id"] == "sf-0450-hayes"


def test_envelope_refuses_a_prose_payload() -> None:
    """An event that carried record text would let a consumer skip re-reading
    state, and a replay would then produce a different result."""
    with pytest.raises(ValidationError):
        _envelope(ids={"narrative": "Stairwell partially obstructed, storage removed"})


def test_envelope_refuses_prose_inside_a_list() -> None:
    with pytest.raises(ValidationError):
        _envelope(ids={"notes": ("fact_a", "two storey wood frame")})


def test_envelope_bounds_the_number_of_ids() -> None:
    with pytest.raises(ValidationError):
        _envelope(ids={f"k{i}": f"v{i}" for i in range(25)})


def test_envelope_requires_an_idempotency_key() -> None:
    with pytest.raises(Exception):  # noqa: B017 - min_length on IdempotencyKey
        _envelope(idempotency_key="short")


def test_caused_inherits_the_causal_chain() -> None:
    parent = _envelope()
    child = parent.caused(
        event_id="ev-2",
        topic=Topic.CONFLICT_DETECTED,
        occurred_at=NOW,
        producer="conflict-engine",
        producer_version="1.0.0",
        ids={"conflict_id": "conflict_1"},
        idempotency_key="fedcba0987654321",
    )
    assert child.correlation_id == parent.correlation_id
    assert child.causation_id == parent.event_id


# ---------------------------------------------------------------- vectors ---


def test_phi_can_never_be_embedded(make_fact) -> None:
    fact = make_fact(classification=Classification.PHI)
    with pytest.raises(ClassificationViolationError):
        build_vector_payload(fact, payload_id="vp-1", text="prior EMS run")


def test_tier_ii_can_never_be_embedded(make_fact) -> None:
    fact = make_fact(classification=Classification.TIER_II_CONFIDENTIAL)
    with pytest.raises(ClassificationViolationError):
        build_vector_payload(fact, payload_id="vp-2", text="chlorine, north mezzanine")


def test_public_facts_may_be_embedded(make_fact) -> None:
    fact = make_fact(classification=Classification.PUBLIC)
    payload = build_vector_payload(fact, payload_id="vp-3", text="two storey wood frame")
    assert isinstance(payload, VectorPayload)
    assert payload.source_ref == fact.source_ref


def test_restricted_facts_may_be_embedded(make_fact) -> None:
    fact = make_fact(classification=Classification.RESTRICTED)
    assert build_vector_payload(fact, payload_id="vp-4", text="inspection note").payload_id


# ------------------------------------------------------------ incident log --


def _entry(sequence: int, **overrides) -> IncidentLogEntry:
    payload = {
        "entry_id": f"le-{sequence}",
        "incident_id": "inc-1",
        "sequence": sequence,
        "entry_type": LogEntryType.BRIEF_EMITTED,
        "occurred_at": NOW + timedelta(seconds=sequence),
        "profile_snapshot_id": "snap-1",
        "content": {"version": sequence + 1},
    }
    payload.update(overrides)
    return IncidentLogEntry(**payload)  # type: ignore[arg-type]


def test_log_appends_in_sequence() -> None:
    log = AppendOnlyLog(incident_id="inc-1").append(_entry(0)).append(_entry(1))
    assert [e.sequence for e in log.entries] == [0, 1]


def test_log_refuses_a_gap() -> None:
    log = AppendOnlyLog(incident_id="inc-1").append(_entry(0))
    with pytest.raises(AppendOnlyViolationError):
        log.append(_entry(2))


def test_log_refuses_a_rewrite() -> None:
    log = AppendOnlyLog(incident_id="inc-1").append(_entry(0))
    with pytest.raises(AppendOnlyViolationError):
        log.append(_entry(0, entry_id="le-other"))


def test_log_refuses_a_foreign_entry() -> None:
    with pytest.raises(ValidationError):
        AppendOnlyLog(incident_id="inc-1", entries=(_entry(0, incident_id="inc-2"),))


def test_sealed_log_accepts_nothing_further() -> None:
    log = AppendOnlyLog(incident_id="inc-1").append(_entry(0)).seal(at=NOW)
    with pytest.raises(AppendOnlyViolationError):
        log.append(_entry(1))


def test_sealing_twice_keeps_the_first_seal() -> None:
    log = AppendOnlyLog(incident_id="inc-1").seal(at=NOW)
    again = log.seal(at=NOW + timedelta(hours=1))
    assert again.sealed_at == NOW


def test_entries_are_content_hashed_on_append() -> None:
    log = AppendOnlyLog(incident_id="inc-1").append(_entry(0))
    assert len(log.entries[0].content_hash) == 64


def test_buffered_entries_are_visible_until_flushed() -> None:
    log = AppendOnlyLog(incident_id="inc-1").append(_entry(0)).append(_entry(1))
    assert len(log.unflushed) == 2
    flushed = log.entries[0].mark_written_to_rms(at=NOW)
    assert flushed.written_to_rms_at == NOW
