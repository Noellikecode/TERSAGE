# Tactical Evidence, Records, Structural Attributes & Geospatial Evaluation: T.E.R.S.A.G.E



Municipal structural intelligence for a fire department, built as an
institutional agent fleet. **Nine scheduled agents across three publishing
departments, two loops, one governance layer** — cataloged in Google Cloud
Agent Registry, running on Cloud Run, and reasoning on Gemini 3.5 Flash through
the Google Gen AI SDK against Vertex AI.

*Submitted to the Fortified Enterprise Fleet track.*

A crew arriving at a structure fire has about ninety seconds of usable decision
time before entry. What they need to know about the building already exists — in
a permit portal, an assessor's roll, an inspections database, a violations log, a
federal hazard registry. None of those systems talks to the others, and none can
be queried usefully under time pressure.

No system can cold-query ten feeds fast enough to matter. So TERSAGE does the
work in the months when nothing is burning, and has the answer ready when the
call comes in.

---

## Run it

No credentials, no cloud account, no API keys.

```bash
git clone https://github.com/noelthomas-dev/TERSAGE.git
cd TERSAGE
make setup        # Python 3.12 via uv, plus the console toolchain
make demo         # API on :8000, console on :3000
```

Open `http://localhost:3000`. The console opens on a district the slow loop has
already surveyed — profiles built, conflicts found, the structures whose records
disagree named one card each — then a 911 call arrives on its own and the screen
reorganises into the incident view.

Every port has a credential-free implementation with the same authorization
rules, idempotency behaviour, event ordering and failure modes as the
Google-backed one, so the whole fleet runs on a laptop.

| Command | What it does |
|---|---|
| `make demo` | API and console, credential-free |
| `make slow-loop` | One complete slow-loop pass over a district, printed |
| `make verify` | Lint, types, tests, OpenAPI, console build, secret scan |
| `make reset` | Deterministic demo state, same content hash every time |
| `make infra-check` | Terraform format, validate, and conformance |

### Deploy to Google Cloud

```bash
gcloud auth application-default login
./infra/bootstrap.sh                      # one-time: Terraform state bucket
cp infra/terraform/envs/staging/terraform.tfvars.example \
   infra/terraform/envs/staging/terraform.tfvars     # set project + billing
make infra-plan                           # read the plan before applying
PROJECT_ID=your-project make deploy-staging
STAGING_BASE_URL=<incident url> make smoke-staging
```

`deploy-staging` builds both images by digest, hands them to OpenTofu, and
applies. Secret *values* are added out of band with `gcloud secrets versions
add`; Terraform creates only the containers.

---

## The two loops

**The slow loop** runs continuously against municipal and federal records. It
turns filings into provenanced facts, detects where two official sources
disagree, ranks structures by whether a person needs to go and look, and drafts
inter-agency referrals for a captain to file.

**The incident loop** wakes on a 911 call or a CAD dispatch. It reads the
caller's narrative, opens the incident against one profile snapshot, and streams
a brief to the incident commander in three stages.

The slow loop does not stop when a fire starts.

---

## What each agent does

Nine agents are scheduled; thirteen are published. The four extras are
superseded and stay resolvable, because a brief recorded two years ago names the
agent version that produced it.

### Slow loop

**`records-watcher`** · 120s budget · writes no external system
Polls permits, the assessor's roll, fire inspections, violations and parcels.
Every document passes two injection screens before a model sees it. Gemma
triages — is this worth a Gemini call — then Gemini extracts typed values, each
bound to the character span in the document that supports it. A LangGraph graph
decides which feeds to read and follows references between filings: a permit
citing a prior permit number is chased rather than ignored. When the cited
filing is not in the published window, the agent opens a durable question
instead of failing silently.

**`hazard-watcher`** · 180s · writes no external system
Reads EPA FRS, PHMSA, NREL and Tier II registries into classified hazard facts.
Its hard problem is identity: whether `ACME PLATING INC` in a federal registry
is the facility at this parcel. A LangGraph graph pulls a candidate record,
notices when the identity is ambiguous, queries a second registry to
disambiguate, and loops until it is confident or out of budget — recording what
it ruled out so the next pass does not repeat the work.

