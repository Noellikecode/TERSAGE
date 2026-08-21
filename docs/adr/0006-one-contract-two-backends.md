# ADR 0006 — In-memory repositories are a second implementation, not a stub

**Status:** accepted (phase 2)

## Context

Phase 2 adds Firestore-backed durable memory. The obvious shape is Firestore for
real and something quick in memory for tests. That shape has a specific failure:
the in-memory version drifts, the tests keep passing, and the behaviours the
system depends on — a 409 on a stale write, a gapless log sequence, a lock that
actually excludes — turn out to hold only in the fake.

Fake mode is also the demo. `make demo` runs with no credentials, so the
in-memory repositories are what a judge evaluates the system on. They cannot be
a lesser thing.

## Decision

Both are first-class implementations of one contract, and both are held to one
test suite.

`tests/contract/` is parametrised over `["memory", "firestore"]`. Every test runs
twice. The Firestore parametrisation skips only when no emulator is reachable,
and CI fails the job if anything skips — a skipped backend has proved nothing.

The suite covers what the rest of the system assumes: optimistic concurrency,
append-only facts and logs, gapless sequences under concurrent writers, stable
snapshot ids, version pinning, fenced locks, idempotency claims, and terminal
agent-run states.

## Consequences

- A behavioural difference between the backends is a bug in one of them, and the
  suite says which. It has already caught one: the in-memory write-action lookup
  returned the first *recorded* action while Firestore could only order by
  `created_at`, so the in-memory one was changed to match.
- Writing a new repository means writing it twice. Accepted: the second one is
  cheap, and the alternative is a demo that behaves differently from production.
- `make up && make test-emulator` runs both locally. Both were run for phase 2
  and both pass — 27 contract tests per backend, plus the Pub/Sub transport
  tests against the Pub/Sub emulator.
