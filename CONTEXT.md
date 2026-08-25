# TERSAGE — technical context

Municipal structural intelligence for a fire department, built as an
institutional agent fleet.

---

## The problem

A crew arriving at a structure fire has roughly ninety seconds of usable
decision time before entry. What they need to know about the building — how it
was built, what has been altered, whether the record is trustworthy, what is
stored inside — already exists. It exists in a permit portal, an assessor's
roll, an inspections database, a violations log, and a federal hazard registry,
none of which talks to the others and none of which can be queried usefully
under time pressure.

No system can cold-query ten municipal and federal feeds fast enough to matter.
So the work has to have happened already.

## The thesis

Build the knowledge continuously, in the months when nothing is burning, and
have the answer ready when the call comes in.

That is a two-loop architecture. A slow loop accumulates a provenanced profile
of every structure in a district. An incident loop reads that profile the moment
a dispatch arrives, and streams what it says to the incident commander.

The interesting engineering is not the retrieval. It is the epistemics: what
happens when two official records disagree, how a fact proves where it came
from, and what a system says when it does not know.

---

## The slow loop

Runs continuously against records. Five agents.

**`records-watcher`** (120s budget) polls permits, the assessor's roll, fire
inspections, violations and parcels. Each document is screened, then triaged,
then read for typed values bound to the character spans that support them. A
permit that cites a prior permit not yet published becomes an open question
rather than a silent gap.

**`hazard-watcher`** (180s) reads EPA FRS, PHMSA, NREL and Tier II registries
into classified hazard facts. Its hard problem is entity resolution: whether
`ACME PLATING INC` in a federal registry is the facility at this parcel.

**`geometry-watcher`** (300s) derives roof geometry, building height and the
collapse zone from the Google Solar API and USGS elevation. Height is a
subtraction — roof plane minus ground datum — and a difference that is
implausible produces no height at all rather than a wrong one.

**`structure-watch`** (60s) reads the district once, at one instant, and does
two things from that single reading: runs the deterministic conflict rules, and
ranks structures for survey. Four weighted signals — open conflict severity
0.40, confidence decay 0.25, source churn 0.20, survey age 0.15. Every row cites
the rule that fired, the facts behind it, and its weight. A queue entry cannot
be constructed without at least one such reason.

**`referral-clerk`** (60s) drafts an inter-agency referral from the worst open
conflict at a structure, and files it once a captain approves.

## The incident loop

Wakes on a 911 call or a CAD dispatch. Four agents.

**`incident-interceptor`** (6s) is the head. It reads the caller's narrative,
opens the incident against one profile snapshot, streams the brief, and routes
the incident to the other agents by their declared capabilities. It then writes
a focus to the incident log: per-agent pointers, each carrying an id and a
reason, never a value. The other agents read it and act on the ids it names.

**`sensor-fusion`** (2s) registers thermal frames to building faces, resolved
from the footprint geometry rather than from the model's guess about which wall
it is looking at. A face with no frame is `UNSCANNED`, not cool.

**`agency-notifier`** (5s) notifies mutual-aid, utility and emergency-management
partners of conditions. Utility shutoff and road closure wait for a chief.

**`incident-recorder`** (15s) writes the append-only incident log through to the
records system and drafts a NERIS-shaped report. It also closes questions the
slow loop opened, when the incident answered them.

## The brief

Three stages, and the first one is the argument.

1. **Instant.** A deterministic template over stored facts, 500ms budget. No
   model call — `BriefEmission` refuses to construct with `model_invoked` set on
   this stage. Construction type, storey count, unresolved conflicts, collapse
   zone, occupancy, suppression status. If every model in the system is down,
   stage one still lands.
2. **Enriched.** Composed prose over the same facts, streamed.
3. **Amendment.** A late source folding in, marked as an amendment so a
   commander knows the brief changed and why.

---

## The data model

**A fact** carries a canonical key, a value, a source type, a source reference, a
snapshot id, an observed time, a confidence, and — where it came from prose — the
character span in the document that supports it. Facts are immutable. A
correction is a new fact that supersedes an old one; both remain, because an
investigation two years later needs to see what was believed at the time.

**Merge precedence** is absolute and checked before recency or confidence: a live
observation outranks a remote measurement, which outranks a filed record. A
human survey outranks all of them.

**Three states.**

