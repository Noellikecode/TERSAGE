"""What a Firestore document actually holds, and what may therefore be queried.

:func:`codec.encode` stores the model as one JSON string and promotes only the
fields passed to it as index arguments. Everything else -- most of the model --
is *inside* that string and does not exist as a document field.

That matters more than it sounds, because Firestore answers a query ordered by a
field a document does not have by **excluding the document**, not by ignoring the
clause. Ordering the audit log by ``occurred_at`` therefore returned zero rows
rather than unordered ones, the console read an empty audit log, and every agent
in the fleet was drawn idle -- with nothing anywhere reporting an error.

So this pins the queryable surface of the one collection the console reads on a
timer. A field added here is a field a query may name; a field not here is not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from firstdue.adapters.firestore.codec import DOC_SCHEMA_FIELD, PAYLOAD_FIELD, encode
from firstdue.ports.audit import AuditEvent, AuditEventKind

NOW = datetime(2026, 8, 28, 9, 30, tzinfo=UTC)


def _event() -> AuditEvent:
    return AuditEvent(
        audit_id="audit_1",
        kind=AuditEventKind.AGENT_STEP,
        occurred_at=NOW,
        actor="sensor-fusion",
        actor_version="1.0.0",
        target="inc_1",
        correlation_id="corr_1",
        detail={"entry": "agent_analysis", "sequence": "7"},
    )


def _document() -> dict[str, object]:
    """Encoded exactly as ``FirestoreAuditSink.record_event`` encodes it."""
    event = _event()
    return encode(
        event,
        audit_id=event.audit_id,
        incident_id=event.incident_id,
        kind=str(event.kind),
        correlation_id=event.correlation_id,
    )


def test_only_the_named_index_fields_are_queryable() -> None:
    document = _document()
    queryable = set(document) - {PAYLOAD_FIELD, DOC_SCHEMA_FIELD}
    assert queryable == {"audit_id", "kind", "correlation_id"}


def test_occurred_at_is_not_a_document_field_so_no_query_may_order_by_it() -> None:
    """The specific mistake, named.

    A Firestore ``order_by("occurred_at")`` on this collection matches nothing.
    If a future change wants server-side ordering it has to promote the field in
    ``record_event`` first -- and even then every document already written lacks
    it, so the backfill is part of the change, not an afterthought.
    """
    assert "occurred_at" not in _document()


def test_the_value_is_still_there_to_sort_on_in_memory() -> None:
    """Which is why sorting in Python is not merely a fallback but correct."""
    payload = json.loads(str(_document()[PAYLOAD_FIELD]))
    assert payload["occurred_at"].startswith("2026-08-28T09:30")
    assert payload["actor"] == "sensor-fusion"
