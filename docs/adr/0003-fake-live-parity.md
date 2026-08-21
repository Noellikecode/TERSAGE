# ADR 0003 — Fake adapters are the same system, not a mock of it

**Status:** accepted (phase 1)

## Context

`USE_FAKE_AGENTS=true` must run the entire fleet with no Google credentials —
that is how the test suite runs and how a judge evaluates the project for free.
A fake layer that always succeeds would make that guarantee worthless: the test
suite would be exercising a different system from the one that deploys.

## Decision

Fake adapters implement the same protocols in `ports/` and reproduce the same
observable behaviour:

| Behaviour | Where it is enforced |
|---|---|
| Authorization | `FakeRuntime` denies a missing scope or expired grant before running anything |
| Idempotency | `FakeWriteTarget` returns the original receipt with `replayed=True`; a same-key different-body replay is a 409 |
| Event ordering | `InMemoryEventBus` delivers in publish order, dedupes per subscriber, retries, then dead-letters |
| Failure modes | `FakeSourceAdapter` opens its circuit after three failures and half-open probes after a cooldown |
| Concurrency | `InMemoryProfileRepository` raises `StaleVersionError` (409) on a stale write |
| Model | `FakeModelClient` extracts against real character offsets and can be made unavailable |

There is no partial mode. `build_container` raises `ConfigurationError` rather
than falling back to a fake adapter in live mode — a process that silently
downgraded would be lying about where its data came from.

## Consequences

- Every degraded-service, authorization, idempotency, and concurrency test runs
  credential-free in CI.
- Live adapters have a behavioural specification to meet: the fake tests are the
  contract.