- `UNKNOWN` — nothing settled it. Carries the sources that were checked.
- `DISPUTED` — two sources disagree. Carries both facts. Never averaged.
- `UNAVAILABLE` — a source could not be reached, named. Not an absence of hazard.

None of these is an empty field, and the distinction between them is the point.

**Confidence decays.** A fact loses confidence over time on a half-life set by
its source tier, so "the permit said two storeys in 2019" is not treated as
current knowledge in 2026. A filed record ages faster than a measurement.

## Governance

**The gateway.** Every read and write the fleet performs decides at a
default-deny policy engine. Ten rules, evaluated in order: expired grant,
missing scope, operation outside scope, a standing grant reaching a person,
incident binding, jurisdiction, PHI derivation, approval threshold, then the two
allow rules. Every decision is recorded with the rule that produced it.

**PHI is derived, never released.** A request that would return prior-EMS detail
returns a derived signal instead — that a condition exists at this address, not
what it is or whose it was.

**Agent identity.** Each agent runs as its own service account with only the
roles its declared scopes imply. No agent can impersonate another. The IAM
policy is generated from the agent descriptors, and a conformance test fails if
the two drift.

**Human approval where an action reaches outside the department.** A captain
files a referral, because filing accuses a property owner of unpermitted
construction. A chief approves a utility shutoff, because closing a gas main is
an irreversible physical act affecting a neighbourhood. Both gates are enforced
at the decision and again at the write.

**Model Armor plus a local detector.** Every ingested document passes two screens
with different failure modes. A screen that cannot run withholds the document
from the model rather than passing it through.

## Memory

The memory bank is two stores. The **record** — everything a thread has ruled
out, what it rests on, how many passes examined it, and every transition it made
— is Firestore, because that is a state machine with invariants. The **prose** of
each question is mirrored into Vertex AI Agent Engine Memory Bank, which gives
recall a second question it can answer: not only *what is this district
carrying*, but *has anyone asked something like this*, by meaning.

The split is a measurement, not a preference: a `Memory.fact` in that service is
capped at 2048 characters, which a question's bounded prose fits comfortably and
a long-running thread's accumulated eliminations do not. The index is never the
record — every match is read back from Firestore and re-gated on the caller's
scopes before it is returned, so an index that is stale, degraded or absent
costs findability and never correctness. `PHI` and Tier II prose never reach it
at all, because writing a memory embeds it.

The memory bank holds what a pass could not finish. An open question records
what an agent is trying to establish, what it is waiting for, what it has
already ruled out, its confidence, and when to give up.

Municipal records arrive weeks or months late. A permit filed in March may
publish in June. Without durable working memory, an agent re-reads the same
document every pass and fails the same way; nothing accumulates.

Recall is the security boundary. Every memory carries a classification, and
`recall` cannot be called without the caller's scopes — a question raised by a
confidential filing is invisible to an agent that lacks the scope for it. The
same gate covers direct reads and checkpoint resumption, because a back door
that returned what the front door refused would not be a boundary.

A LangGraph graph that exhausts its budget checkpoints into the bank and opens a
question. The next pass resumes rather than restarting. `ABANDONED` to `RESOLVED`
is a legal transition: that is the case the component exists for, where a filing
arrives two months after everyone stopped waiting.

## Reasoning

Six agents run LangGraph graphs on Gemini through Vertex AI. A graph decides
what to look up, what to cross-check, and when it is done. The nodes and the
router are ordinary code; LangGraph is the executor, and a built-in driver runs
the identical node set when it is not used.

The rule that shapes all of it: **a model may route, resolve, compose and point,
but may not author a fact.** Every value a graph ultimately writes goes through
the deterministic path with its span binding, provenance and confidence intact.

Search grounding follows the same line. It may decide what a reference points
at, because the output is an id chosen from candidates the caller already knows.
It may not decide what is true about a building.

Every graph node emits an OpenTelemetry span carrying its name, its decision and
counts — never document content. The trace is the reasoning chain, and it is
recorded as the replayable unit, because a graph with a variable step count
cannot be replayed one model call at a time.

---

## Architecture

Ports and adapters. Sixteen ports, one per seam:

```
audit  bus  city  clock  fireactivity  grounding  imagery  memory
model  office  repositories  runtime  sources  vectors  vision  writes
```

Nine adapter packages — `memory`, `fake`, `firestore`, `pubsub`, `google`,
`vertex`, `resend`, `nasa`, `clock`. Nearly every port has two implementations,
and one contract suite holds both to the same behaviour.

