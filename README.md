# FIRST DUE

**Municipal structural intelligence as an institutional agent fleet.**

A fire officer gets 60–90 seconds to decide how a building will behave before
crews go inside. Everything that would inform that decision already exists in
writing — the permit for the attic conversion, the inspection that flagged a
blocked stairwell, the roof geometry showing a solar array that cannot be cut,
the fact that the floor is lightweight parallel-chord truss.

None of it is reachable with an engine rolling. FIRST DUE makes it reachable by
doing the work months earlier.

> **This is a decision-support prototype, not a certified public-safety system.**
> It has not been through the validation any tool would need before an incident
> commander relied on it under fire conditions. See [Honest disclosure](#honest-disclosure).

---

## The architectural thesis

**The brief is instant because months of background work already happened.**

|                | Slow loop                                    | Incident loop                       |
| -------------- | -------------------------------------------- | ----------------------------------- |
| Trigger        | Scheduler, continuous                        | CAD dispatch event                  |
| Horizon        | Weeks to years                               | Seconds                             |
| Job            | Watch sources, extract, detect conflict, rank survey work, file referrals | Load profile, stream brief, notify agencies, log the incident |
| Output         | Department readiness console                 | Streaming tactical brief            |
| Scale          | 3,800+ structures per district               | 1 structure, no artificial delay    |

No system can cold-query eleven municipal sources fast enough to matter. The
slow loop is the product; the incident loop is the payoff.

## The three principles

1. **Disagreement is signal.** When the permit says two stories and the lidar
   measures three, the system surfaces the conflict rather than averaging or
   picking a winner — unpermitted construction is itself a structural risk.
2. **Absence renders as UNKNOWN, never as NONE.** "No hazmat filing on record"
   and "no hazardous materials present" are different statements.
3. **Inferred renders differently from observed.** Confidence propagates to
   every downstream conclusion.

## What it will never do

No tactical recommendations. No offensive/defensive call. No crew assignments.
No evacuation orders. No fire-behaviour prediction. Every incident agent is
information delivery or clerical execution. Tactics belong to the incident
commander, and an agent that nudges them is a liability.

Gemini extracts facts into strict schemas, composes bounded prose, and explains
deterministic results. Gemma decides only whether a document is worth a Gemini
call — the one judgement whose failure is safe in both directions, and it fails
open. Neither model makes an authorization decision, decides whether facts
conflict, invents a structural fact, fills an UNKNOWN, blocks the instant brief,
or issues a tactical recommendation. **The instant brief stage contains no model
call at all.**

---

## Quick start (no credentials required)

```bash
make setup      # install backend + frontend toolchains (Python 3.12 via uv)
make demo       # start the credential-free demo: API on :8000, console on :3000
```

Fake mode is the default. It runs the entire fleet, gateway, and console with no
Google credentials — the fake adapters implement the same interfaces,
authorization rules, idempotency behaviour, event ordering, and failure modes as
the live ones.

### The commands

| Command               | What it does                                                    |
| --------------------- | --------------------------------------------------------------- |
| `make demo`           | Credential-free demo: backend + frontend, seeded and ready       |
| `make verify`         | The complete verification suite: lint, types, tests, build, scan |
| `make reset`          | Deterministic demo reset — same content hash every time          |
| `make deploy-staging` | Documented staging deployment (see `docs/deploy.md`)             |
| `make slow-loop`      | One complete slow-loop pass over a district, no credentials       |
| `make infra-check`    | Terraform format, validate, and conformance — no credentials      |

Run `make help` for the full target list. `make up && make test-emulator` runs
the durable-memory contract suite against the Firestore and Pub/Sub emulators —
the same tests that run in memory, against the real clients.

## Repository layout

```
backend/src/firstdue/   FastAPI application, domain model, ports, adapters
  domain/               Models, invariants, and the deterministic engines
  reliability/          Failure classification, derived backoff, circuit breakers
  eventing/             One delivery policy, shared by both transports
  sources/              Source framework: caching, limits, snapshots, backfill
  extraction/           Screening, triage, typed extraction with spans
  agents/               Records · geometry · hazard watchers, ranker, actions
  gateway/              Default-deny policy, PHI derivation, jurisdiction
  incident/             Controller, reconciler, fusion, resources, recorder
  security/             Screening, signed callbacks, request limits
  registry/             The eleven agent descriptors, and topic routing
  observability/        Structured logs, OpenTelemetry traces and metrics
  adapters/             memory · fake · firestore · pubsub · google · vertex
frontend/               Next.js 14 App Router command center
  app/api/gateway/      Server-side proxy; the backend credential never reaches the browser
  components/           Standby, profile, incident, audit, geometry
  lib/api/              Typed client, SSE stream, contract-checked types
infra/terraform/        Terraform (OpenTofu): 12 modules, staging and prod
  policy/               Index, topic, and IAM data derived from the code
docs/                   Architecture, ADRs, setup, build notes, threat model
fixtures/               Synthetic fixtures (EMS, Tier II, CAD, RMS, thermal)
tests/                  pytest suite: invariants, API, adapters, contract
```

## Documentation

- [Setup](docs/setup.md) — toolchains, environment variables, running locally
- [Architecture](docs/architecture.md) — the two loops, the fleet, the gateway
- [Architecture decisions](docs/adr/) — why the system is shaped this way
- [Threat model](docs/threat-model.md) — what can go wrong, what stops it, what does not
- [Build notes](docs/build-notes.md) — decisions, deviations, commands, risks

## Honest disclosure

A hidden simulation is worse than an admitted one.

- CAD dispatch, the building department referral intake, and the department
  records system are **simulated receiving APIs with real write semantics**.
- EMS, mutual-aid, and confidential Tier II fixtures are **synthetic**.
  **No real person's records appear anywhere in this project.**
- Permit, assessor, inspection, imagery, hazmat, pipeline, EV, weather, and
  lidar sources are real public data for real addresses.
- Thermal footage is recorded, not a live flight.

Default municipality: **San Francisco**. City-specific behaviour is isolated
behind adapter interfaces.

## Licence

Apache-2.0.
