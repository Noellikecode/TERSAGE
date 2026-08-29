# TERSAGE — technical context

Municipal structural intelligence for a fire department, built as an
institutional agent fleet.

**Status, 2026-08-29.** `main` at `e9a696f`, working tree clean. 1,796 backend
and 575 console tests pass; mypy is clean across 206 source files, and Ruff, the
console build, Terraform validation and `tofu fmt` are clean — see
[Verification](#verification). The fleet publishes at **`FLEET_VERSION 1.2.0`**;
two content changes today each cost a version, which is the append-only
catalog working as designed — see [Fleet versioning](#fleet-versioning-1200).
Staging **is deployed** and healthy in project `firstdue-dev`: 377 Terraform
resources, 12 Cloud Run services, a public console, and — new today — the
billing budget that had never applied. It runs `1.1.0` images, so it is one
fleet version behind `main` and is missing today's second round of fixes. See
[What is actually deployed](#what-is-actually-deployed).

**The headline finding of 2026-08-29:** every model-bearing budget in the
incident loop had been sized against fake mode, where a model answers in
microseconds. Measured against the live Vertex endpoint this project actually
calls, one `gemini-3.5-flash` compose costs **5.72–6.97 s**. The interceptor's
whole-run cap was 6 s and its stage deadlines were 4–5 s, so **no live incident
had ever produced an entry package** — no crew brief, no optimal path — and the
sweep had never landed a single thermal frame. All of it passed locally. See
[The budgets were never measured](#the-budgets-were-never-measured-2026-08-29).

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

Five agents run LangGraph graphs on Gemini through Vertex AI —
`records-watcher`, `hazard-watcher`, `incident-interceptor`, `agency-notifier`
and `incident-recorder`. The other four are deterministic: `geometry-watcher`
measures, `structure-watch` scores, `sensor-fusion` registers frames to faces,
`referral-clerk` drafts from the worst open conflict. That split is worth
stating rather than blurring — `structure-watch` holds no model on purpose,
because a model that could invent a conflict could invent its absence. A graph decides
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

Ports and adapters. Eighteen ports, one per seam:

```
audit  bus  city  clock  fireactivity  grounding  imagery  memory  model
office  repositories  runtime  sources  threads  tiles  vectors  vision
writes
```

`threads` is semantic recall over open question threads, separate from `memory`
because it answers a different question — *has anyone asked something like this*
rather than *what is this district carrying*.

`tiles` is the newest. It is separate from `imagery` because a tile is not a
picture: `imagery` answers *what does this thing look like* and returns one
finished image with the box it covers, while a tile is one addressed square of
an infinite grid, meaningless without its neighbours, requested in hundreds as a
camera moves. Different cache lifetimes, different failure granularity — one
tile missing is a hole, not an outage — and a different shape on the wire.

Eight adapter packages — `fake`, `firestore`, `google`, `memory`, `nasa`,
`pubsub`, `resend`, `vertex` — plus `adapters/clock.py`, which is a module
rather than a package because a clock has no second seam to hide. Nearly every
port has two implementations, and one contract suite holds both to the same
behaviour.

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

Fourteen Terraform modules across staging and production. Twelve Cloud Run
services: the slow loop, the incident loop, nine per-agent workers — each on its
own service account — and the console. Pub/Sub carries dispatch fan-out and
agent completion, with a dead-letter topic per subject. Firestore holds incident
state, building profiles, the audit log, policy decisions, the agent registry
and the memory bank. Secrets are containers in Secret Manager; values are added
out of band and never appear in Terraform.

Each service verifies a stable Cloud Run custom audience rather than a generated
URL, so an identity survives a service being recreated. The Firestore index and
IAM policy are derived from the code, and the conformance suite fails if they
drift.

### What is actually deployed

Verified 2026-08-26 against the account, not read off this file. Earlier
revisions of this document, and of `docs/build-notes.md`, said nothing had ever
been deployed. That stopped being true on 2026-08-24.

**Staging is applied and healthy, in project `firstdue-dev`.** The env is named
`staging`; the project it targets is `firstdue-dev`, and those two names do not
match on purpose — `firstdue-test` exists only for the contract suite and has
Cloud Run disabled. State lives in `gs://firstdue-dev-firstdue-tfstate` under
prefix `firstdue/staging`, and holds **377 resources**.

| What | State |
|---|---|
| Cloud Run services | 12, all `Ready=True`, created 2026-08-24T23:47–2026-08-25T00:00 |
| Console | `https://firstdue-console-kaw7xwxu7a-uc.a.run.app`, public, 200 |
| Incident service | private; invokers are the console, `pubsub-push` and `ci-smoke` only |
| Firestore | `(default)`, native mode, `nam5`, 33 composite indexes |
| Pub/Sub | 17 topics, 17 dead-letter topics, 17 push subscriptions, 24 per-agent subscriptions |
| Service accounts | 9 agent, plus console, incident, slow, pubsub-push, scheduler, ci-smoke |
| Secrets | 5 containers (`callback-secret`, `google-maps-api-key`, `nrel-api-key`, `resend-api-key`, `socrata-app-token`); `live_source_keys = ["google-maps-api-key", "nrel-api-key"]`, and `callback-secret` is mounted by every service. Cloud Run refuses to start a container whose secret reference has no version, so the services being `Ready` is itself the evidence those versions exist |
| Model Armor | `projects/firstdue-dev/locations/us-central1/templates/firstdue-ingest` |
| Memory Bank | Agent Engine `4054090136877531136`, enabled |
| Vector Search | **off** (`vector_search_enabled = false`) — a running index endpoint is hundreds of USD a month; recall uses the lexical index |
| Grounding search | **off** (`grounding_search_enabled = false`), for per-pass cost |
| Budget | 50 USD with an email alert channel |

`/api/v1/system/status` through the deployed console returns `mode: live`,
`storage_backend: firestore`, `event_backend: pubsub`, `workspace_writes: fake`,
`published_agents: 13`.

**Three things are true about that deployment that a demo script must not gloss.**

1. **It is running code one commit behind `main`, and one fleet version
   behind.** Redeployed 2026-08-29 from the working tree at `520d34f`, so both
   images carry `FLEET_VERSION = 1.1.0` and the deployed catalog holds thirteen
   descriptors at `1.1.0` with `sub_fire_*` pinned to `1.1.0`. `main` is now at
   `e9a696f` and publishes `1.2.0`. What staging therefore **does not** have:
   the interceptor's 12 s run cap, the three raised model-stage deadlines, the
   derived `COMPOSITION_CAP`, the recorder's version fix, the records-watcher
   candidate cache, and the whole console pass (entry-package redesign, named
   timeouts, geometry-state panel). Staging can still compose no entry package
   for the reason set out under [Known gaps](#known-gaps); the fix is on `main`
   and not on the deployment.
2. **The deployed district is empty.** `seeded_profiles: 0`, and
   `/districts/sffd-district-03/stats` returns 0 profiles, 0 facts, 0 conflicts.
   Sources report `LIVE` with closed circuits. The seed is a local artefact and
   the deployed Firestore holds no profiles, so the console at that URL has
   nothing to show.
3. **The slow loop is not on a schedule.** `firstdue-staging-slow-loop` exists
   in Cloud Scheduler at `0 3 * * *` and is **PAUSED**, with no last-attempt
   time — it has never fired.

Also deployed-but-unconfigured: `FIRMS_MAP_KEY` is unset, so
`/districts/{id}/fire-activity` answers `available: false` with
*"NASA FIRMS needs a map key this process was not given; no fire detection
provider was contacted"*. That is the panel reporting a missing credential, not
an outage and not an absence of fire.

## Stack

Python 3.12, FastAPI, Pydantic v2, uv. LangGraph and LangChain on
`langchain-google-vertexai`. The Google Gen AI SDK for direct model calls.
OpenTelemetry, pytest, Ruff, strict mypy. Next.js 14 App Router with TypeScript,
three.js for the massing model and deck.gl for the regional heat map. Both are
loaded on demand rather than statically imported, so neither reaches the server
bundle and a browser that cannot run them gets a stated refusal instead of a
crash. Terraform, Docker, Cloud Run.

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
| Street View Static, Maps Static | Building imagery beside the massing model, street and aerial |
| Maps Static, dark-styled | The flat fallback ground plane, when the terrain mesh cannot be built. One cached image per region, fetched server-side and served as a data URI |
| Google Map Tiles API | The satellite skin on the terrain mesh. Session token and key both stay in the process; the console addresses tiles at this system's own origin |
| AWS terrarium (public domain) | RGB-encoded elevation for the terrain mesh. No credential, and proxied anyway so the console has one origin to talk to and one place where caching, rate limiting and the region check live |

Two settings decide where real data enters, independently of `USE_FAKE_AGENTS`,
because Maps Platform authenticates with an API key while Vertex uses
Application Default Credentials and one flag can express neither:

* `IMAGERY_PROVIDER=fake|google` — the building photograph. `google` without a
  key reports the refusal; it never falls back to the watermarked placeholder.
* `LIVE_SOURCES=` — comma-separated source ids polled live while the rest stay
  fixtures. `sf-parcels,google-solar,usgs-3dep` measures real geometry without
  taking Vertex, Firestore and every municipal record live in the same move. An
  id the catalog does not publish is a startup failure, not a silent no-op.

Both default to off, so `make demo` stays hermetic and reproducible.
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

In standby it leads with **what was found**, not with what found it. The
district's vital signs run across the top in both arrangements, above the mode
switch, because they are true in both. Under that, **three columns**, the same
skeleton an incident uses, so the screen does not change shape under an officer
at the moment a fire starts:

* **Left** — the slow-loop fleet.
* **Middle** — the subject. In standby that is the region, drawn; on dispatch it
  becomes the building. The selected structure opens *here*, under the map, and
  therefore never changes column when a call comes in.
* **Right** — what the fleet found, one card per question: `Regional fire
  activity` (counts, fire weather, the instrument's caveat) and `Records
  disagree` (one card per structure whose paperwork and measurement do not
  match, in the sentence the rule wrote: *the permit records 2 storeys; lidar
  DSM measures 3*).

All three are the same kind of object — a rounded, hairlined card on `surface`
with a ruled header carrying a name, a count and a line of plain English.
`components/standby/PanelCard.tsx` is that chrome, extracted so the fleet column
and the two findings cards cannot drift apart. Below `lg` it stacks, because
three columns at that width is one unreadable column.

**The survey ranking is not on this screen.** `structure-watch` still scores
every structure on every pass and the queue endpoint still answers — the ranking
is how the department decides where to send a company, and the reasons are
recorded. What it is not is a thing to read under time pressure: a rank, a score
and a band of structures tied on identical reasons asked an officer to act
differently on row 47 than on row 48, and nothing separates them. The reason a
structure is worth looking at survives, in words, in `Records disagree`. The
number stays in the record.

**The fleet is rows and one pane.** Nine agents as one line each — status, id,
one live number — and a single detail pane carrying whichever is selected: role,
publisher, pinned version, budget, its glyph, its reasoning terminal. Hover
previews, click pins so it holds while somebody talks over it, arrow keys reach
every row. Each row is a real target now: the rail was 320 fixed pixels of
six-point type, which left the fleet whispering down one edge and gave the pane
no room to answer a question about the agent selected. It is about a quarter of
the width in standby and a fifth during an incident, where it shares the flanks
with the brief — sized to be read, where the middle is sized to be looked at, so
the building is the largest thing on the display and the rails beside it are
still legible across a room. Drawing all nine at once put five screens of scroll on the page
before anything had happened, and made "which agent is working" the hardest
thing on it to find.

On dispatch the page reorganises into three columns. Incident agents to the
left, the building in the middle — the massing model beside its photograph,
street level or straight down, switchable — and the brief down the right. The
brief used to run the full width under the model, so a three-stage brief filling
in pushed the building it described off the top of the screen; in a column of
its own it grows downwards past nothing. The slow loop leaves the screen and
says so in a line of its own: it did not stop because a fire started, and an
officer should not have to guess.

**The flanks are chrome, the middle is the subject.** Both rails sit on
`surface`, the same tone as the header and the footer; the middle column keeps
`ground` and is set off by a hairline and a gutter rather than by anything
louder. One step on the existing three-tone scale, no new colour, and the eye
lands on the building instead of counting three identical panels.

### The regional heat map

**The SVG scatter came out; a 3D terrain map went in.** `e830390` deleted the
`Scatter` component — inline SVG over a linear lon/lat projection, detections as
circles sized by FRP — because with no coastline, no graticule and no
coordinates it was a rectangle nobody could place. `FireActivityMap` survived as
text and now lives in the right rail. The middle column carries
`RegionalHeatMap`, which answers the question the counts cannot: **how far from
us, and how big.**

**The ground is a mesh, not a plate.** A flat picture answers "where"; it does
not answer "which side of the ridge", and at a five-degree box that is most of
what terrain is for — ridgelines are what wind follows, what a fire runs up, and
what a crew has to drive around. deck.gl's `TerrainLayer` builds the mesh from
two tiled grids: public-domain terrarium elevation, and Google Map Tiles
satellite imagery draped over it. Both are proxied through
`/api/v1/terrain/{layer}/{z}/{x}/{y}`, so the browser talks to one origin and
the Maps key and tile session never leave the process. The proxy refuses any
square outside the configured region or outside its zoom range — without that it
is an open relay onto somebody else's metered quota, reachable by anyone who can
reach the console.

That route is deliberately **not** under `/districts`: the tile client is built
once from `FIRE_ACTIVITY_REGION`, which is a property of the process, and this
municipality's two districts share it. A district id there would have varied
nothing, and a decorative path parameter is a claim.

**Vertical exaggeration is ×8, and the key prints it.** The region is 550 km
across and its relief is under half a percent of that; true to scale the terrain
is a flat sheet and the mesh is pointless. The shape is real, the steepness is
not, and an unlabelled exaggeration is a claim about how steep the country is.

**What is drawn over it.**

1. **A continuous heat field**, weighted by radiative power — not a count, since
   ten smouldering pixels and one campaign fire are not the same event. It fades
   out rather than resolving to a cool colour: VIIRS does not report "cold", it
   reports nothing, and a blue periphery would be data where there is none.
2. **The district, and range rings at 25, 50 and 100 km.** The district marker
   is a hollow ring in the `live` blue, never the fire ramp, so it cannot read
   as a detection; the old panel's refusal to invent a *city marker* was about
   inventing activity, and that still holds.
3. **The six strongest clusters, ranked and numbered**, each openable for what
   the instrument reported there.

Everything above the mesh draws with `depthTest: false`. A heat field buried
inside a hillside is not a subtler rendering of the same fact — it is a fire you
cannot see.

**Clustering is greedy and power-weighted, not k-means.** A VIIRS pass lays a
fire down as a line of pixels along the scan, so a single fire arrives as a
scatter and six pins on it would read as six fires. k-means needs a *k* nobody
can justify and moves centres between renders, so a hotspot numbered 3 could
become 4 because a pixel arrived on the far side of the region. The cluster
centre is weighted by radiative power, so the marker sits on the energy rather
than on the middle of the scatter's bounding box. The cap on numbered pins is a
reading order, not a filter — everything else stays in the field and in the
totals.

**The hotspot card carries only what the feed said**: summed and peak radiative
power, peak brightness temperature, the detection count and its confidence mix,
day and night passes, distance from the district, and the last pass that saw it.
There is no risk score, no spread projection and no concern level, because a
five-day detection table does not support one and a number labelled that way
would be acted on as though it did.

**Brightness is a temperature, not an anomaly.** VIIRS reports how hot the pixel
radiated and ships no background to subtract, so anything phrased as "+8 °C above
normal" would be inventing the normal. `bright_ti4` is carried through as
`brightness_k` and printed as °C beside the kelvin it arrived in. `bright_ti5` is
in the feed and deliberately not read: two brightness temperatures on one
detection invite being differenced into an "anomaly", which is not what either
of them means.

Fire weather stays out of the card. It exists — NASA POWER reanalysis, in the
card beside this panel — but it is regional and days old, and inside a
per-hotspot card it would read as conditions measured *there, now*.

**Everything the picture claims is in the key**: the weighting, the relativity,
the exaggeration, the region total, the peak, the instrument's resolution note
read verbatim from the payload, and the Google attribution the licence requires.

Three failure states, none of them a blank rectangle: a browser with no WebGL2
says so; a renderer that fails to load says that instead of waiting forever on a
promise that already rejected; and with no Maps key there is no mesh, so it falls
back to the single flat basemap image — strictly worse and strictly honest, the
same ground without the shape.

In the deployment none of this draws, because no `FIRMS_MAP_KEY` is configured
and the panel says which credential is missing.

The `FireActivityMap` header docstring still describes the SVG projection it no
longer has -- stale since `e830390`, and worth fixing in the file.

## Fixed on 2026-08-25

Four defects found by auditing the running build, all verified against live
Google endpoints rather than reasoned about.

**`geometry-watcher` never measured anything outside fake mode.** Two bugs, and
between them the massing model was a constant while the caption said "measured
height" and derived a collapse zone — a distance a crew stands outside of —
from it.

1. *Point sources were asked as a district sweep.* The agent fetched parcels,
   Solar and 3DEP with no address. A fixture answers that in bulk; a point
   source refuses with `address_required` before a request is made, which is
   why the log showed `SOURCE_UNAVAILABLE` with no HTTP error behind it.
2. *Targets came from whatever a source attributed.* The live DataSF parcel
   feed returns rows keyed by block-and-lot with no address id, so the target
   list was empty against real data and full against a fixture. It now
   enumerates the department's own profiles.

   For `sf-0450-hayes`, seeded against measured: 2 roof segments → **11**;
   9.51 m → **16.30 m**; footprint 11.5 × 22 m constant → **14.4 × 27.6 m,
   the 398 m² Solar measured**; collapse zone 14.25 m → **24.45 m**. The
   permit says 2 storeys and the measurement now says 5.

   Where no parcel ring is attributable, the footprint is a rectangle of the
   measured roof area: the right *size*, no claim about shape, and better than
   a constant no source ever measured.

**One disagreement was rendered three times.** A conflict's id is derived from
the facts it cites, so an amended permit mints a new finding while the earlier
one — about a pairing nothing compares any more — stays `OPEN`. Only a human may
`RESOLVE` one, so nothing is closed or deleted: `BuildingProfile.current_conflicts`
returns the newest open finding per rule and attribute, and the record keeps
every finding for an investigator. Four call sites computed "open conflicts"
independently and now read one property. District count went 4 → 2.

**The fire-activity map never rendered.** It printed *"No bounding box
reported"* while holding one: the component read `bbox`/`region_bbox`/
`query_bbox` and the backend sends `region` and `city`. Fixed in `e830390`,
which then deleted the drawing anyway. The *panel* stays and is still on the
standby screen — see [The console](#the-console).

**`preincident-plan-store` still names the wrong owner.** Unchanged from the
2026-08-24 audit and still open — see below.

---

## Known gaps

Found by auditing the running build on 2026-08-24, and **each one re-checked
against the code on 2026-08-26** rather than carried forward on trust. Three
survive; one is fixed and is recorded as fixed below.

**FIXED — the demo seed no longer pre-bakes a live `GeometrySpec`.** The gap was
real: `geometry_is_stale` is false for a profile that already has one, and the
seed dated its spec five days *after* the newest geometry-invalidating fact, so
`geometry-watcher` skipped every address and the model on screen was seed output.
`8d292b0` moved the seeded `generated_at` to `epoch - 420 days`, which puts it
before the 2025-07 permit that disputes it — the exact sequence the staleness
rule exists for. The demo still opens with a model; the first pass now replaces
it with a measured one.

**Nothing runs concurrently.** `asyncio.gather`, `TaskGroup`, `create_task` and
`as_completed` return zero hits across `backend/src`, graphs included. Both
loops are strictly serial, and so is `wake_all`, which starts routed agents one
at a time. The dependency structure is already a DAG: `records-watcher`,
`hazard-watcher` and `geometry-watcher` read disjoint sources and write disjoint
canonical keys, `structure-watch` joins them, `referral-clerk` follows.
`wake_all`'s own docstring states the woken agents are not each other's
prerequisites, which is the precondition for concurrency, stated and unused.
`FleetRunner` still mints the grant and enforces the deadline per agent, so
governance is untouched by the change.

**The slow loop is a scheduled pipeline, not a handoff.** `run_slow_loop`
iterates a hardcoded tuple of four agent ids and then runs `referral-clerk`.
Every pass goes through the runtime, so authority and run records are real, but
nothing hands anything to anything and the order is a literal in the source. The
incident loop is where genuine agent-to-agent routing lives: seven wake rules
matched against the catalog by declared capability, no rule naming an agent.
Describe the two loops differently, because they are different.

**The demo clock un-retires four agents.** `SUPERSEDED_AT` is
`2026-08-21 12:00 UTC`; fake mode runs at `2026-08-20 08:00`; routing asks
`descriptor.is_deprecated(now)`. Verified against the running build — a dispatch
woke `brief-reconciler@1.0.0` and `incident-controller@1.0.0`, both
`started: True`. The console's fleet rail filters on the field rather than on
`now`, so one panel lists them as superseded while another shows them running.
Moving `SUPERSEDED_AT` before the demo clock is a one-line fix and changes no
logic.

**`preincident-plan-store` names the wrong owner.** `geometry-watcher` declares
`Capability.WRITE`, `write_targets=("preincident-plan-store",)` and
`Scope.WRITE_PREINCIDENT_PLAN`, and its constructor takes no plan store — it has
never written one. The plan is written by `structure-watch`, through
`ActionFlow`, which declares neither the target nor the scope. So the catalog
names an owner that cannot write, and the real writer writes on undeclared
authority. `test_every_external_write_target_has_an_owning_agent` compares
declared targets to *configured* targets and passes. The missing invariant is
the other one: every target a handler writes must be declared by the agent whose
id that handler runs under.

**The deployment has drifted from `main`, and nothing detects that.** The
running images are tagged `11165da`; `main` is 17 commits past it. Nothing in
`make verify`, in CI or in the smoke suite compares the digest a service runs to
the commit that is checked out, so the console at the staging URL can disagree
with the console on a laptop indefinitely and neither will say so. See
[What is actually deployed](#what-is-actually-deployed).

**Documentation drift is its own gap, and this file was part of it.** Corrected
here on 2026-08-26: the port count (16 → 17, `threads` was missing), the adapter
count, the Terraform module count (13 → 14), the Cloud Run service count
(11 → 12, the console was not counted), the claim that nothing had been deployed,
what "no regional map" meant (the SVG drawing went; the panel is still
rendered), and the strict mypy file count (187 → 193). `backend/src/firstdue/sources/catalog.py:3` still
opens "Eleven sources" where the catalog holds thirteen — left alone here
because it is a code docstring, not this document, and should be fixed in the
file it lives in.

---

## Verification

Every row below was run on 2026-08-26 at commit `55e3e53`, with the working tree
as described under [In flight](#in-flight). Numbers in this section are outputs,
not estimates.

| Check | Command | Result |
|---|---|---|
| Backend tests | `uv run pytest` | **1,536 passed, 47 skipped**, 23s |
| Console tests | `npx vitest run` | **356 passed**, 21 files |
| Strict mypy | `make typecheck` | clean across **197 source files** |
| Ruff | `make lint` | clean; 271 files already formatted |
| Console lint / types / build | `npm run lint`, `typecheck`, `build` | clean; production build succeeds |
| Terraform | `make infra-check` | `fmt` clean, staging and prod both validate, 38 infra tests pass |
| Seed determinism | `make verify-seed` | 385 profiles, hash `38f25004df7956d8…c68da0`, reproduced |
| Secret scan | `make secret-scan` | gitleaks over 43 commits, **no leaks found** |

The 47 skips are the contract suite, which needs `GCP_TEST_PROJECT_ID` and a
real Firestore and Pub/Sub — it is not skipped for being broken. `make
test-cloud GCP_TEST_PROJECT_ID=firstdue-test` is the way to run it.

Ten architecture decision records. A contract suite that holds the in-memory and
Firestore backends to one set of behaviours, an infrastructure suite that holds
Terraform to the agent descriptors, and an observability suite that asserts
telemetry carries no document content.

The catalogue and fleet counts, also read from the code rather than from prose:
**13 published agents, 9 scheduled** (`brief-reconciler`, `conflict-detector`,
`incident-controller` and `survey-ranker` are the superseded four); **13
catalogued sources, 10 with live endpoints**, the other three carrying the
reason they have none.

## In flight

Uncommitted on `main` as of 2026-08-26, and passing: **token-by-token streaming
of the enriched brief in the console.**

The backend has streamed it since the reconciler was built — `GET
/api/v1/incidents/{id}/brief/stream-enriched` yields provisional `narrative`
frames as the model composes and a final persisted `brief` frame. The console
was asking for prose with a blocking POST and rendering the finished paragraph
in one go, so the one part of the brief that genuinely is written over time was
being waited for in silence. `useNarrativeStream` in `frontend/lib/api/stream.ts`
now consumes the SSE endpoint, drops a chunk whose `for_version` is behind the
current emission, and holds provisional prose separately from emissions so it is
never merged into the record.

`BriefPanel` changes with it: it accumulates rather than replaces, keying each
line on `section + label + value_render` and marking the version it first
appeared in. A label whose *value* changed is a new line — a face that was
`UNSCANNED` and is now 166 °C is the drone sweep having flown it, and treating
that as the same line would let exactly the change a commander is waiting for
arrive silently.

`OpenIncidentResponse` gains `address_display`, resolved from the city adapter
and sent *alongside* `address_id` rather than replacing it: the id is what every
event, grant and log entry is keyed by. It is empty rather than a placeholder
when the city cannot place the id, and the banner falls back to the id.

Two new test files, `frontend/tests/brief-streaming.test.tsx` and
`frontend/tests/incident-banner.test.tsx`, 11 tests, green.

Also uncommitted: **the three-column standby and the regional terrain map** — see
[The console](#the-console) and phases 18–19 in `docs/build-notes.md`. Backend: a
`RegionBasemap` model and a `fetch_region` verb on the imagery port; the new
`ports/tiles.py` seam with Google Map Tiles and terrarium behind it, a fake that
generates a landscape from coordinates, and
`GET /api/v1/terrain/{layer}/{z}/{x}/{y}`; `adapters/mercator.py` with 25 tests;
and `brightness_k`/`daynight` carried through from the FIRMS feed. Console:
`RegionalHeatMap` on deck.gl `TerrainLayer` and `HeatmapLayer`, ranked hotspot
clustering with a detail card, `PanelCard`, a `headless` prop on the two panels
that now sit inside one, and a binary passthrough branch in the gateway.

Apache-2.0. No real person's records appear anywhere in this project.