```
backend/src/firstdue/
  domain/         models, invariants, deterministic engines
  ports/          the seams
  adapters/       memory, fake, firestore, pubsub, google, vertex, resend, nasa
  agents/         slow-loop agents, and graphs/ for the reasoning
  incident/       controller, interceptor, fusion, recorder, focus, session
  gateway/        default-deny policy, PHI derivation, jurisdiction
  registry/       agent descriptors, versioning, topic routing
  services/       memory bank, grounding, grants, materialization, replay
  sources/        source framework: caching, limits, snapshots, backfill
  extraction/     screening, triage, typed extraction with spans
  security/       document screens, signed callbacks, request limits
  reliability/    failure classification, derived backoff, breakers
  observability/  structured logs, traces, redaction
frontend/         Next.js 14 console
infra/terraform/  13 modules, staging and prod
```

**Fake mode is the default.** The fake and in-memory adapters implement the same
interfaces, authorization rules, idempotency behaviour, event ordering and
failure modes as the live ones. The entire fleet, gateway and console run with
no Google credentials, which makes the credential-free demo a rehearsal rather
than a mock.

**Replay.** Every run records the agent versions, policy versions and source
snapshots that produced it. Model responses are recorded and replayed. The demo
seed rebuilds to the same content hash every time.

## Deployment

Thirteen Terraform modules across staging and production. Eleven Cloud Run
services: the slow loop, the incident loop, and nine per-agent workers, each on
its own service account. Pub/Sub carries dispatch fan-out and agent completion,
with a dead-letter topic per subject. Firestore holds incident state, building
profiles, the audit log, policy decisions, the agent registry and the memory
bank. Secrets are containers in Secret Manager; values are added out of band and
never appear in Terraform.

Each service verifies a stable Cloud Run custom audience rather than a generated
URL, so an identity survives a service being recreated. The Firestore index and
IAM policy are derived from the code, and the conformance suite fails if they
drift.

## Stack

Python 3.12, FastAPI, Pydantic v2, uv. LangGraph and LangChain on
`langchain-google-vertexai`. The Google Gen AI SDK for direct model calls.
OpenTelemetry, pytest, Ruff, strict mypy. Next.js 14 App Router with TypeScript
and three.js for the massing model. Terraform, Docker, Cloud Run.

## External integrations

| Service | Used for |
|---|---|
| Vertex AI — Gemini 3.5 Flash, Gemma | Extraction, composition, triage, frame reading |
| Google Search grounding | Reference resolution, local fire reports |
| Model Armor | Inline screen on ingested documents |
| Vertex Vector Search | Semantic recall over screened narratives |
| Vertex AI Agent Engine Memory Bank | Semantic recall over open question threads |
| Firestore, Pub/Sub, Cloud Run, Secret Manager, Cloud Storage | State, events, execution, credentials, artifacts |
| Google Solar API | Roof geometry, pitch, plane height, array detection |
| Street View Static, Maps Static | Building imagery beside the massing model |
| NASA FIRMS | Regional satellite fire activity |
| NASA POWER | Fire-weather context |
| Resend | Delivering an approved referral |
| SF open data | Permits, assessor, inspections, violations, parcels |
| EPA FRS, USGS 3DEP, NREL, NWS | Federal hazard, elevation, EV charging, weather |

Thirteen sources are catalogued and ten have live endpoints. The three without
one are catalogued with the reason — a restricted feed, a confidential filing, a
dataset the city does not publish — and the console renders that reason rather
than an empty panel.

## The console

One screen, two arrangements.

In standby it reads as a platform at work: the district's vital signs across the
top, the fleet spread across the width with a regional fire-activity map in the
middle, and each agent carrying its own visual and a terminal of what it did.
Slow-loop passes run on their own, so the fleet ticks over.

On dispatch the page reorganises. Incident agents to the left, the massing model
and a photograph of the building in the middle, the slow loop to the right —
still there, still in full cards, because it did not stop.

## Verification

1,516 backend tests and 286 console tests. Strict mypy across 187 source files.
Ten architecture decision records. A contract suite that holds the in-memory
and Firestore backends to one set of behaviours, an infrastructure suite that
holds Terraform to the agent descriptors, and an observability suite that
asserts telemetry carries no document content.

Apache-2.0. No real person's records appear anywhere in this project.