**`geometry-watcher`** · 300s · writes the pre-incident plan store
Derives roof geometry, building height and the collapse zone from the Google
Solar API and USGS elevation. Height is a subtraction — roof plane minus ground
datum — and a difference that is physically implausible produces no height at
all rather than a wrong one. Renders an NFPA-1620-shaped pre-incident plan to
Cloud Storage.

**`structure-watch`** · 60s · writes inspection work orders
Reads the district once, at one instant, and does two things from that single
reading: runs the deterministic conflict rules, and ranks every structure for
survey. Four weighted signals — open conflict severity 0.40, confidence decay
0.25, source churn 0.20, survey age 0.15. Every row cites the rule that fired,
the facts behind it and its weight; a queue entry cannot be constructed without
at least one such reason.

**`referral-clerk`** · 60s · **captain approval** · writes the building department
Drafts an inter-agency referral from the worst open conflict at a structure. The
letter prints the supporting fact ids so the receiving department can pull the
same documents, and states that it reports a disagreement rather than a code
violation. A model may improve the wording; a draft that drops a fact id,
invents one, or loses the no-determination sentence is rejected and the
deterministic template ships. On approval, Resend delivers it.

### Incident loop

**`incident-interceptor`** · 12s · the head of the loop
Reads the 911 or CAD narrative, opens the incident against one profile snapshot,
streams the three-stage brief, and routes the incident to the other agents by
their declared capabilities. It then writes a **focus** to the incident log:
per-agent pointers, each carrying an id and a reason, never a value. Caller says
third floor, the permit says two storeys, lidar measured three, and a conflict
has been open since March — the head points every downstream agent at exactly
those ids.

**`sensor-fusion`** · 12s
Gemini multimodal reads a thermal or optical frame and returns observations
bound to image regions. Which wall it is looking at is resolved from the
footprint geometry, not from the model — a model that could name the wall could
name it wrong, and a temperature painted onto a side nobody photographed reads
as coverage. A face with no frame is `UNSCANNED`, never cool, and coverage
lapses when a frame ages out.

**`agency-notifier`** · 5s · **chief approval on utility shutoff**
Notifies mutual-aid, utility and emergency-management partners of conditions,
matching what the profile knows to who needs to hear it: a rooftop solar array
means a live DC circuit crews cannot de-energise from the panel; a transmission
pipeline inside the collapse zone is a different call, more urgently. Each
partner gets its own draft. Notifying is autonomous; closing a gas main is not.

**`incident-recorder`** · 15s · writes the records system
Runs after the incident closes, so nothing waits on it. Writes the append-only
incident log through to RMS and drafts a NERIS-shaped report. It also **closes
questions the slow loop opened months earlier** — when crews physically stood in
the building and the record now answers what the filings could not.

### Shared services

**Memory Bank** — durable open questions and graph checkpoints, in two stores.
The record is Firestore; the prose of each question is mirrored into Vertex AI
Agent Engine Memory Bank so recall can also answer *has anyone asked something
like this*. Recall is gated on the caller's scopes, against the record — never
against the index.
**Grounding service** — resolves a fuzzy external reference to a canonical id,
or declines. Google Search grounding, with citations.

---

## Enterprise platform components

