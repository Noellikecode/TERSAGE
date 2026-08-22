# Architecture

## The shape

```
                     ┌──────────────── slow loop (continuous) ────────────────┐
  Scheduler ──▶ source.poll ──▶ watchers ──▶ facts ──▶ conflict engine ──▶ ranker
                                   │                        │                 │
                                   └──────── BuildingProfile (Firestore) ─────┘
                                                    │
                                            ProfileSnapshot
                                                    │
                     ┌──────────────── incident loop (seconds) ───────────────┐
  CAD dispatch ──▶ incident controller ──▶ reconciler ──▶ SSE ──▶ IC tablet
                          │                     │
                          └── grant ────────────┴──▶ incident record agent ──▶ RMS
```

One interface joins the loops: `ProfileSnapshot`. See [ADR 0001](adr/0001-two-loops.md).

## Layers

| Package | Responsibility |
|---|---|
| `domain/` | Models, invariants, and the deterministic engines. No I/O, no framework, no clock reads. |
| `ports/` | Protocols every adapter must satisfy. |
| `adapters/` | Implementations — `memory/`, `fake/`, `firestore/`, `pubsub/`. |
| `sources/` | The source framework: caching, rate limiting, snapshots, resumable backfill. |
| `extraction/` | Document screening, triage, typed extraction with spans. |
| `agents/` | The slow-loop fleet: records, geometry, hazard, ranker, actions. |
| `incident/` | The incident loop: controller, reconciler, fusion, resources, recorder. |
| `gateway/` | Default-deny policy, PHI derivation, jurisdiction filtering. |
| `security/` | Injection screening, signed callbacks, request limits. |
| `services/` | Where pure engines meet durable stores. |
| `reliability/` | Failure classification, derived backoff, circuit breakers. |
| `eventing/` | One delivery policy: dedupe, retries, dead letters, Pub/Sub codec. |
| `registry/` | The eight agent descriptors and registry seeding. |
| `city/` | Municipality-specific behaviour. San Francisco is the only one. |
| `api/` | App factory, middleware, error envelope, auth, routes. |
| `observability/` | Request context, redaction, structured logging. |
| `demo/` | Deterministic seed, reset, and state loading. |
| `container.py` | The composition root — one place decides fake or live, memory or Firestore. |

Dependencies point inward. `domain/` imports nothing from `api/`, `adapters/`,
or `ports/`.

## Invariants, and where they live

| Invariant | Enforced in |
|---|---|
| A fact cannot exist without provenance | `domain/facts.py` — `source_ref`, `source_snapshot_id` required and non-blank |
| Only a survey sets `human_verified` | `domain/facts.py` — validator requires `HUMAN_SURVEY` + `survey_id` |
| UNKNOWN ≠ null ≠ false ≠ missing | `domain/values.py` — four distinct absence types; `value` is required |
| Conflicting facts both persist | `domain/factsets.py` — `FactSet` appends, never replaces |
| Live observation outranks memory | `domain/merge.py` — tier compared before recency and confidence |
| PHI / Tier II never reach a vector | `domain/vectors.py` — raises at construction |
| Every external write has an idempotency key | `domain/work.py` — `WriteAction` cannot be built without one |
| Timelines and logs are append-only | `domain/profiles.py`, `domain/logentries.py` — gapless sequences, no update or delete |
| Profiles use optimistic concurrency | `domain/profiles.py` + `adapters/memory/repositories.py` — 409 on stale write |
| Briefs persist before transmission | `domain/briefs.py` — `require_persisted()` |
| The instant brief invokes no model | `domain/briefs.py` — validator on `stage is INSTANT` |
| No model makes an authorization decision | `domain/policy.py` — `decided_by` is a constant literal |
| Events carry ids, not payloads | `domain/events.py` — validator rejects non-identifier tokens |
| Standing grants can never reach PHI | `domain/identity.py` — validator on allowed classifications |
| Re-detecting a conflict yields one record | `domain/conflict_engine.py` — conflict ids derived from rule + address + key + fact ids |
| Only a human observation closes a conflict | `domain/conflict_engine.py` — `survey_resolutions()` requires a survey record |
| A published agent version is immutable | `adapters/*/repositories.py` — republishing a changed descriptor is a 409 |
| A pinned version stays pinned | `RegistryRepository.resolve_pinned` — publishing newer versions does not move it |
| One district is polled by one instance | `domain/locks.py` — leased, fenced locks |
| A duplicate delivery has one effect | `domain/idempotency.py` — claim before the work, complete after |
| A run reaches a terminal state and stays there | `domain/runs.py` — `TERMINAL_STATUSES` is closed; transitions are guarded |
| Poison messages are never redelivered forever | `reliability/retry.py` — classified, then dead-lettered on attempt 1 |

