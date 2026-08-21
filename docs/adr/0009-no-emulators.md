# ADR 0009 — No emulators, and Workspace writes are their own switch

**Status:** accepted (2026-08-21)

Amends [ADR 0006](0006-one-contract-two-backends.md).

## Context

Two unrelated problems turned out to have the same shape: a switch that bundled
things which do not actually belong together.

**The emulators.** `make up` started Firestore and Pub/Sub emulators in Docker
so `make test-emulator` could run the contract suite with no credentials. CI had
already stopped using them — a hosted runner could not start them reliably, and
the replacement job runs against a real project. That left the emulators as a
local-only convenience with a cost nobody was paying attention to: the contract
suite exists to assert *semantics* — that a transaction serialises a
read-compare-write, that `create` on an existing document fails at the database
rather than at a Python guard a concurrent instance could race past, that a
fence counter survives a release, that ordered delivery stays ordered. Those are
precisely the properties a reimplementation approximates. A green local run
against an emulator was weaker evidence than it looked, and it looked like the
same evidence CI produced.

It had already misled once. The `FirestoreLockRepository.acquire` livelock —
every contender reading an exhausted transaction as a clean loss, so nobody took
the lock and the work never happened — reproduced only under enough contention,
and was found by hammering a real backend rather than by a passing emulator run.

**Workspace.** `USE_FAKE_AGENTS=false` built six live integrations at once.
Five of them — Firestore, Pub/Sub, Cloud Storage, Vertex, and the municipal
source fetchers — authenticate as the deployment's own principal, which is what
Application Default Credentials and a Cloud Run service account provide. Calendar
and Gmail do not. Both act *as a user*: a service account has no calendar and no
mailbox, so reaching them needs domain-wide delegation on a Google Workspace
domain or an interactive OAuth consent.

So a deployment with entirely valid credentials for five integrations could not
use any of them without also constructing two clients that raise on first call —
and they would raise in the middle of a survey dispatch, not at startup where a
configuration problem belongs.

## Decision

**One target for the contract suite.** `FIRESTORE_TEST_PROJECT` and
`PUBSUB_TEST_PROJECT`, reached through Application Default Credentials. The
emulator branch is gone from `tests/contract/`, the Makefile, `docker-compose.yml`,
and `.env.example`. Not configured means skip, loudly; CI fails the job on a skip.

**`WORKSPACE_WRITES` is a separate setting** with values `fake` and `google`,
defaulting to `fake` and read only when `USE_FAKE_AGENTS=false`. Cloud Storage
stays with the rest of live mode, because a pre-incident plan is written by the
deployment itself and has no user to act as.

`fake` is not a no-op. The calendar event and the crew mail are recorded through
the same durable idempotency store and emit the same audit events as the live
clients, and the console labels those two actions simulated. A silently skipped
crew notification would be worse than an admitted one — it is the same failure as
rendering an absent record as "none present".

## Consequences

- **Running the contract suite now costs a Google project.** That is the real
  price, and it is the point: 78 contract tests that used to pass against an
  approximation now pass against the thing. `make demo`, `make test`, and the
  880-test default suite still need no credentials at all, and that has not
  changed for anyone.
- **A judge or contributor cannot run the contract suite offline.** Accepted.
  The in-memory half of every contract test still runs in the default suite, so
  what they lose is the Firestore parametrisation, not the coverage of the
  behaviour.
- **Docker is no longer needed for anything but image builds.** `make up` and
  `make down` are gone; `docker-compose.yml` now only runs the app in fake mode.
- **A deployment can be honestly live without a Workspace domain.** The write
  actions the Delta Ranker performs split into those the fleet genuinely
  executes and two it records as simulated, and the split is visible in the
  console rather than buried in configuration.
- **`WORKSPACE_WRITES=google` is written and unrun.** Nobody on this project has
  a Workspace domain to delegate from, so that branch has been tested for which
  clients it constructs and never for whether they authenticate. It is
  catalogued honestly rather than claimed.