| Component | How it is built |
|---|---|
| **Agent Registry** | `registry/descriptors.py` publishes every agent with its version, scopes, write targets, capabilities and latency budget. Departments subscribe to what they are authorized to run, pinned to a version. Cross-department: fire publishes the structural agents, building publishes the permit agent, county emergency management publishes the hazmat agent. |
| **Agent Runtime** | Grants, scopes and deadlines enforced around every run; every run reaches a terminal state. Twelve Cloud Run services: the slow loop, nine per-agent workers and the console. The workers scale to zero between passes; the incident service keeps one instance warm, because a cold start on dispatch is the one latency this system exists to avoid. A LangGraph graph that exhausts its budget checkpoints and resumes on a later pass. |
| **Memory Bank** | Adopted for the half it fits, and only that half. Vertex AI Agent Engine Memory Bank holds each open question's prose and serves semantic recall over it; the record — eliminations, evidence, examination counts, transitions, checkpoints — stays in Firestore, because a `Memory.fact` caps at 2048 characters and a long-running thread's eliminations do not fit. Questions outlive a pass, a restart and a scale-to-zero: one opened in March is closed in August by the incident that answered it. Recall is scope-gated against the stored record, so a match the index offers for a confidential thread still never reaches an agent without the scope. Five behaviours of the managed service turned out otherwise than its SDK implied; `scripts/verify_memory_bank.py` is what found them and is what keeps them found. |
| **Agent Identity** | Each agent runs as its own service account with only the roles its declared scopes imply. No agent can impersonate another. The IAM policy is generated from the descriptors, and a conformance test fails if the two drift. |
| **Agent Gateway** | Every read and write decides at a default-deny policy engine — ten rules in order, including PHI derivation and jurisdiction. Every decision is recorded with the rule that produced it. |
| **Model Armor** | Two screens with different failure modes in front of every ingested document. A screen that cannot run withholds the document from the model rather than passing it through. |
| **Agent Observability** | OpenTelemetry traces, structured logs, and an append-only audit log that never holds document contents. Each graph node emits a span carrying its name, decision and counts — the reasoning chain, recorded as the replayable unit. |

---

## The epistemics

Three states, and none of them is an empty field.

- `UNKNOWN` — nothing settled it, with the sources that were checked.
- `DISPUTED` — two sources disagree, with both facts intact. Never averaged.
- `UNAVAILABLE` — a source could not be reached, named. Not an absence of hazard.

Every fact carries its source, its snapshot, when it was observed, a confidence,
and the span in the document that supports it. Facts are immutable; a correction
is a new fact that supersedes an old one and both remain. Confidence decays on a
half-life set by source tier, so a 2019 permit is not treated as current
knowledge.

**The model may not author a fact.** Models extract typed values bound to spans,
route documents, compose prose and resolve references. Deterministic code decides
what is true, what conflicts, and what gets ranked.

**A human approves anything that reaches outside the department.** A captain
files an inter-agency referral, because filing accuses a property owner of
unpermitted construction. A chief approves a utility shutoff, because closing a
gas main affects a neighbourhood. Both gates are enforced at the decision and
again at the write.

**The first brief stage contains no model call.** `BriefEmission` refuses to
construct with `model_invoked` set on the instant stage. Construction type,
storey count, unresolved conflicts, collapse zone, occupancy, suppression status
— 500ms, deterministic. If every model in the system is down, stage one lands.

---

## How weeks of analysis become ninety seconds

This is the whole argument, and it is why the fleet is shaped the way it is.

**Months before the fire.** Nine agents work a district on their own schedule.
`records-watcher` polls permits, the assessor's roll, inspections and
violations; every document passes two injection screens, Gemma decides whether
it is worth a Gemini call, and Gemini extracts typed values each bound to the
character span that supports it. `geometry-watcher` derives roof geometry and
height from the Solar API and USGS elevation — a subtraction, not a guess, and a
physically implausible difference produces no height rather than a wrong one.
`hazard-watcher` resolves federal registries against this parcel, looping until
it is confident or out of budget and recording what it ruled out so the next
pass does not repeat the work. `structure-watch` runs deterministic conflict
rules over everything they filed and ranks the district for survey.
`referral-clerk` drafts the letter a captain files.

**The record accumulates, provenanced.** Every fact carries its source, its
snapshot, when it was observed, a confidence that decays on a source-tier
half-life, and the span in the document behind it. Disagreements are kept as
disagreements. Questions the record could not answer stay open in the Memory
Bank for months, across restarts and scale-to-zero.