Each has at least one test asserting the failure, not just the success.

## The five gateway outcomes

`ALLOW · DERIVE · WITHHOLD_JURISDICTION · REQUIRE_APPROVAL · DENY` — a closed
enum in `domain/enums.py`, produced by `gateway/engine.py`: an ordered list of
small named functions, each of which either answers or abstains. A request that
matches nothing is denied by `policy.default-deny`.

| Outcome | Means | Evidence the decision must carry |
|---|---|---|
| `ALLOW` | the grant carries the exact scope | rule id, justification |
| `DERIVE` | PHI: a life-safety fact is returned, the record is not | the derivation function that ran |
| `WITHHOLD_JURISDICTION` | the record exists and is not shared | the aid agreement applied |
| `REQUIRE_APPROVAL` | staged and prefilled for one human tap | the threshold (supervisor / chief) |
| `DENY` | refused | rule id, justification |

Nothing here reads a clock, draws a random number, or imports model code — the
last is asserted by a test that walks the gateway's source. `decided_by` is a
`Literal` constant, so "no model makes an authorization decision" is checkable
in the audit record rather than asserted in a README.

**Grants.** A `StandingGrant` is the slow loop's permanent, narrow authority and
refuses at construction to hold person-level scope. An `IncidentGrant` is bound
to one incident, one address, one jurisdiction, and one responding agency, with
a TTL, and is revoked at incident close. Each binding is checked separately,
because each corresponds to a different way authority leaks.

**Read never implies write.** `READ_SCOPES` is derived as
`set(Scope) - WRITE_SCOPES`, and the check is exact membership. Every read scope
in the system is tried as a write in the test suite and refused.

## Semantic recall

Structured facts are what the department *believes*. The narratives those facts
were read out of are searchable separately, by meaning, through `VectorIndex` —
in-memory in fake mode, Vertex Vector Search in live mode.

A match is a **pointer, never an assertion**. It carries the ids and a distance,
not text and not a value, so an officer goes and reads the record. An embedding
can say two documents resemble each other; it cannot say a building has three
storeys, and nothing downstream may promote a match into something the system
believes. Only *screened* text is indexed, so an injection attempt an ingested
document carried is not something a later query can recall, and `PHI` and
`TIER_II_CONFIDENTIAL` never enter the index at all — refused at payload
construction and again at the adapter boundary.

## Durable memory

Two backends, one contract. `STORAGE_BACKEND` selects `memory` or `firestore`;
`tests/contract/` runs every repository test against both, and CI fails if either
is skipped. See [ADR 0006](adr/0006-one-contract-two-backends.md).

| Property | How it holds in memory | How it holds in Firestore |
|---|---|---|
| Optimistic concurrency | `asyncio.Lock` around read-compare-write | one transaction around read-compare-write |
| Append-only | id already present → raise | `create()`, so a duplicate fails at the database |
| Gapless log sequence | mutex + `AppendOnlyLog.append` | transaction over counter doc + entry create |
| Distributed lock | lease + monotonic fence counter | transaction; fence survives release |
| Idempotency | claim before work, complete after | same, transactionally |

A Firestore document stores the model as one canonical JSON string plus a few
lifted index fields. That is what makes a round trip byte-identical — and it is
also the only way to store a building footprint, since Firestore rejects nested
arrays.

## Events

