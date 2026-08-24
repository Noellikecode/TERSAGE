# TERSAGE

Municipal structural intelligence for a fire department, built as an institutional
agent fleet.

A fire department already holds most of what its crews need to know about a
building: permits filed, inspections closed, violations still open, the
assessor's record of how it was built. That knowledge is spread across systems
nobody reads under time pressure. TERSAGE reads it continuously, keeps a
provenanced profile of every structure in a district, and has the answer ready
before the call comes in.

## Run it

No credentials, no cloud account.

```bash
make setup
make demo
```

The API serves on `:8000` and the console on `:3000`. Every port has a
credential-free implementation with the same authorization rules, idempotency
behaviour, event ordering and failure modes as the Google-backed one, so the
whole fleet runs on a laptop.

| Command | What it does |
|---|---|
| `make demo` | API and console, credential-free |
| `make slow-loop` | One complete slow-loop pass over a district |
| `make verify` | Lint, types, tests, schema, console build, secret scan |
| `make reset` | Deterministic demo state, same content hash every time |
| `make infra-check` | Terraform format, validate, and conformance |

## Two loops

**The slow loop** runs against municipal and federal records. It reads permits,
the assessor's roll, fire inspections, violations, parcels, federal hazard
registries and satellite geometry; turns filings into facts bound to the
character spans they came from; detects where two sources disagree; ranks
structures by whether a person needs to go and look; and drafts inter-agency
referrals for a captain to file.

**The incident loop** wakes on a 911 call or a CAD dispatch. It reads the
caller's narrative, opens the incident against one profile snapshot, and streams
a brief to the incident commander in three stages. The first stage contains no
model call — it is a deterministic template over stored facts, and
`BriefEmission` refuses to be constructed any other way.

The slow loop does not stop when a fire starts.

## What the fleet is

Thirteen agents are published in the catalog; nine are scheduled. The other four
are superseded and stay resolvable, because a brief recorded two years ago names
the agent version that produced it.

| Agent | Loop | Budget | Approval |
|---|---|---|---|
| `records-watcher` | slow | 120s | — |
| `hazard-watcher` | slow | 180s | — |
| `geometry-watcher` | slow | 300s | — |
| `structure-watch` | slow | 60s | — |
| `referral-clerk` | slow | 60s | captain |
| `incident-interceptor` | incident | 6s | — |
| `sensor-fusion` | incident | 2s | — |
| `agency-notifier` | incident | 5s | chief (utility shutoff) |
| `incident-recorder` | incident | 15s | — |

Each is published to a registry with its version, scopes, write targets and
latency budget. Departments subscribe to what they are authorized to run, pinned
to a version.

## How it is built

Ports and adapters. Sixteen ports, one per seam, and nine adapter packages —
`memory`, `fake`, `firestore`, `pubsub`, `google`, `vertex`, `resend`, `nasa`,
`clock`. Two implementations of nearly every port, so the credential-free demo is
a faithful rehearsal rather than a mock.

- **Domain** — facts, conflicts, profiles, geometry, briefs, the merge engine,
  the conflict rules. Deterministic, no I/O.
- **Gateway** — default-deny policy on every read and write, with PHI derivation
  and jurisdiction checks.
- **Registry** — the agent catalog, versioning, and per-department subscription.
- **Memory bank** — durable open questions that outlive a pass, recall gated by
  the caller's scopes.
- **Reliability** — failure classification, derived backoff, circuit breakers.
- **Observability** — structured logs, OpenTelemetry traces, an append-only
  audit log that never holds document contents.

Firestore and Pub/Sub sit behind the same contracts as the in-memory
implementations, and one contract suite holds both to the same behaviour.

## The epistemics

Three states, and none of them is an empty field.

- An attribute nothing settled is `UNKNOWN`, with the sources that were checked.
- An attribute two sources disagree about is `DISPUTED`, with both facts intact.
  Disagreement is never averaged.
- A source that could not be reached is `UNAVAILABLE`, naming the source. It is
  not an absence of hazard.

Every fact carries its source, its snapshot, when it was observed, a confidence,
and the span in the document that supports it. Facts are immutable and
append-only; a correction is a new fact that supersedes an old one, and both
remain.

**The model may not author a fact.** Models extract typed values bound to spans,
route documents, compose prose and resolve references. Deterministic code decides
what is true, what conflicts, and what gets ranked.

**A human approves anything that reaches outside the department.** A captain
files an inter-agency referral; a chief approves a utility shutoff. Both gates
are enforced twice — once at the decision and once at the write.

## Reasoning

Six agents run LangGraph graphs on Gemini through Vertex AI: `records-watcher`,
`hazard-watcher`, `referral-clerk`, `incident-interceptor`, `incident-recorder`,
`agency-notifier`. A graph decides what to look up, what to cross-check, and when
it is done. When a graph runs out of budget it checkpoints into the memory bank
and opens a question, so work that spans weeks survives a restart.

`incident-interceptor` is the head of the incident loop. It reads the caller's
narrative against the profile the slow loop compiled, then writes a focus to the
incident log — pointers carrying an id and a reason, never a value — and the
other incident agents read it. `incident-recorder` closes questions the slow loop
opened months earlier, when the incident actually answered them.

## External services

| Service | Used for |
|---|---|
| Vertex AI — Gemini, Gemma | Extraction, composition, triage, imagery reading |
| Google Search grounding | Resolving a reference to a canonical id |
| Model Armor | Inline screen on every ingested document |
| Firestore, Pub/Sub, Cloud Run, Secret Manager | State, events, execution, credentials |
| Vertex Vector Search | Semantic recall over screened narratives |
| Google Solar API, Street View, Static Maps | Roof geometry, building imagery |
| NASA FIRMS | Regional satellite fire activity |
| NASA POWER | Fire-weather context |
| Resend | Delivering an approved referral |
| SF open data, EPA FRS, USGS 3DEP, NREL, NWS | Municipal and federal records |

Ten of thirteen catalogued sources have live endpoints. The rest are catalogued
with the reason they have none, and say so on screen.

## The console

One screen, two arrangements. In standby the fleet works across the width with a
regional fire-activity map in the middle; each agent has its own visual and a
terminal showing what it did. On dispatch the page reorganises: incident agents
on the left, the massing model and building imagery in the middle, the slow loop
on the right, still running.

## Deployment

Thirteen Terraform modules across staging and production. Eleven Cloud Run
services — the two loops and nine per-agent workers — each running as its own
service account with only the roles its declared scopes imply. The IAM and
Firestore index policy is derived from the agent descriptors, and a conformance
suite fails if the two drift apart.

## Verification

1,425 backend tests and 278 console tests. Strict mypy across 184 source files.
Ruff. A contract suite that holds the in-memory and Firestore backends to one set
of behaviours, and an infrastructure suite that holds Terraform to the code.

Apache-2.0.