**Then the call comes in.** The incident loop does not query anything. It reads
*one immutable snapshot* of work already done. Stage one of the brief contains
**no model call at all** — `BriefEmission` refuses to construct with
`model_invoked` set on the instant stage — so construction type, storey count,
open conflicts, collapse zone, occupancy and suppression status land in
milliseconds even if every model in the system is down.

**And the whole loop is budgeted backwards from the promise.**

    93 s  compose deadline      the latest a composition may start
  + 20 s  composition cap       the interceptor's own latency_target_ms
  +  7 s  delivery allowance    poll interval + route draw + read pause
  ------
    120 s hard ceiling          a card on a commander's screen

Every term on the left is a cap something already enforces, so the sum is a
bound rather than a hope. `COMPOSITION_CAP` is read off the agent catalog at
import, and `COMPOSE_DEADLINE` is solved from it — change a descriptor's budget
and the deadline moves rather than the promise breaking.

---

## Every technology, and what it does here

**Google AI**

| | |
|---|---|
| **Gemini 3.5 Flash** (Vertex AI) | Typed extraction bound to source spans; enriched-brief and crew-brief prose; the multimodal read of a thermal frame; reference grounding |
| **Gemma 4 26B** (`gemma-4-26b-a4b-it-maas`, Vertex AI) | Document triage only — the cheap model decides whether a document is worth a Gemini call, never what it says |
| **Google Gen AI SDK** (`google-genai`) | The agent framework every model call goes through, constructed `vertexai=True` so one project, location and service-account identity govern all of them |
| **Vertex AI Agent Engine — Memory Bank** | Semantic recall over the prose of open questions that outlive a pass, a restart and a scale-to-zero |
| **Model Armor** | Inline guardrail on every ingested document, paired with a local injection detector |
| **Vertex AI Vector Search** | Wired behind the vectors port; off by default on cost (`vector_search_enabled`) |
| **Google Search grounding** | Resolves a fuzzy external reference to a canonical id, with citations, or declines |

**Google Cloud infrastructure**

| | |
|---|---|
| **Cloud Run** | 12 services — slow loop, incident loop, console, and nine per-agent workers, each on its own service account |
| **Firestore** | 32 collections: append-only facts, profiles with optimistic concurrency, the agent catalog, grants, policy decisions, audit log, idempotency records, fenced locks |
| **Pub/Sub** | Dispatch fan-out and agent completion, a dead-letter topic per subject, push to authenticated Cloud Run endpoints |
| **Cloud Storage** | NFPA-1620-shaped pre-incident plans |
| **Secret Manager** | Containers created by Terraform; values added out of band |
| **Cloud Scheduler** | The slow loop's cadence |
| **Cloud Trace** + **OpenTelemetry** | A span per agent run and per graph node — the reasoning chain as the replayable unit |
| **Cloud Billing Budgets** | A hard spend ceiling with alerting |
| **Artifact Registry** + **Cloud Build** | Images built and deployed **by digest**, never by tag |
| **Google Cloud Agent Registry** | 9 agents published as A2A agent cards for cross-department discovery |
| **IAM** | Per-agent service accounts whose roles are *generated from* the declared scopes |

**Google data & imagery**

Solar API (roof geometry) · USGS 3DEP (ground datum) · Photorealistic 3D Tiles · Street View / Static Maps · Google Calendar and Gmail (survey scheduling, behind a `WORKSPACE_WRITES` switch)

**Non-Google external data**

DataSF permits, assessor, inspections, violations, parcels · EPA FRS · PHMSA pipelines · NREL EV infrastructure · Tier II · NASA FIRMS (regional fire activity) · NASA POWER (fire weather) · Resend (inter-agency referral email)

**Backend**

