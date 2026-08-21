# ADR 0001 — Slow loop and incident loop are separate tempos

**Status:** accepted (phase 1)

## Context

A fire officer gets 60–90 seconds to decide how a building will behave. The
information that would inform that decision lives in eleven municipal and
federal systems owned by different departments, each with its own access
boundary, rate limits, and availability.

## Decision

Two loops with different tempos, joined by exactly one interface.

| | Slow loop | Incident loop |
|---|---|---|
| Trigger | Scheduler, continuous | CAD dispatch |
| Horizon | Weeks to years | Seconds |
| Reads | Eleven external sources | One `ProfileSnapshot` |
| Writes | Facts, conflicts, queue, referrals | Brief emissions, log entries |

The interface is `ProfileSnapshot` (`domain/profiles.py`), read once at incident
start and recorded on the incident by `snapshot_id`.

## Why this is necessity, not framing

Cold-querying eleven sources at dispatch cannot meet the latency budget, and
would fail on any source outage. Precomputing means the instant brief is a
single Firestore read.

## Consequences

- The slow loop must be genuinely useful on its own — it is, as the readiness
  console and survey queue.
- Cold start is a real path: an address with no profile degrades to live queries
  and the brief says *"No pre-incident profile. Structural attributes unknown."*
  `ProfileSnapshot.is_cold_start` exists for exactly this.
- Staleness must be visible. `domain/decay.py` computes it deterministically and
  it lands on every snapshot.
