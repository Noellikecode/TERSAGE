# ADR 0007 — Failures are classified before they are retried

**Status:** accepted (phase 2)

## Context

At-least-once delivery means every consumer will be handed the same message
again. The naive policy — retry N times, then dead-letter — has two failure
modes that matter operationally:

- A **poison message** (malformed, or naming a fact that cannot exist) burns the
  full retry budget on every redelivery and never succeeds. At scale that is a
  queue that stops moving.
- A **correct refusal** (the agent's grant lacks the scope) is retried as though
  the authorization system might change its mind.

Meanwhile a genuine dependency outage should be retried, and should also stop
being fed after a while so it can recover.

## Decision

`firstdue/reliability/retry.py` classifies every handler failure into four
classes, and the dispatcher acts on the class rather than the exception type:

| Class | Meaning | Action | Counts against the breaker |
|---|---|---|---|
| `TRANSIENT` | dependency is unwell | retry with backoff | yes |
| `CONTENDED` | another writer got there first | retry with backoff | no |
| `PERMANENT` | a correct refusal | dead-letter now | no |
| `POISON` | the message is unprocessable | dead-letter now | no |

An unrecognised exception is `TRANSIENT`: giving up immediately on an unknown
failure loses work that might have succeeded, and the dead letter still happens
— just after the retries.

Backoff is exponential with **derived** jitter: a hash of the event id and the
attempt number, so a replay waits exactly what the original wait was and a test
can assert the schedule instead of tolerating it. Nothing reads a clock or a
random number generator.

The policy lives in one place and both transports run it. The in-memory bus and
the Pub/Sub push endpoint are two transports over one
`EventDispatcher` — so what fake mode proves is what the deployed system does.

## Consequences

- A poison message is dead-lettered on attempt 1 and the push endpoint **acks**
  it. Nacking would guarantee the same bytes arrive again forever.
- A consumer whose dependency is down is cut off by its breaker after three
  transient failures, and its messages return 503 so Pub/Sub redelivers them
  later rather than the fleet burning retries on a dead dependency.
- Poison messages never open a breaker: a bad message says nothing about whether
  the consumer is healthy.
- Dead letters are surfaced at `GET /api/v1/internal/events/dead-letters` with
  attempt counts and stable error codes. Never a message, never a stack trace.