An envelope carries identifiers, a schema version, correlation and causation
ids, an idempotency key, and its retry state. Nothing else. Both transports —
the in-process bus and the Pub/Sub push endpoint — run the same
`EventDispatcher`, so dedupe, retry classification, jittered backoff, circuit
breaking, and dead-lettering are literally the same code.

```
publish ─▶ schema check ─▶ dedupe (per subscriber) ─▶ breaker ─▶ handler
                │                    │                   │          │
             POISON               DEDUPED           CIRCUIT_OPEN   fail ─▶ classify
                │                                                          │
                └──────────────── dead letter ◀────── retries exhausted ◀───┘
```

The push endpoint's status codes are its contract with the broker: `200` for
delivered, deduped, *and* dead-lettered (all three mean "do not send this
again"); `503` when a breaker is open or another worker holds the claim; `401`
or `403` for a caller that is not the fleet. See
[ADR 0007](adr/0007-failure-classification.md).

## The agent registry

Eleven agents, two loops, five external write targets, all published at `1.0.0`
and pinned by the fire department at startup.

**Every one of them runs through `AgentRuntime`.** `agents/fleet.py` resolves
the pinned descriptor, obtains the authority that descriptor's scopes imply,
opens a durable run record, hands the work to the runtime, and closes the record
with whatever terminal state came back. Four properties follow that no agent has
to remember: no agent runs without a grant, none runs past the latency target
its own descriptor declares, every run — including the denied and timed-out ones
— reaches a terminal state on the record, and every run names the pinned version
that produced it. A slow-loop agent runs under a standing grant; an incident
agent runs under the incident's own grant and the runner refuses to mint it a
permanent one.

| Agent | Publisher | Loop | Writes to | Approval | Budget |
|---|---|---|---|---|---|
| `records-watcher` | building | slow | — | — | 120 s |
| `geometry-watcher` | fire | slow | preincident-plan-store | — | 300 s |
| `structure-watch` | fire | slow | inspection-work-orders | — | 60 s |
| `referral-clerk` | fire | slow | building-referral-intake | supervisor | 60 s |
| `incident-interceptor` | fire | incident | — | — | 6 s |
| `sensor-fusion` | fire | incident | — | — | 2 s |
| `agency-notifier` | fire | incident | agency-notifications | chief | 5 s |
| `incident-recorder` | fire | incident | department-rms | — | 15 s |

Four agents were merged into two, and the originals are **still catalogued and
still resolvable** — deprecated, never deleted:

| Superseded | Merged into | Why they were one job |
|---|---|---|
| `conflict-detector` + `survey-ranker` | `structure-watch` | Ranking reads the conflicts detection just wrote, on the same profiles, in the same pass. Now severity and rank come from one reading, so they cannot describe different corpora. |
| `incident-controller` + `brief-reconciler` | `incident-interceptor` | One produced stage one of the brief and the other produced stages two and three of the same document. |

Version pinning exists for NIOSH investigations, and every brief records the
agent versions that produced it. An `agent_id` deleted from the catalog turns a
two-year-old recorded run into a reference to something this build has never
heard of, so a superseded agent stays resolvable and stops being scheduled.
`ACTIVE_FLEET` — derived from the absence of `deprecated_at`, not hand-listed —
is what routing, workers, and service accounts come from.

`structure-watch` carries **no approval threshold**, and that is a correction
rather than a relaxation. `survey-ranker` published `SUPERVISOR` while nothing
on the work-order path ever called the gateway: a gate asserted and never held.
A work order commits the department's *own* morning, so the department's own
agent may cut one. A referral accuses a property owner and still needs a
captain; a utility shutoff or a road closure still needs a chief.

`Capability.WRITE` means writing *outside* the department's own store. An agent
that only appends facts to a profile declares the `write:profile` scope and no
write targets; the descriptor model rejects one without the other.

Pinning is not devops hygiene. A NIOSH investigation has to reconstruct what a
commander knew two years ago, so publishing `2.0.0` does not move anybody's pin —
upgrading is a decision a department makes.

## The deterministic engines

Everything that decides what the system believes is pure: no clock, no
repository, no model, no randomness. `now` arrives as an argument.

| Engine | Module | Decides |
|---|---|---|
| Conflict detection | `domain/conflict_engine.py` | which facts disagree, and how badly |
| Confidence decay | `domain/decay.py` | how much an aging fact is still worth |
| Merge precedence | `domain/merge.py` | which fact represents an attribute now |
| Materialization | `domain/materialize.py` | the two above, applied to a whole profile |

The rule registry is open — a new rule registers without this code changing —
but the *shape* of a rule is closed: it must cite its `rule_id` and every fact it
rests on, so the console can show an officer exactly which two documents
disagree. Three rules ship: `permit-vs-lidar-story-count`,
`authoritative-source-disagreement`, and `survey-contradicts-record`.

A model may narrate a conflict after the fact. It can neither create one nor
change its severity — a model that could invent a conflict could also invent its
absence.

## The slow loop, end to end

`make slow-loop` runs this with no credentials:

```
sources ──▶ screen ──▶ triage ──▶ extract ──▶ facts ──▶ materialize ──▶ conflicts
   │                                             │                          │
 cache                                      provenance                    ranker
 limit                                    (ref, snapshot, span)             │
 breaker                                                              survey queue
                                                                            │
                        ┌───────── autonomous ─────────┬──── approval-gated ─┘
                        │                              │
        work order · calendar · crew mail · pre-plan    referral ──▶ human tap ──▶ case number
```

**The four ranking signals**, weights fixed in `agents/ranker.py`:

| Signal | Weight | Means |
|---|---|---|
| Open conflict severity | 0.40 | Two sources disagree; only a person settles it |
| Confidence decay | 0.25 | What is on file has aged past being relied on |
| Source churn | 0.20 | Filings recorded since anyone last looked |
| Survey age | 0.15 | Nobody has stood in the building in a long time |

Every queue row cites the rules that produced it and the facts behind them. A
row with no reason cannot be constructed.

**What is autonomous and what is not.** A work order, a calendar hold, a crew
notification, and a pre-incident plan commit the department's own time — an
agent may do them. A referral to the building department accuses a property
owner and commits another agency, so it is staged, prefilled, and waits for one
human tap. The case number that comes back lands on the profile.

## Source availability

Every source reports where its records came from, and the console renders it
verbatim.

| Mode | Means | Fetch behaviour |
|---|---|---|
| `LIVE` | a real public feed | HTTP, breaker, rate limit |
| `FIXTURE` | deterministic synthetic records | in-memory, paginated |
| `UNCONFIGURED` | named in the catalog, no endpoint reachable | raises; the fact becomes `UNAVAILABLE` |

Thirteen sources, ten of them live. Three are `UNCONFIGURED` **and say why**:
PHMSA restricts programmatic access to pipeline centrelines, Tier II filings are
confidential under EPCRA, and San Francisco publishes no open hydrant dataset.
The console renders that reason verbatim, because "the feed is down" and
"withheld by statute" are different statements about the same empty result.

Two of the ten need a key — Google Solar and NREL. Without it they report
`UNCONFIGURED` rather than falling back to a fixture, so a live-mode process
never serves synthetic records under a live label.

**Measured height is a subtraction.** No public digital surface model answers
"how tall is this building" for San Francisco, so the Geometry Watcher takes the
Solar API's roof-plane height and subtracts USGS 3DEP's ground elevation at the
same coordinate. The resulting fact cites **both** readings, because a
subtraction that cites one operand is a number nobody can check. A difference
below one storey is refused outright rather than recorded as a short building —
a structure of height zero would be one storey with a collapse zone computed
from nothing.

Live mode never falls back to a fixture. A source that is down produces an
explicit `UNAVAILABLE` fact naming it — never an empty result that reads as "no
hazard here".

## The incident loop

```
CAD ──▶ open ──▶ grant ──▶ ONE snapshot ──▶ instant brief ──▶ log ──▶ SSE
                                               (no model)      │
                                                        persist-before-transmit
        enriched (Gemini prose, COAL WAS WEALTH) ──────────────┤
        amendments (EMS-derived · weather · thermal · IC) ─────┘
                                                                │
                        close ──▶ revoke grant ──▶ seal log ──▶ NERIS draft
```

**Three stages, and only one of them can be slow.**

| Stage | Model | Contains | If it fails |
|---|---|---|---|
| Instant | none, by construction | construction, storeys, conflicts, collapse zone, occupancy, suppression, unknowns | it cannot: nothing on its path can be down |
| Enriched | optional Gemini | the same sections plus prose, in size-up order | the deterministic brief stands, narrative marked unavailable |
| Amendment | none | late data, marked as an amendment | earlier stages are already on screen |

**Prose streams; facts do not.** The enriched stage emits the narrative as the
model writes it, over `narrative` SSE frames marked `provisional`. Those frames
carry no facts, no version, and no content hash, and the incident log does not
store them — three seconds of nothing looks exactly like three seconds of
broken, and that is the only problem streaming solves here. Every such stream
ends with an authoritative `brief` frame: the persisted emission, or, if the
composition was refused or timed out, one whose narrative is absent and marked
unavailable. There is no path where provisional prose is left standing on a
screen with nothing behind it.

`BriefEmission` refuses to be constructed with `model_invoked=True` at the
instant stage, so "no model call" is enforced by the type rather than by
discipline.

**Persist before transmit.** `require_persisted()` raises unless the incident
log already holds the emission and its content hash. The SSE stream calls it on
every frame, so a frame that somehow reached the transport unpersisted raises
rather than being shown. Reconnect is by `Last-Event-ID`, replaying the stored
emissions in order — so a resumed stream shows what the original one sent.

**Sensor fusion.** Thermal frames register to Alpha–Delta faces. A face with no
current frame is `UNSCANNED`, coverage lapses rather than holding a stale
reading, and every rendering carries the sentence that thermal measures surface
temperature and cannot see through walls. Void detection is a fixed threshold
over adjacent regions — an observation, never a claim about what is behind the
wall.

**Notification versus commitment.** Telling the water department is autonomous.
Cutting gas, closing a road, committing hazmat or collapse rescue is
`REQUIRE_APPROVAL` — and that line is drawn by `PolicyEngine.decide`, not by the
endpoint or the UI.

**The truss timer** states a published UL/NIST test window and the elapsed time
since dispatch, side by side, with the disclaimer built into the rendering. It
is not a collapse prediction, and there is no code path that renders the numbers
without the caveat.

## Absence states

| State | Means | Never rendered as |
|---|---|---|
| `UNKNOWN` | no record found | "none present" |
| `UNAVAILABLE` | source unreachable / breaker open | "none present" |
| `WITHHELD` | statute or aid agreement | silently dropped |
| `UNSCANNED` | surface has no sensor coverage | "cool" |
| `CONFIRMED` | filed or measured, sources agree | — |
| `DISPUTED` | sources disagree | averaged or resolved away |

## Error envelope

Every failure leaves the API in one shape:

```json
{"error": {"code": "STALE_VERSION", "message": "...", "details": {},
           "request_id": "req_...", "correlation_id": "corr_..."}}
```

Domain errors carry their own HTTP status (`StaleVersionError` → 409,
`IdempotencyMismatchError` → 409, `ClassificationViolationError` → 403).
Unhandled exceptions return `INTERNAL_ERROR` and never leak their message.

## Observability

`request_id`, `correlation_id`, and `causation_id` are context variables bound by
middleware and stamped on every log line. Redaction runs at construction on both
key names and value patterns, so a stray `extra={"document_text": ...}` cannot
leak a citizen record. Bucket URIs, API keys, JWTs, emails, phones, and SSNs are
scrubbed by pattern.

## Lifecycle

Liveness (`/healthz`) and readiness (`/readyz`) answer different questions. On
SIGTERM the lifespan hook drains readiness first so the load balancer stops
routing, then in-flight work finishes. Both containers run non-root and honour
`PORT`.

## What the system will never do

No tactical recommendations, offensive/defensive calls, crew assignments,
evacuation orders, or fire-behaviour prediction. The collapse zone is the
standard 1.5× height convention applied to a measured height; the structural
timer states a published material property and elapsed time since dispatch.
Neither predicts anything about this fire.
