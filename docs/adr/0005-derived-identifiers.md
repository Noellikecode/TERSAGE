# ADR 0005 — Derived identifiers, not minted ones

**Status:** accepted (phase 2)

## Context

The system re-derives things it has already derived. A watcher re-polls a source
it polled yesterday; a redelivered event makes a consumer materialize a profile
it already materialized; an incident opens against a profile version another
incident already snapshotted.

With minted identifiers — a counter, a UUID — each of those produces a *new*
record that is indistinguishable from a genuinely new finding. The same
disagreement gets filed twice, under two ids, and an officer sees the building
arguing with itself twice.

## Decision

Identifiers for derived records are a hash of what produced them.

| Record | Derived from |
|---|---|
| `Conflict.conflict_id` | rule id, address, canonical key, sorted fact ids |
| `ProfileSnapshot.snapshot_id` | address, profile version, profile content hash |
| `ProfileEvent.event_id` (materializer) | address, the conflict it records |
| `IdempotencyRecord.storage_id` | scope, key |
| Subscription ids (seeded) | department, agent id |

```python
conflict_id_for("permit-vs-lidar-story-count", "sf-0450-hayes",
                "structure.stories", ["fact_a", "fact_b"])
# -> "conflict_8887f472ae0b3b04c4361795", every time, forever
```

Facts keep minted ids: a fact is an observation, and two observations of the same
attribute are two facts even when they say the same thing.

## Consequences

- Idempotency stops being a flag somebody has to remember to check and becomes a
  property of the arithmetic. Re-running the conflict engine over unchanged facts
  produces the same ids, so "already recorded" is an exact test.
- Replay equivalence follows: `tests/unit/test_materialize_replay.py` asserts
  that a second materialization changes nothing — not the conflicts, not the
  decay map, not the profile version, not the content hash.
- Ids are opaque. An operator cannot tell when a conflict was found by looking at
  its id, and must read `detected_at`. Accepted.
- Changing a rule id changes every conflict id it produces. That is correct — a
  different rule is a different finding — but it means rule ids are part of the
  public contract, not an implementation detail.