Python 3.12 · FastAPI · Pydantic v2 + pydantic-settings · Uvicorn · SSE-Starlette (streaming brief) · httpx · tenacity · **LangGraph** + langchain-core (graph *executor* for the reasoning agents — the nodes and router are ordinary code, and a built-in driver runs the identical set when the package is absent) · langchain-google-vertexai (the planner's one job: choose the next lookup from a closed list)

**Console**

TypeScript · Next.js · React · Tailwind · **Three.js** (the measured structure model) · **deck.gl** (regional heat map) · 3d-tiles-renderer (Photorealistic 3D Tiles)

**Infrastructure & quality**

OpenTofu/Terraform (14 modules, 377 resources) · Docker · uv · Ruff · strict mypy (206 files) · pytest (1,807 tests) · Vitest (577 tests) · gitleaks · GitHub Actions

---

## The pipeline, end to end

**1 · Poll.** `records-watcher` reads DataSF permits, the assessor's roll,
inspections and violations over **httpx**, paging until the feed is exhausted or
its 40 s budget is spent. `hazard-watcher` reads EPA FRS, PHMSA, NREL and Tier
II. `geometry-watcher` reads the **Solar API** and **USGS 3DEP**. Snapshots land
in **Firestore** with a content hash.

**2 · Screen.** Every document passes **Model Armor** *and* a local injection
detector before a model sees it. A screen that cannot run **withholds** the
document — it does not pass it through.

**3 · Triage.** **Gemma 4 26B** decides whether the document is worth a Gemini
call. It can only ever *skip* work, and a broken triage answers "extract".

**4 · Extract.** **Gemini 3.5 Flash**, via the **Gen AI SDK** against Vertex AI,
returns typed values each bound to the character span that supports it. The
model may not author a fact — deterministic code decides what is true.

**5 · Materialize.** Facts append to a **Firestore** profile under optimistic
concurrency; contention re-derives rather than dropping. Confidence decays on a
source-tier half-life. `conflict.detected` publishes to **Pub/Sub**.

**6 · Reason.** `hazard-watcher` runs a **LangGraph** identity loop — pull a
candidate, notice ambiguity, query a second registry, stop when confident or out
of budget — recording what it ruled out. What it cannot settle becomes an open
question in **Vertex AI Memory Bank**, where it waits months.

**7 · Rank and refer.** `structure-watch` runs deterministic conflict rules and
ranks the district on four weighted signals. `referral-clerk` drafts a letter;
a **captain** approves before it is filed, and **Resend** delivers it.
`geometry-watcher` renders a pre-incident plan to **Cloud Storage**.

*— months pass —*

**8 · Dispatch.** A 911 call or CAD narrative arrives. `incident-interceptor`
opens the incident against **one immutable snapshot**, mints a scoped grant, and
emits brief v1 — **no model call**, milliseconds.

**9 · Intake.** Gemini reads the caller's narrative and binds reported values to
spans in the transcript; a **LangGraph** focus composer routes the incident to
the other agents by their declared capabilities, and recalls from Memory Bank.

**10 · Sweep.** `sensor-fusion` reads a thermal frame with **Gemini multimodal**
and returns observations bound to image regions. Which wall it is looking at is
resolved from the measured footprint — never from the model.

**11 · Notify.** `agency-notifier` drafts for mutual aid, utilities and
emergency management. Notifying is autonomous; a utility shutoff needs a **chief**.

**12 · Compose.** The interceptor assesses six readiness criteria, solves an
**A\*** entry path over a graph priced by what the sweep measured, and composes
the crew brief. Every step passes the **Agent Gateway** (default-deny) under the
agent's own **service-account identity**, and lands a span in **Cloud Trace**.

**13 · Approve.** The console raises the package over **SSE**, with the route
drawn on a **Three.js** model of the measured geometry. Two human signatures,
then release.

**14 · Close.** `incident-recorder` seals the append-only log, writes through to
RMS, drafts a NERIS-shaped report — and **closes the questions the slow loop
opened months earlier**, now that a crew has stood in the building.

---

## Architecture

Ports and adapters. **Eighteen ports**, one per seam; **ten adapter packages**.
Nearly every port has two implementations, and one contract suite holds both to
the same behaviour.

```
backend/src/firstdue/
  domain/         models, invariants, deterministic engines
  ports/          the eighteen seams
  adapters/       memory, fake, firestore, pubsub, google, vertex, resend, nasa
  agents/         slow-loop agents, and graphs/ for the reasoning
  incident/       controller, interceptor, fusion, recorder, focus, session
  gateway/        default-deny policy, PHI derivation, jurisdiction
  registry/       descriptors, versioning, topic routing
  services/       memory bank, grounding, grants, materialization, replay
  sources/        source framework: caching, limits, snapshots, backfill
  extraction/     screening, triage, typed extraction with spans
  security/       document screens, signed callbacks, request limits
  reliability/    failure classification, derived backoff, circuit breakers
  observability/  structured logs, traces, redaction
frontend/         Next.js 14 console
infra/terraform/  13 modules, staging and prod
```

**Stack.** Gemini 3.5 Flash and Gemma on Vertex AI, through the Google Gen AI
SDK. LangGraph and LangChain on `langchain-google-vertexai` for the reasoning
graphs. Cloud Run, Firestore, Pub/Sub, Cloud Storage, Secret Manager, Vertex
Vector Search, Agent Engine Memory Bank, Model Armor. Python 3.12, FastAPI,
Pydantic v2. Next.js 14,
TypeScript, three.js.

**External data.** SF open data (permits, assessor, inspections, violations,
parcels), EPA FRS, USGS 3DEP, NREL, NWS, Google Solar API, Street View, NASA
FIRMS, NASA POWER. Ten of thirteen catalogued sources have live endpoints; the
rest are catalogued with the reason they have none.

---

## The console

One screen, two arrangements.

**Standby** reads as a platform at work: the district's vital signs across the
top, the fleet spread across the width, a NASA FIRMS regional fire-activity map
in the middle. Each agent carries its own visual — a ranking weight bar, face
coverage quadrants, a fan-out glyph — and a terminal that types out what it just
did. Slow-loop passes run on their own, so the fleet ticks over.

**On dispatch** the page reorganises. The incident agents take both flanking
columns, the structure and a photograph of the building split the middle, and
the slow loop moves off screen while still running and saying so.

The two middle panels answer two different questions and never pretend to
answer each other's.

**Building imagery** is what the building looks like — street, aerial, and a
`3d` viewpoint that streams Google's Photorealistic 3D Tiles, framed by
inverting the east-north-up frame at the parcel's coordinates so the address
sits at the origin, with drag-to-orbit.

**Structure** is what the records say it *is*, generated in Three.js r128 from
the GeometrySpec alone: storeys at their filed heights, a window grid and a
doorway on the wall the backend labels Alpha, a gabled or hipped roof built
from the roof segments' own pitch and count, roof obstructions where the
records put them, the collapse zone at the 1.5× convention, and the disputed
storey drawn translucent with its outline picked out. It builds up level by
level and the drone sweep's heat map fades on after, and it orbits freely —
drag to rotate, scroll to zoom, shift-drag to pan, double-click to return to
the last named framing, with the ALPHA/BRAVO/CHARLIE/DELTA/ISO buttons jumping
to a wall an officer can call over the radio. Openings are regular
fenestration and the caption says so — no survey counted windows, and nobody
should read a window count off a picture.

---

## Fortified Enterprise Fleet — where each requirement is met

| Requirement | Where it lives |
|---|---|
| **Gemini 3.5+ via Vertex AI** | `gemini-3.5-flash` through `genai.Client(vertexai=True)`. Gemma (`gemma-4-26b-a4b-it-maas`) does document triage — the cheap model is allowed to decide whether a document is worth reading, never what it says. |
| **Google Agent Framework** | **GenAI SDK** (`google-genai`), used in `adapters/vertex/model.py`, `vision.py` and `grounding.py`. |
| **Google Cloud infrastructure** | Cloud Run (12 services), Firestore (32 collections), Pub/Sub with a dead-letter topic per subject, Cloud Storage, Secret Manager, Cloud Scheduler, Cloud Trace. |
| **Cataloged for cross-department use** | **9 agents published to Google Cloud Agent Registry** as A2A agent cards, plus the in-system catalog in `registry/descriptors.py`. Three publishing departments: fire publishes the structural agents, **building** publishes the permit agent, **county emergency management** publishes the hazmat agent. A department subscribes to a *pinned version* of an agent another department owns. |
| **Context across weeks of async operation** | Vertex AI Agent Engine **Memory Bank** holds open-question prose for semantic recall; the record — eliminations, evidence, examination counts, checkpoints — stays in Firestore because a `Memory.fact` caps at 2048 characters. A question opened in March is closed in August by the incident that answered it. |
| **Production data without violating compliance** | Default-deny **Agent Gateway**, PHI derivation, jurisdiction rules, Tier II confidential classification, scope-gated recall, and an audit log that never holds document contents. |
| **Zero-trust identity** | Each agent runs as its own service account carrying only the roles its declared scopes imply. The IAM policy is *generated from the descriptors*, and a conformance test fails if the two drift. |
| **Inline guardrails** | **Model Armor** plus a local injection detector in front of every ingested document. A screen that cannot run **withholds** the document rather than passing it through. |
| **OpenTelemetry observability** | Traces per agent run and per graph node, structured logs, an append-only audit log, and `/internal/audit/*` for the reasoning chain. |

---

## Lifecycle, versioning and safe change

The catalog is **append-only and immutable per version**. Republishing a
descriptor whose *content* changed under the same version is refused by a
Firestore transaction — `AppendOnlyViolationError`, "publish a new version
instead". That is not a nicety: a NIOSH line-of-duty-death investigation
reconstructs what a commander knew two years later, and every brief records the
agent versions that produced it.

So the fleet is at **`1.4.0`**, and each bump is a promise that changed:

- `1.1.0` — `sensor-fusion` 2 s → 12 s, `incident-recorder` gained `read:public-records`
- `1.2.0` — `incident-interceptor` 6 s → 12 s
- `1.3.0` — `incident-interceptor` 12 s → 20 s
- `1.4.0` — `records-watcher` 120 s → 40 s

Four superseded agents stay **published and resolvable** rather than deleted:
`brief-reconciler`, `conflict-detector`, `incident-controller`, `survey-ranker`.
Thirteen descriptors, nine scheduled. An `agent_id` that vanished from the
catalog would turn a recorded run into an unresolvable reference.

**Every budget in this system was measured, not picked.** One live
`gemini-3.5-flash` compose on this project costs 5.72–6.97 s. Budgets sized in
fake mode — where a model answers in microseconds — were cancelling every live
model call in the incident loop. The descriptors carry the measurements.

---

## Spin-up

**Locally, no credentials:**

```bash
make setup        # Python 3.12 via uv, plus the console toolchain
make demo         # API on :8000, console on :3000
```

Every port has a credential-free implementation with the same authorization
rules, idempotency behaviour, event ordering and failure modes as the
Google-backed one, so the whole fleet runs on a laptop.

**Against real Google Cloud:**

```bash
gcloud auth application-default login
cp .env.live.example .env.live                 # project, models, Model Armor template
make live-demo                                 # real Gemini, real feeds, real Firestore
```

**Deploy:**

```bash
./infra/bootstrap.sh                           # one-time: Terraform state bucket
make infra-plan                                # read the plan before applying
PROJECT_ID=your-project make deploy-staging
STAGING_BASE_URL=<incident url> make smoke-staging
python scripts/publish_agent_registry.py --project your-project
```

`deploy-staging` builds both images **by digest** — a rollback has to name the
exact image that was running, and `:latest` cannot — and hands them to OpenTofu.
Secret *values* are added out of band; Terraform creates only the containers.

---

## Verification

1,807 backend tests and 577 console tests. Strict mypy across 206 source files.
A contract suite that holds the in-memory and Firestore backends to one set of
behaviours, an infrastructure suite that holds Terraform to the agent
descriptors, and an observability suite that asserts telemetry carries no
document content.

`make verify` runs all of it, plus a secret scan over the full history.

Apache-2.0. No real person's records appear anywhere in this project.
