# ADR 0004 — The instant brief stage contains no model call

**Status:** accepted (phase 1)

## Context

Stage one of the brief has a 500 ms budget. It carries construction type, storey
count, unresolved conflicts, collapse zone, occupancy, and suppression status —
the facts a commander needs before crews make entry.

A model call in that path adds latency variance, a dependency that can be down,
and a component that can invent a value.

## Decision

`BriefEmission` refuses to exist in an invalid shape:

```python
if self.stage is BriefStage.INSTANT:
    if self.model_invoked:   raise ValidationError(...)
    if self.narrative:       raise ValidationError(...)
    if self.version != 1:    raise ValidationError(...)
```

Stage one is a deterministic template over stored facts. Prose arrives at stage
two and is appended; if Vertex AI is unavailable, stage two still emits with
`narrative_available=False` and the brief says the narrative is unavailable.

Related invariant, same module: `require_persisted()` is the only sanctioned
gate in front of a transport, so nothing reaches the commander that is not
already in the incident log.

## Consequences

- Speed and safety point the same direction — the fastest path is also the one
  that cannot fabricate.
- A model outage degrades the brief rather than blocking it.
- Tested in `tests/unit/test_briefs.py`.
