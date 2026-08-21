# ADR 0002 — Events carry identifiers, never payloads

**Status:** accepted (phase 1)

## Context

Agents hand work to each other across Pub/Sub with at-least-once delivery.
If an event carried the record it describes, a consumer could act on the
event's copy instead of stored state, and a replay months later would produce
a different result from the original delivery.

## Decision

`EventEnvelope.ids` accepts identifier-shaped tokens only. A validator rejects
any value containing whitespace — which is what a prose payload looks like.
Consumers re-read from the store; envelopes carry `correlation_id`,
`causation_id`, and an `idempotency_key` for delivery dedupe.

```python
EventEnvelope(topic=Topic.FACT_WRITTEN, ids={"address_id": "sf-0450-hayes",
                                             "fact_ids": ("fact_a", "fact_b")}, ...)
```

## Consequences

- Replaying an event produces the same result as the original delivery, which is
  what makes NIOSH replay possible.
- Consumers cost one extra read. Accepted: correctness over a saved round-trip.
- Agents never call each other directly. There is no in-process handle from one
  agent to another anywhere in the codebase.
- Tested in `tests/unit/test_events_vectors_log.py`.
