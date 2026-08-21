# Build notes

Running record of decisions, deviations, commands, and risks. Newest phase last.

---

## Phase 1 — Repository foundation and executable domain contracts

**Date:** 2026-08-20

### Decisions

| # | Decision | Why |
|---|---|---|
| 1 | Python source lives in `backend/src/firstdue`, tests in root `tests/`, one root `pyproject.toml` | Matches the PRD tree and the required monorepo layout while keeping a single toolchain config and one `uv` environment. Hatchling builds the package from `backend/src`. |
| 2 | `is_known` is a read-only property, not a field | As a field it could be forged past `extra="forbid"` — `UnknownValue(is_known=True)` would have been constructible. A property cannot be. |
| 3 | `BuildingProfile.fact_sets` holds every fact; `facts` is a computed resolved view | The PRD types both `BuildingProfile.facts` and `ProfileSnapshot.facts` as one fact per key, but "conflicting facts both remain stored" needs a set per key. `fact_sets` stores; `facts` resolves. `ProfileSnapshot.facts` keeps the PRD shape exactly. **Deviation, deliberate.** |
| 4 | Merge order: live observation → known-beats-absent → tier → recency → confidence → `fact_id` | "Memory never outranks live observation, always" is absolute, so tier is compared first. An *absent* live reading is handled separately: within the live tier strict recency wins, so lapsed thermal coverage shows as `UNSCANNED` rather than reverting to a stale hot reading. `fact_id` is the final tie-break purely for determinism across replays. |
| 5 | Events are validated to contain identifier tokens only | Makes "events carry IDs, never payloads" checkable rather than a convention. A validator rejects any value with whitespace. |
| 6 | `PolicyDecision.decided_by` is a `Literal` constant | Makes "no model participates in an authorization decision" auditable in the record itself, not just in a README. |
| 7 | `ports/` package added (not in the PRD tree) | Protocols span layers; putting them in `domain/` would make the domain import I/O concerns. |
| 8 | Live mode raises `ConfigurationError` instead of falling back to fakes | A process that silently downgraded would lie about where its data came from. Live adapters land in a later phase; today the failure is loud and explicit. |
| 9 | `capabilities.py` drives the console's built/planned split | Keeps the shell honest: it renders what exists and labels what does not, instead of buttons that do nothing. |
| 10 | Deterministic seed writes `.demo-state/seed.json` with a content hash | Makes "deterministic reset" verifiable — `firstdue verify-seed` rebuilds and compares. |
| 11 | Fake adapters do real work | `FakeModelClient` extracts against real character offsets; `FakeSourceAdapter` runs a real circuit breaker with cooldown and half-open probe; `FakeWriteTarget` really dedupes. No pass-only stubs. |
| 12 | Ruff: `ANN101`/`ANN102` disabled (deprecated), `UP040` disabled | The first two are noise; PEP 695 aliases interact badly with pydantic discriminated unions. |

### Deviations from the PRD

- **`BuildingProfile.facts` shape** — see decision 3. `ProfileSnapshot` matches the PRD exactly.
- **`ports/` package** — see decision 7. The PRD tree was prefixed "incl.", so this is an addition, not a contradiction.
- **Live adapters not wired** — Phase 1 delivers the credential-free path and the seams. Documented in the container and in `docs/setup.md`.

### Commands run

```bash
uv python install 3.12
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy                                   # strict, 65 files
uv run pytest                                 # 206 tests
uv run firstdue seed && uv run firstdue verify-seed
uv run firstdue schema --out docs/openapi.json
cd frontend && npm install && npm run typecheck && npm run test && npm run build
```

### Verification evidence

- Backend: **206 passed**, Ruff clean, `mypy` strict clean across 65 files.
- Console: **10 passed** (vitest), `tsc --noEmit` clean, `next build` succeeded.
- OpenAPI generates: 3 paths (`/healthz`, `/readyz`, `/api/v1/system/status`).
- Seed determinism: rebuild produces an identical content hash.
- Smoke test: API served on :8000, console on :3000, console rendered live
  backend state (see phase report).

### Risks carried into phase 2

| Risk | Mitigation |
|---|---|
| Docker and Terraform are not installed on this machine, so `make docker-build` and the Terraform work are **unverified locally** | CI builds both images and asserts non-root + `PORT` + `/healthz`. Do not claim a green container smoke test until CI has run. |
| gitleaks is not installed locally; `make secret-scan` prints an install hint instead of scanning | CI runs `gitleaks-action` on every push. Install locally with `brew install gitleaks`. |
| The `local_status` heuristic on `FactSet` overlaps with the conflict engine landing in phase 2 | Documented on the property. The engine's `Conflict` records are authoritative; `local_status` is the fallback view when no conflict record exists. |
| `_DOCUMENT_SOURCES` (which sources require an extraction span) is a small hand-maintained set | Extend it as watchers land in phase 2. |
| No live adapters means fake/live parity is asserted by design, not yet by a shared test suite | Phase where live adapters land should run the same behavioural tests against both. |

---

## Phase 2 — Durable memory, registry, event infrastructure, deterministic engines

**Date:** 2026-08-20

### Decisions

| # | Decision | Why |
|---|---|---|
| 1 | Derived identifiers for everything the system re-derives — conflicts, snapshots, materializer timeline events, idempotency documents, seeded subscriptions | Idempotency becomes a property of the arithmetic rather than a flag somebody remembers to check. Re-running the conflict engine over unchanged facts produces the same conflict id, so "already recorded" is an exact test. See [ADR 0005](adr/0005-derived-identifiers.md). |
| 2 | The in-memory repositories are a second implementation, not a stub, and both backends run one contract suite | Fake mode is what `make demo` runs and what a judge evaluates, so it cannot behave differently from production. See [ADR 0006](adr/0006-one-contract-two-backends.md). |
| 3 | Failures are classified into `TRANSIENT` / `CONTENDED` / `PERMANENT` / `POISON` before any retry decision | Retrying a poison message forever is how a queue stops moving; retrying a correct refusal is asking the authorization system to change its mind. See [ADR 0007](adr/0007-failure-classification.md). |
| 4 | Backoff jitter is **derived** from the event id and attempt number, not drawn from a PRNG | A replay must reproduce the timing it recorded, and a test should assert the schedule rather than tolerate it. Nothing in `reliability/` reads a clock or a random number generator. |
| 5 | One `EventDispatcher` behind both the in-memory bus and the Pub/Sub push endpoint | A delivery policy that lives in one adapter is a delivery policy the other adapter gets wrong. What fake mode proves is now literally what the deployed path does. |
| 6 | Firestore documents store the model as one canonical JSON string plus lifted index fields | Round-trip fidelity (tuples, frozensets, discriminated unions, aware datetimes) and byte-identical replay. Also the only way to store a building footprint at all: Firestore rejects nested arrays, and a footprint is a tuple of coordinate pairs. **Deviation from a "native map" encoding, deliberate.** |
| 7 | `STORAGE_BACKEND` and `EVENT_BACKEND` are independent of `USE_FAKE_AGENTS` | The Firestore repositories and the Pub/Sub transport run against local emulators with no credentials, which is how they are tested. Fake mode is about agents, models, and sources; it is not about where bytes are stored. |
| 8 | The internal push endpoint fails **closed** — no verifier configured means every request is refused | This endpoint injects events into the fleet. An unauthenticated one lets anyone publish a `fact.written` for any address and have the fleet act on it. |
| 9 | The fake-mode push token is derived from `DEMO_SEED` and printed by `firstdue status` | The demo can exercise an authenticated endpoint without a secret existing in any file. Live mode ignores it entirely and verifies a Google-issued OIDC token. |
| 10 | `Capability.WRITE` means writing *outside* the department's own store | An agent that only appends facts to a profile declares the `write:profile` scope and no write targets. Reading it the other way would have forced every watcher to declare a fictional external target. |
| 11 | Descriptor publication requires an explicit `published_at` | Republishing an identical descriptor must be a true no-op. Stamping the server clock would have made every republish differ by a timestamp and trip the immutability check. |
| 12 | The demo seed builds its conflict by calling the production conflict engine | The seeded disagreement is now produced by the same code path that runs in production. If a rule changes, the demo changes with it. The seed asserts the engine produced exactly one storey conflict rather than trusting it. |
| 13 | Google client libraries are dev dependencies as well as an optional extra | The Firestore and Pub/Sub adapters are type-checked against the real client API instead of an `ignore_missing_imports` hole. No credentials are needed to install them, and fake mode still never imports them. |
| 14 | Pub/Sub attributes use `event_topic`, not `topic` | The publisher client takes attributes as keyword arguments, so an attribute named `topic` is swallowed as the client's own parameter. `RESERVED_ATTRIBUTE_NAMES` now makes that a validation error at encode time. Found by the Pub/Sub emulator test, not by review. |

### Deviations from the PRD

- **`ports/` gains six repositories** the PRD did not name — snapshots, locks,
  idempotency, agent runs, compensations — because the phase's own requirements
  (stable snapshot ids, distributed locks, duplicate-event protection, terminal
  run states, compensating-action records) need somewhere durable to live.
- **Firestore document encoding** — see decision 6.
- **`reliability/` and `eventing/` packages** — new top-level packages, for the
  reason in decision 5. Documented in `docs/architecture.md`.

### Commands run

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy                                        # strict, 91 files
uv run pytest                                      # 368 passed, 29 skipped (no emulators)

brew install openjdk
gcloud components install cloud-firestore-emulator pubsub-emulator beta
gcloud emulators firestore start --host-port=127.0.0.1:8081 --project=firstdue-local &
gcloud beta emulators pubsub start --host-port=127.0.0.1:8085 --project=firstdue-local &
FIRESTORE_EMULATOR_HOST=127.0.0.1:8081 PUBSUB_EMULATOR_HOST=127.0.0.1:8085 uv run pytest
                                                   # 397 passed, 0 skipped

uv run firstdue seed && uv run firstdue verify-seed
uv run firstdue schema --out docs/openapi.json     # 9 paths
```

### Verification evidence

- **397 passed, 0 skipped** with both emulators running; 368 passed, 29 skipped
  without them (the skips are the two emulator-backed suites, and CI fails the
  job if they skip there).
- The 27-test contract suite passes **twice**: once in-memory, once against
  Firestore. Same tests, same assertions, no backend-specific branches.
- Pub/Sub transport verified against the emulator: an envelope published through
  `PubSubEventBus` comes back decoding to the same object, with the ordering key
  and attributes intact, and three events about one building arrive in order.
- Ruff clean, `mypy --strict` clean across 91 source files.
- Seed determinism: `2769d3aac450f0…` on rebuild, and the seeded conflict is now
  the engine's output (`permit-vs-lidar-story-count`, severity 4).
- OpenAPI: 9 paths, up from 3.

### What phase 2 did **not** build

Stated plainly so the console's capability list and this document agree:

- **No slow-loop watchers.** Nothing polls a source on a schedule yet. The
  descriptors, the locks, the checkpoints, and the run records exist; the agents
  that use them are phase 3.
- **No delta ranker.** `SurveyQueueEntry` and its repository exist and are
  tested; nothing computes a ranking.
- **No gateway.** `PolicyDecision` is a contract with no engine behind it.
- **No live-mode agent, model, or source adapters.** `build_container` still
  raises `ConfigurationError` for `USE_FAKE_AGENTS=false`. Storage and event
  backends are separately selectable and both are exercised.
- **The OIDC branch of the push authenticator is unexercised**, because live mode
  cannot start. It is written and type-checked; it has never verified a real
  token.

### Risks carried into phase 3

| Risk | Mitigation |
|---|---|
| A `BuildingProfile` is one Firestore document and Firestore caps a document at 1 MiB. A structure with thousands of facts would eventually fail to save | `codec.encode` raises a `ValidationError` at the limit rather than truncating, so the failure is loud. Phase 3 should move `fact_sets` into a subcollection. |
| Firestore queries are equality-only and are sorted and limited in Python | Avoids requiring a composite index for every filter/order pair, but it reads the whole matching set. Fine at demo scale; the audit-event listing is the first place that will hurt. Needs ordered queries plus index definitions in Terraform. |
| `QueueRepository.replace_district_queue` is not transactional in Firestore — it deletes and writes entry by entry | A crash mid-replace leaves a partial ranking. The ranker is phase 3; do this in a batched write when it lands. |
| A dead letter that arrives at a process with no local subscriber is recorded as `NO_SUBSCRIBER` | Correct for a single-process demo. In a fleet where each Cloud Run service subscribes to different topics, the push subscription must name the right service, which is Terraform's job and not yet written. |
| Dead letters live in memory in both bus implementations | They survive a request but not a restart. A durable dead-letter collection is a small addition once there is an operator surface that reads it. |
| The internal push endpoint has no rate limit | It authenticates, but a valid caller can flood it. Cloud Run concurrency limits are the current backstop. |
| Docker and Terraform still unverified locally | Unchanged from phase 1. CI builds both images. |

---

## Phase 3 — The San Francisco slow-loop vertical slice

**Date:** 2026-08-20

### Decisions

| # | Decision | Why |
|---|---|---|
| 1 | One `ManagedSource` wrapping one small `PageFetcher` per source, rather than eleven adapters | Caching, rate limiting, breaking, snapshotting, and health reporting are identical for every source; only "get me a page" differs. Swapping a source from its fixture to its live feed changes the fetcher and nothing else. |
| 2 | Fact ids are derived from the observation's natural key — address, attribute, document, observation time, value | This is what makes "run the demo twice, get no duplicates" arithmetic rather than a check. Re-polling an unchanged source re-derives ids the append-only store already holds. Extends [ADR 0005](adr/0005-derived-identifiers.md) to facts. |
| 3 | `extracted_by_model` replaces source-type inference for the span requirement | A filed dataset column is not a model extraction and has no line to cite. See [ADR 0008](adr/0008-negation-and-column-vs-prose.md). |
| 4 | A negated phrase drops the candidate instead of inverting it | "No sprinkler system on file" is a statement about the file. Writing `false` claims somebody looked; writing `true` inverts the document. Writing nothing is true. See [ADR 0008](adr/0008-negation-and-column-vs-prose.md). |
| 5 | Triage (the Gemma stand-in) can only ever *skip* work | A false negative costs one extraction and a false positive costs one model call. Neither can put a wrong fact in front of an officer, which is the only property that matters for a component whose job is to save money. |
| 6 | Storey count from measured height is `round(height / 3.2)`, floored at one, stated in one function | The product's central disagreement rests on this arithmetic, so it is one named function an officer can check, not a heuristic spread across a watcher. |
| 7 | A source that is down produces an explicit `UNAVAILABLE` fact per address, not an empty result | "The Tier II registry is unreachable" and "no hazardous materials present" are different statements, and only one of them is safe to act on. |
| 8 | `hazard-watcher` is published by **county emergency management**, making nine agents | Tier II filings are confidential and the county holds them. The pinned subscription is the authorization boundary. **Deviation from phase 2's "eight descriptors", deliberate** — phase 3 explicitly requires the county subscription, which requires a county-published agent. |
| 9 | The pre-incident plan is stamped with the profile's last timeline event, not the wall clock | The artifact describes one exact profile version, so it must be a pure function of that version. Stamping `now` made rewriting the same plan produce different bytes, which the store correctly rejected as a key collision. Found by the console API tests. |
| 10 | The NFPA 1620 plan prints unknowns as a section | A plan listing five confirmed facts and silently omitting six unchecked attributes reads as a complete picture of a simple building. |
| 11 | `POST /districts/{id}/poll` exposes one slow-loop pass over HTTP | A scheduler drives this in production, but exposing it makes the demo a single request and lets the console show the loop running. It is idempotent, so it is safe to expose. |
| 12 | Live mode never falls back to a fixture | A live-mode process serving synthetic records would be lying about where its data came from. Sources with no reachable public endpoint report `UNCONFIGURED` and raise, so the fact becomes `UNAVAILABLE`. |

### What the demo does

`make slow-loop` (or `uv run firstdue slow-loop`), no credentials:

```
  facts written     43
  conflicts found   1  (permit-vs-lidar-story-count, severity 4)
  screened          directive-to-assert, instruction-override
  survey queue      4 structures ranked
  top of queue      sf-0450-hayes  score 0.871
                    - Severity 4 conflict open: Permit records 2 storeys; lidar DSM measures 3.
                    - Confidence in structure.height_m has decayed to 0.20 of its filed value
                    - 13 source changes recorded since the last survey
                    - No company survey on record for this structure
  autonomous        work order WO-00001 · calendar CAL-00001 · crew MSG-00001 · pre-plan written
  referral          AWAITING APPROVAL (a captain files this, not an agent)
  approved by human case REF-00001
```

Running it again writes nothing: 0 facts, 0 conflicts, the same work order, the
same case number, `replayed: true`.

### Verification evidence

- **489 passed** with both emulators running; 449 passed + 40 skipped without.
- Ruff clean, `mypy --strict` clean across 117 source files.
- OpenAPI: 19 paths, up from 9.
- The end-to-end demo suite (`tests/integration/test_slow_loop_demo.py`) asserts
  every clause of the acceptance criteria, including that the second run
  produces no duplicate actions.
- Backfill interruption and resume are tested against real page cursors
  (`tests/integration/test_backfill_resume.py`).

### What phase 3 did **not** build

- **No scheduler.** `poll` runs when something calls it. Cloud Scheduler wiring
  is not written.
- **No gateway.** These agents call repositories directly. Every write they make
  would route through the policy engine once it exists; today the approval gate
  on referrals is enforced in the action flow rather than by a gateway.
- **One live feed.** Only SF building permits has a real `HttpFetcher` mapping.
  The other ten report `UNCONFIGURED` in live mode rather than pretending.
- **Gemini and Gemma are seams, not integrations.** `FakeModelClient` does the
  extraction; `RecordedModelClient` pins the responses. No Vertex client exists,
  and there is no emulator for one.
- **Calendar, Gmail, and GCS live adapters are unexercised.** Written, typed, and
  never run against Google.

### Risks carried into phase 4

| Risk | Mitigation |
|---|---|
| Watchers call repositories directly, so the authorization model is designed but not enforced on their writes | Phase 4 routes them through the gateway. The scopes are already declared on every descriptor. |
| Negation detection is a word list and a 40-character window | It will miss constructions it has not seen. The architectural guarantee is unchanged: a model cannot fill an UNKNOWN, and only a survey settles an attribute. |
| `RecordsWatcher._resolve` drops a record whose address will not normalise | Correct — a permit filed against the wrong building is worse than one nobody saw — but the drop is currently invisible. It should be counted and surfaced. |
| The action flow's approval gate lives in the flow, not in a policy engine | An agent that skipped `ActionFlow` could file a referral. Phase 4's gateway is what closes this. |
| Live `HttpFetcher` mappings are unverified against the real SF open-data schema | The mapper is written from the published column names; nothing has run against the endpoint. Verify before claiming live mode works. |

---

## Phase 4 — Identity, gateway policy, security controls, replayable auditing

**Date:** 2026-08-20

### Decisions

| # | Decision | Why |
|---|---|---|
| 1 | The gateway is an ordered list of small named functions, each returning an outcome or abstaining | The whole policy is readable top to bottom in one screen, and every decision cites the function that made it. A rule nobody can find is a rule nobody can review. |
| 2 | An unmatched request is denied by `policy.default-deny`, not allowed by omission | A rule someone forgets to write costs a refusal rather than a leak. Tested with an engine that has *no* rules at all. |
| 3 | `READ_SCOPES` is derived as `set(Scope) - WRITE_SCOPES` rather than hand-listed | A new scope cannot end up in neither set, which is how a scope ends up unchecked. |
| 4 | There is no `ALLOW` for a PHI target anywhere in the policy | The most permissive outcome is `DERIVE`. Making "release the record" inexpressible is stronger than making it forbidden. |
| 5 | `DerivedFact` has a fixed field set with no field a record could occupy | Combined with "no function returns a record", the PHI boundary is structural rather than procedural. Age is a band, location is a floor. |
| 6 | `WITHHOLD_JURISDICTION` renders a row with the agreement, the authority, and a reason | Silently filtering under mutual aid is more dangerous than refusing: the officer cannot tell "not shown to you" from "nothing there". |
| 7 | The emergency exception is its own rule id and audit event, and reaches jurisdiction only | An override that could promote an expired grant or a missing scope would be an override of the whole model. Tested that it cannot. |
| 8 | Authorization is declared on the route, not checked in the handler | "Which endpoints are protected" is answerable by reading the router. A completeness test walks the route table and fails on any endpoint without a caller dependency. |
| 9 | Health probes stay public | A load balancer cannot hold a credential, and an unauthenticated liveness probe leaks nothing an attacker could not learn from the open port. |
| 10 | Console credentials are derived per role from `DEMO_SEED` | The demo authenticates against real checks without a secret existing in any file. Live mode verifies OIDC and never derives a secret from a seed that ships in the repo. |
| 11 | Callbacks are signed over method, path, timestamp, and a body hash, with the timestamp *inside* the signed material | A signature that never goes stale is a replay waiting to happen. |
| 12 | Replay reports both a per-entry hash check and an ordered digest | They catch different tampering: editing content under its own hash fails the per-entry check; editing content *and* rehashing changes the digest. |
| 13 | Rate limiting and body caps are outermost, and exempt health probes | Rate-limiting readiness pulls a healthy instance out of rotation during exactly the spike the limit exists to survive. |

### A bug the tests found

`coerce_value` detected negation only in the text *preceding* a match, so the
malicious permit's "no hazardous materials present" coerced to
`hazard.tier_ii.present = true` -- the exact inversion the phase-3 fix was meant
to prevent, in a phrase that carries its own negation. Fixed by checking the
matched text for a leading negation as well.

Two more were found by the authorization completeness test: `/internal/events/dead-letters`
was missing from the matrix, and `/internal/callbacks/write` had no caller
dependency (it authenticates by signature). Both are now explicit entries rather
than gaps.

### Verification evidence

- **615 passed** with both emulators running; the full suite is green without
  them too, with the emulator-backed suites skipped.
- Ruff clean, `mypy --strict` clean across 128 source files. 22 API paths.
- All five gateway outcomes are produced by one engine at one policy version.
- Every guarded endpoint refuses an anonymous caller and a forged token; every
  read scope in the system is tried as a write and refused.
- The malicious permit from the threat model is screened on four patterns and
  cannot assert a fact through prose or through a structured column.
- Replay reconstructs the same ordered output and the same digest twice, and
  detects both tampering shapes.

### What phase 4 did **not** build

- **The watchers still call repositories directly.** The engine, the grants, and
  the scopes exist and are tested; routing agent writes through `decide()` is
  the remaining work. Until then the approval gate lives in `ActionFlow`.
- **The OIDC branches have never verified a real token.** Live mode cannot start.
- **Rate limiting is per-instance.** A distributed limit needs shared state.
- **Model Armor is a boundary, not an integration.** The local detector runs in
  fake mode and would run alongside Armor in live mode; the Armor call itself is
  unexercised.

### Risks carried into phase 5

| Risk | Mitigation |
|---|---|
| Agent writes bypass the gateway | Phase 5 should route `ActionFlow` and the watchers through `PolicyEngine.decide` and record every decision. The scopes are already on every descriptor. |
| The console role model is three fixed roles | Real departments have more. The mapping is one dict; the enforcement does not change. |
| `DerivedFact` guards against the obvious identifying shapes only | A subtly identifying note would pass. Derivation functions are a closed, reviewable set for exactly this reason. |
| Audit events live wherever the audit sink lives | In-memory by default. The Firestore sink exists and is contract-tested; production must select it. |

---

## Phase 5 — The incident loop

**Date:** 2026-08-20

### Decisions

| # | Decision | Why |
|---|---|---|
| 1 | Opening does authority, then the snapshot, then the record, then the event, then the clock -- in that order | Nothing is read before there is a grant to read under, and the snapshot id is on the incident before anything is emitted about it. |
| 2 | The elapsed clock runs from CAD dispatch, not from when this process received the message | The difference is queue time the commander already spent. |
| 3 | A cold profile opens the incident anyway, marked `cold_start` | New construction is not an error condition. The brief says the structural attributes are unknown, which is the honest output. |
| 4 | The SSE frame id is the brief version | `Last-Event-ID` then gives resume for free, and "version 3" is a thing an operator can reason about. |
| 5 | The stream replays stored emissions rather than re-rendering | A resumed stream must show what the original one sent. Re-rendering could differ, and the difference would be invisible. |
| 6 | A malformed `Last-Event-ID` replays the whole stream instead of erroring | Showing the brief again is always safe; refusing to show it is not. |
| 7 | Registering a thermal frame requires a **write** scope | It amends the brief and appends to the log. Found by the authorization matrix test, which refused to let a viewer do it. |
| 8 | An incident grant *carries* the commitment scopes; the gateway returns `REQUIRE_APPROVAL` for them | Without the scope the answer is `DENY` and a chief could never approve a shutoff -- the agent would have no authority to ask. The scope makes it stageable; policy makes it approved. |
| 9 | The NERIS artifact is a **draft** with the disclaimer on the model | It is assembled from the log for a human to complete. Nothing here files a report. |
| 10 | The truss window builds its rendering in a property, disclaimer included | A template cannot show the numbers without the caveat, because the string is not assembled in a template. |
| 11 | Thermal coverage lapses rather than holding the last reading | Yesterday's warm wall is not today's warm wall, and a stale reading presented as current is worse than no reading. |
| 12 | The RMS flush is best-effort and never blocks the close | An incident blocked by a logging failure is a worse failure than the logging one. Entries stay buffered and a recovery flush drains them. |

### A gap in the fake that this phase exposed

`FakeModelClient(reject_output=True)` only rejected *extractions*, so the
enriched brief's rejection path -- "the model returned something the contract
refuses, keep the deterministic brief" -- could not be exercised at all. The fake
now rejects compositions too, and the test that needed it passes for the right
reason.

### Verification evidence

- **707 passed** with both emulators running; 667 passed + 40 skipped without.
- Ruff clean, `mypy --strict` clean across 137 source files. 33 API paths.
- The instant brief lands inside the 500 ms budget with `model_invoked=False`,
  and the emission is in the log with its content hash before it is returned or
  streamed.
- SSE ordering, reconnect by `Last-Event-ID`, and replay-equivalence are tested
  through the real transport.
- Model unavailable, model output rejected, and RMS unreachable each still
  produce a brief that says what is missing.
- A full incident is driven end to end and every rendered string is scanned for
  tactical language; none appears.

### What phase 5 did **not** build

- **No live EMS, NWS, or thermal feeds.** The amendment path takes derived
  facts, weather items, and thermal frames and is tested with all three; the
  adapters that would fetch them live are not written. Thermal footage is
  recorded or synthetic and is never presented as a live flight.
- **The watchers still bypass the gateway.** Unchanged from phase 4. The
  incident loop's resource requests *do* route through `PolicyEngine.decide`;
  the slow loop's writes do not yet.
- **One session per process.** The incident loop holds its emissions in memory
  for the stream. A second instance would not serve a resume for an incident it
  did not open; the log is durable, the in-memory index is not.

### Risks carried forward

| Risk | Mitigation |
|---|---|
| SSE resume is served from an in-memory index | The emissions are in the durable log; rebuilding the index from it on demand is a small addition, and would make resume work across instances. |
| The gateway is not on the slow loop's write path | Highest-value remaining work. The scopes and the engine both exist. |
| Void detection is a fixed threshold over adjacent regions | Stated with its threshold so an officer can disagree with it. It is an observation about a surface and says so. |
| `sse-starlette` binds a shutdown event to the first loop it sees | A test-harness artifact; the conftest clears it between tests. A long-lived server has one loop. |

---

## Phase 6 — The command-center frontend

**Date:** 2026-08-20

### Decisions

| # | Decision | Why |
|---|---|---|
| 1 | The browser never calls the backend directly. Every request goes through the console's own `/api/gateway` route | The backend credential is read from the server environment and attached there, so it never reaches the browser. `FIRSTDUE_CONSOLE_TOKEN` is deliberately **not** a `NEXT_PUBLIC_` variable -- Next.js inlines those into the client bundle. |
| 2 | The dispatch transition does not navigate | Losing district context at the moment a fire starts is when losing it costs most, and a page transition is a moment where a tablet on a bad connection shows nothing at all. Standby compresses; incident surfaces expand in place. |
| 3 | The slow-loop rail compresses rather than disappearing during an incident | The slow loop does not stop when a fire starts. A rail that vanished would imply it had. |
| 4 | The SSE frame id is the brief version, and the hook drops any frame without `persisted_at` | The backend gates on this too. Two independent checks, because rendering a brief the record does not contain is the failure the whole persist-before-transmit design exists to prevent. |
| 5 | Three.js is imported dynamically, and the SVG fallback is the backend's own | A tablet without WebGL never downloads the renderer, and the fallback marks the *same* disputed mass -- because the conflict is in the data, not in the renderer. |
| 6 | Every state is a glyph, a word, and a colour | On a washed-out tablet in daylight the word is what survives. Asserted by a test that counts the glyphs and reads the words. |
| 7 | `aria-live="polite"` announces each new brief version once | An officer who cannot see the version tick still hears that the brief changed, and hears whether the narrative is unavailable. |
| 8 | Queue rows carry their ranking reasons inline, expandable | A chief who disagrees with row three has to see which rule put it there. A score alone asks them to trust arithmetic they cannot check. |
| 9 | The console's types are hand-written and checked against `docs/openapi.json` by a test | A renamed backend field becomes a failing test rather than an `undefined` on a fireground. |

### Three bugs the tests and the running server found

- **`BackendStatus` crashed on a readiness payload without `checks`.** A malformed
  response blanked the console. Now it degrades the badge instead.
- **The SSE guard checked for the `EventSource` *key*, not a usable constructor.**
  A browser exposing the name without an implementation would have thrown inside
  a React effect and blanked the page at exactly the moment it is needed.
- **The server-rendered page was not authenticating.** Every endpoint requires a
  caller since phase 4, so the first paint silently lost its status header. Found
  by curling the running console, not by a test -- and now the shared client
  attaches the server-side token.

### Verification evidence

- **87 console tests pass** (vitest + Testing Library), covering standby, the
  profile, the full dispatch → resolve → close transition, SSE ordering and
  persistence, the geometry fallback, the audit console, the OpenAPI contract,
  and accessibility.
- `next lint` clean, `tsc --noEmit` clean, `next build` succeeds.
- The running console server-renders authenticated state: mode, both backends,
  municipality, the skip link, and the disclosure.
- The gateway proxies real data: the queue ranks 450 Hayes first at 0.877 with
  four cited reasons, and the backend refuses the same call without a token.

### What phase 6 did **not** build

- **No Playwright.** The acceptance criteria asks for browser tests; the Chrome
  extension was not connected in this environment and adding a browser runner
  that cannot download its binaries would have broken `make verify`. The flows
  are covered by jsdom component tests plus HTTP verification against the
  running server. **The WebGL path itself has never rendered in a real browser**
  -- jsdom exercises the fallback, which is the honest state of it.
- **No responsive testing in a real viewport.** The layout uses Tailwind's
  `sm:`/`lg:` breakpoints and collapses to one column below 1024 px, but that
  has been read rather than seen.
- **Agent activity and throughput are structural, not live.** The rail renders
  whatever activity map it is given; nothing streams per-agent run counts yet.

### Risks carried forward

| Risk | Mitigation |
|---|---|
| No real-browser test coverage | Add Playwright where browsers can be installed. The components are already driven end to end in jsdom, so the E2E would be a thin layer. |
| WebGL rendering unverified | The fallback is verified and is what a locked-down tablet gets. Verify the canvas path before demoing on hardware. |
| The gateway route is unauthenticated from the browser's side | It relies on the console being served to trusted users. A real deployment puts IAP in front of it; the backend credential never being in the browser is what limits the blast radius. |

---

## Phase 7 — Live Google integrations, observability, infrastructure, deployment

**Date:** 2026-08-20

### The four defects phase 7 found before it built anything

Exploration of the "live" paths turned up code that would have failed on a real
deploy. Each violated a rule the system already states about itself, and each is
now fixed and tested.

| Defect | Why it mattered | Fix |
|---|---|---|
| `build_live_clock_and_ids()` existed with **no callers**; live mode would have run on `SteppingClock` + `DeterministicIdGenerator` | Two Cloud Run instances sharing a seeded counter mint `fact_000001` for different facts. With derived identifiers (ADR 0005), that is silent data loss, not a visible error | The live branch calls it. `tests/test_live_mode_wiring.py` asserts a live container never holds a deterministic generator, and that two containers do not repeat ids |
| `CALLBACK_SECRET` was optional in live mode | The signed-callback endpoint would fail at **request time on a fireground**, not at startup — the "no partial live mode" the container docstring forbids | Added to the live-mode validator. Deriving it from the repo's demo seed would have been worse than having none |
| Calendar and Gmail deduped in a process-local dict | A restart or a second instance double-books a company and double-sends a crew notification. The idempotency guarantee held only for the fakes | `DurableArtifactDedupe` over the same `IdempotencyRepository` the Pub/Sub dedupe uses |
| The `google` extra shipped 3 packages; the code's own errors named 6 more | Following the error message did not fix the error | The extra now ships every package any `ConfigurationError` tells an operator to install |

### What was built

**Vertex.** `VertexModelClient` implements the existing three-verb `ModelClient`
with no signature change and slots *inside* `RecordedModelClient`, so cassettes
keep working and a miss reaches Gemini. Structured output uses a response schema
derived from `ExtractionResult` with `unknowns` required. Retries happen inside
the caller's deadline rather than per attempt.

The deterministic fallback is **not** "fall back to the fake extractor" — that
would put synthetic values behind a live label. It returns `accepted=False`, so
the facts that never needed a model stand and `model_output_rejected` is
audited. `ADKRuntime` enforces refusals in the same order as `FakeRuntime`
(expired grant → `DENIED`, missing scope → `DENIED`, elapsed deadline →
`TIMED_OUT`) and additionally propagates cancellation.

**Observability.** `observability/tracing.py` and `metrics.py`. Six span names,
nine metrics, no-op unless `OTEL_ENABLED=true` — so the suite needs no collector
and the demo needs no credentials. Every span attribute passes through
`safe_attribute()`. The `model.invoke` span carries model id, verb, schema ref,
token counts, latency, and retries; never the prompt, the completion, the
document, or a field value.

Spans and metrics were added where the matching audit event already fires:
`gateway.policy_decision` in `PolicyEngine.evaluate`, `model.invoke` around a
cassette miss only (a replay made no call, and a span claiming one would put
invented latency in the trace), `source.query` in the source framework,
`incident` around `IncidentController.open`.

**Correlation.** `POST /incidents` did not thread the caller's
`X-Correlation-ID` into the incident, so the request that opened it could not be
joined to the audit trail. It does now, and `/internal/audit/events` gained a
`correlation_id` filter — a field you can read but not search by is not a
correlation id.

**Infrastructure.** `infra/terraform/`: 12 modules, `envs/staging` and
`envs/prod`. Both validate under OpenTofu 1.12.6 against the real Google
provider schema.

Three things Terraform must agree with the code about are **data files**, not
HCL: `policy/firestore.json` (all 23 collections, each with indexes or a stated
reason), `policy/topics.json` (16 topics), and `policy/agents.json` (agent
scopes, and the scope → role map). `tests/infra/` fails if any drifts from
`COLLECTION_NAMES`, the `Topic` enum, or `registry/descriptors.py`.

**Scheduler.** Phase 3 built `poll` and left it to be called by something. That
something is now `POST /internal/scheduler/tick`, authenticated as a service the
same way the Pub/Sub push endpoint is, driven by a Cloud Scheduler job.

### The IAM claim, and how it is checked without a cloud project

"IAM prevents one agent from assuming another agent's permissions" is an
acceptance criterion that normally needs a project to verify. Three tests
establish it from the checked-in configuration:

- an agent's roles are the union of what its **own** scopes map to, and no agent
  receives a role that only another agent's scopes imply;
- exactly two `google_project_iam_member` resources exist in the IAM module, both
  driven by the derived binding map — a hand-written third would pass a
  data comparison while widening an identity, so it is asserted absent;
- no `serviceAccountTokenCreator`, `serviceAccountUser`, `owner`, or `editor`
  appears anywhere in the module or the policy. Impersonation is the one route
  by which correct roles still let an SA act as another agent.

`read:ems-derived` maps to **no IAM role at all**. PHI is reachable only through
the gateway's `DERIVE` path, at runtime, under a grant that expires; a standing
role would make that grant decorative.

### Verification actually run

| Check | Result |
|---|---|
| `uv run pytest` | 719 passed, 46 skipped (40 emulator, 6 staging) |
| `uv run ruff check . && uv run ruff format --check .` | clean |
| `uv run mypy` (strict) | no issues, 145 source files |
| `tofu fmt -check -recursive` | clean |
| `tofu validate` (staging and prod, real provider schemas) | valid |
| `tests/infra` | 29 passed |
| `FIRESTORE_EMULATOR_HOST=… PUBSUB_EMULATOR_HOST=… uv run pytest` | 743 passed, 6 skipped |
| `npm run lint && typecheck && test && build` | clean; 87 console tests pass; production build succeeds |
| `firstdue slow-loop` and one incident through the running API | unchanged: conflict at severity 4, rank 1 at 0.871, instant brief 0.319 ms with `model_invoked=false`, one correlation id joining response and audit trail |
| `tests/staging/test_smoke.py` **against a locally running server** | 6 passed |

The smoke suite was pointed at `http://127.0.0.1:8077` running in fake mode. That
does not prove staging works — but it caught five wrong assumptions about the
API contract (`opened["incident"]["incident_id"]` vs `opened["incident_id"]`,
`disposition` vs `closed_by`, a dispatch body that is required, an audit list
that is a bare array, and a district id that had no fixtures), every one of which
would otherwise have surfaced as a red herring during a real deployment.

### What phase 7 did **not** verify

**No cloud resource was created and no cloud test was run.** `gcloud` is
unauthenticated in this environment and there is no ADC file. Every cloud-facing
path — `VertexModelClient`, `ADKRuntime`, `VertexVectorIndex`, the Secret
Manager resolver, the Pub/Sub dead-letter store, the Cloud Trace exporter, and
all of Terraform — is **written and unvalidated against a real project**. The
Terraform validates against the provider's schema, which catches a misspelled
argument but not a quota, an org policy, or an API that is not enabled.

Nothing in `docs/deploy.md` has been applied. The cost table is arithmetic on
published list prices, not a bill.

Docker is still absent here, so container builds remain CI-only, unchanged since
phase 1.

### Risks carried forward

| Risk | Mitigation |
|---|---|
| The entire live path is unexercised | The fake and live adapters implement the same ports and the same contract suite covers both. The first real deploy will still surface something; the smoke test is written and ready to say what |
| CI's staging key is a long-lived SA JSON key in a GitHub secret | Scoped to an SA with **no project roles** — its only power is invoking the staging service, which is what an unauthenticated fireground console can already do. Workload Identity Federation is the better shape and is a drop-in replacement for that one job |
| The gateway is only consulted on incident commitment paths | Slow-loop reads do not produce a `PolicyDecision`. That predates phase 7 and is unchanged by it; widening it is a policy-coverage phase, not an infrastructure one |
| `terraform.tfvars` and `backend.hcl` are operator-managed | Both gitignored, both with `.example` files. `tests/infra` asserts no `google_secret_manager_secret_version` resource exists anywhere, so a value cannot be added to Terraform by habit |
| Firestore index builds are not instant | `docs/deploy.md` says to apply an index before deploying the code that queries it. Nothing enforces the ordering |

---

## Phase 8 — Closing the gap between what the PRD claims and what the code does

**Date:** 2026-08-20

A cross-reference of PRD v3 against the built system found eight places where a
claimed capability was a seam, an adapter with no caller, or configuration
nothing read. None of them were credential problems, so none would have been
fixed by a deploy. This phase closes all eight.

### The eight, and what each actually was

| # | Claimed | Actually | Now |
|---|---|---|---|
| 1 | Agents run on an Agent Runtime | `AgentRuntime.invoke` was called from **nowhere** in production code | `agents/fleet.py`; both loops run through it |
| 2 | Gemma triages documents | `GEMMA_MODEL` read by nothing; triage was a keyword list | `triage` verb on `ModelClient`, real Gemma call in live mode |
| 3 | Vertex Vector Search for semantic recall | `VertexVectorIndex` never constructed or called | `ports/vectors.py`, in-memory index, recall endpoint |
| 4 | Eleven live public sources | **One** had a live endpoint | ten of thirteen, verified against the real feeds |
| 5 | Hydrants and NWS in the catalog | Declared by the city adapter, no adapter existed | both catalogued; NWS live, hydrants honestly unconfigured |
| 6 | Prose token-streamed over SSE | One buffered string in one frame | `compose_stream`, provisional `narrative` frames |
| 7 | Incident replay | A service with tests and no route | `GET /internal/audit/incidents/{id}/replay` |
| 8 | One Cloud Run service per agent | Three services; `FIRSTDUE_LOOP` read by no code | eleven agent workers, each on its own SA |

### Decisions

| # | Decision | Why |
|---|---|---|
| 1 | The runtime *executes* agents rather than wrapping nothing: handlers register against a descriptor and `invoke` calls them | A runtime that only enforced rules on a path nothing took made `required_scopes` and `latency_target_ms` documentation. Now a denied run is a denial of actual work. |
| 2 | Slow-loop handlers are module-level and stateless, reading from `AgentInput` | Per-pass closures had to be re-registered every pass, which collided with the guard against two implementations of one agent. Reading identifiers from the payload is what the contract always asked for. |
| 3 | Incident agents require an explicit `IncidentGrant`; the runner refuses to mint them a standing one | Incident authority is bound to one incident and dies at its close. For the agents that reach EMS-derived facts, `StandingGrant` refuses to construct at all — the type already said this. |
| 4 | Triage fails **open**: every failure path answers *extract* | A wrong "extract" costs one model call; a wrong "skip" means nobody reads the document. Only that asymmetry justifies letting a cheap model decide at all. |
| 5 | A document skips only when **both** the model and the local vocabulary screen agree | The cheap model may save a call. It may not hide a filing. |
| 6 | Height is `solar plane height − 3DEP ground elevation`, and the fact cites both readings | No public DSM answers "how tall is this building" for San Francisco. The subtraction is real, and a subtraction citing one operand is a number nobody can check. |
| 7 | An implausible height difference produces **no** height rather than a small one | A building of height zero is one storey with a collapse zone computed from nothing. |
| 8 | Streamed prose is `provisional` and is always followed by an authoritative `brief` frame | Persist-before-transmit is about *facts*. Prose being composed is not a fact, carries no version, and is withdrawn by the emission that follows — including when the composition is refused. |
| 9 | A vector match returns ids and a distance, never text or a value | An embedding can say two documents resemble each other. It cannot say a building has three storeys, and nothing downstream may promote a match into an assertion. |
| 10 | Agent → topic routing lives in `registry/routing.py`, and Terraform reads a file derived from it | A push subscription pointed at a service that does not handle its topic dead-letters forever while every dashboard looks healthy. Phase 2's notes flagged exactly this. |
| 11 | `sensor-fusion` declares no `WRITE` capability | `Capability.WRITE` means writing *outside* the department's own store. A thermal observation goes to the profile and the log. |

### Four defects this phase found

| Defect | Why it mattered |
|---|---|
| **Every DataSF timestamp is naive.** `StructuralFact` refuses a naive datetime, so the first live poll would have raised on every row | Fixed by attaching `America/Los_Angeles` per source. Reading them as UTC would have shifted every municipal filing by up to eight hours — enough to reorder a merge against a same-day survey |
| **The configured screen was never called.** `build_screen` chose Model Armor in live mode; `FactExtractor` called the module-level `screen_document` regardless | A fully configured live process would have screened every ingested document with the local detector and never called Armor once. The boundary existed; nothing crossed it |
| **The incident grant lacked `write:profile`.** The incident loop writes IC resolutions and thermal readings back to the profile and always did | Nothing checked, so it worked. Routing sensor-fusion through the runtime produced a `DENIED` run — the scope declaration had been wrong since phase 5 |
| **An error inside an SSE generator cannot become an error envelope.** `200 OK` and the content type are already on the socket | An unknown incident broke the connection instead of returning 404. Prerequisites are now resolved before the response begins |

### Verification actually run

| Check | Result |
|---|---|
| `uv run pytest` | **859 passed**, 46 skipped |
| `FIRESTORE_EMULATOR_HOST=… PUBSUB_EMULATOR_HOST=… uv run pytest` | **899 passed**, 6 skipped |
| `uv run ruff check . && ruff format --check .` | clean |
| `uv run mypy` (strict) | no issues, **151 source files** |
| `make infra-check` (fmt, validate staging + prod, `tests/infra`) | clean; 38 infra tests |
| `npm run lint && typecheck && test && build` | clean; 87 console tests; production build succeeds |
| `firstdue slow-loop` | unchanged finding: severity-4 conflict, 450 Hayes rank 1 at 0.871, referral staged, one case number |
| `firstdue verify-seed` | deterministic, hash unchanged |
| Live mappers against captured real rows | 6 feeds, 3 rows each, all map; captures checked into `tests/fixtures/live_rows` |

**Live endpoints were reached from this machine** to capture those rows: DataSF
(permits, assessor, inspections, violations, parcels), EPA Envirofacts, USGS
3DEP EPQS, and api.weather.gov all answered. Google Solar and NREL were **not**
reached — both need a key this machine does not have — so their mappers are
written against the published response shapes and tested against
hand-constructed payloads. That is the honest state of those two.

### What phase 8 did **not** verify

**Still no cloud resource has been created and no cloud test has been run.**
`gcloud` remains unauthenticated here with no ADC file. Everything new that
touches Google — the Gemma triage call, `compose_stream` against Vertex, the
Solar and NREL fetchers, `VertexVectorIndex`, the eleven agent workers, and the
per-agent push subscriptions — is **written and unvalidated against a real
project**. Terraform validates against the provider schema, which catches a
misspelled argument and not a quota, an org policy, or a disabled API.

Two further honest notes:

- **Eleven Cloud Run services is a cost decision nobody has priced.** Ten scale
  to zero and the incident worker keeps one warm instance. The arithmetic is in
  `docs/deploy.md`; no bill has confirmed it.
- **`GEMINI_MODEL` defaults to `gemini-3.5-flash` and `GEMMA_MODEL` to
  `gemma-3-4b-it`.** Neither id has been resolved against a real Vertex
  endpoint. Confirm both against the model list before the first live call.

### Risks carried forward

| Risk | Mitigation |
|---|---|
| The entire live path remains unexercised | Unchanged from phase 7, and now larger. The fake and live adapters implement the same ports and the same contract suite covers both |
| Address normalisation against real DataSF rows is unproven at scale | The assessor's fixed-width location field is parsed and tested against real rows; the city adapter still drops what it cannot resolve, and the drop is counted but not surfaced |
| PHMSA, Tier II, and SF hydrants have no public feed | Catalogued as `UNCONFIGURED` with a stated reason each, rendered verbatim. "Unavailable" and "withheld by statute" are different statements and the console shows which |
| The in-memory vector index is lexical, not semantic | It is a real second implementation of the port with the same refusals, and it is what fake mode runs. A query needing true semantic distance needs the live index |

---

## CI: the emulator job became a real-database job

**Date:** 2026-08-20

The emulator job failed on three consecutive runs and was replaced rather than
repaired. Both halves of that decision matter.

**Why it was failing.** `gcloud components install` cannot work on a
GitHub-hosted runner: the preinstalled SDK comes from a package whose component
manager is disabled, so the step exited immediately. Installing an SDK that
*can* install components (`google-github-actions/setup-gcloud`) got past that
and the emulators then failed to accept a connection within 120 seconds.

**Why it was replaced rather than repaired.** An emulator is a reimplementation
of Firestore's semantics, and this suite exists to check semantics: that a
transaction serialises a read-compare-write, that `create` on an existing
document fails at the database rather than at a Python guard a concurrent
instance could race past, that a fence counter survives a release, that ordered
delivery stays ordered. Those are the properties an emulator is most likely to
approximate. Against a real project, a pass means what it says.

| Decision | Why |
|---|---|
| The emulators stay as the local path; CI uses a real project | `make demo` and `make test` must keep needing no credentials. What changed is what *CI* treats as evidence. |
| Emulator environment variables win when both are set | A developer with live credentials in their shell who starts an emulator meant to use the emulator. Writing a test's throwaway documents into a real project should not be a quiet mistake. |
| Every test purges its own namespace and deletes its own topics | An emulator forgets when it stops. A real database does not, and a suite that left a namespace behind on every run would turn a test project into a landfill and then into a bill. |
| Cleanup failures warn, they do not fail the test | A cleanup error must not turn a passing contract test red. It must not be silent either — an accumulating namespace is something somebody should notice. |
| The job skips loudly when no project is configured | A fork has no credentials and must not fail for it. The skip is a run-summary warning, because a green tick on a job that proved nothing is worse than a red one. |
| Workload Identity Federation preferred, service-account key as fallback | Phase 7 already recorded the long-lived-key risk on the staging job. A federated token expires in minutes and there is no key to leak. The provider carries an `attribute-condition` pinning it to this repository — without one it trusts every repository on GitHub. |

**Two other CI failures, fixed in the same pass.**

`gitleaks` was reporting four leaks, all of them **test idempotency keys** like
`"abcdef1234567890"` that the generic-api-key rule reads as high-entropy
secrets. An idempotency key is not a credential: in this system it is *derived*
from the content of the work it guards (ADR 0005), it is written into audit
records and API responses on purpose, and it authorises nothing. It is now
allowlisted **by field**, with `regexTarget = "match"`. Exempting `tests/`
wholesale would have been wrong — a real credential in a test file is still a
leak, and a planted Google API key, service-account JSON, and Stripe key were
each confirmed still caught. CI now runs the pinned binary rather than the
action, so `make secret-scan` reproduces a CI finding exactly and no licensing
check can fail a job for a reason unrelated to secrets.

The console job failed on `npm run test` while passing locally. The cause was a
Node version difference, not a configuration one: `tests/command-center.test.tsx`
asserted on an `aria-live` region that a follow-up `useEffect` fills, and the
helper it awaited waits for the banner rather than the announcement. Node 24
had flushed the effect by assert time and CI's Node 20 had not. Wrapped in
`waitFor`; verified against a locally installed Node 20, three runs clean.

**What is verified, and what is not.** The emulator path still passes locally
(78 contract tests, both backends). The real-project path is **written and
unrun** — this machine has no ADC, so what has been confirmed is that setting
`FIRESTORE_TEST_PROJECT` stops the suite skipping and makes it attempt a real
connection, failing only on absent credentials. Whether a real Firestore
satisfies the same contract the in-memory repositories do is exactly the
question the job exists to answer, and it has not been answered yet.

### A livelock the move to a real database exposed

Running the contract suite repeatedly, to be sure the new job would not be
flaky against real Firestore, surfaced a defect that had been there since
phase 2. `test_a_second_instance_does_not_repeat_the_work` failed about one run
in three, and the failure was not the one the name suggests: *both* instances
reported they had not run.

`FirestoreLockRepository.acquire` treated an exhausted transaction as a clean
loss. The reasoning was recorded in the docstring and was half right --
contention on the lock document does mean somebody is taking the lock, and
raising would turn "another instance is polling this district" into an
exception a caller cannot use. What it missed is that exhaustion and a clean
loss are different facts:

* a **clean loss** means somebody else holds the lease, so standing down is
  correct -- their pass produces the result ours would have;
* **exhaustion** means nobody could commit, and if every contender reads that
  as a loss, every contender stands down and the work never happens.

Two Cloud Run instances polling one district would both decline, and the
profile would go unmaterialized until the next scheduler tick.

`acquire` now re-reads the lock on exhaustion and asks the question that
actually matters -- is it held *now*? If it is, this was a loss. If it is free,
nobody won, and this contender retries after a delay derived from its own owner
id, so two contenders do not retry in lockstep and collide forever. Derived
rather than random, for the reason phase 2 gave for the event backoff: a replay
has to reproduce the timing it recorded.

Measured against the emulator, before and after, as simultaneous contenders on
one lock:

| Contenders | Before | After |
|---|---|---|
| 2 | livelocked 3 runs in 8 | 8/8 settle on one holder |
| 4 | 0 in 8 | 8/8 |
| 8 | 4 in 8 | 8/8 |
| 16 | 7 in 8 | 8/8 |

The regression test uses **eight** contenders because that is where the old
behaviour reproduced about half the time. It was first written with four, where
it passed with the fix stashed -- a test that passes either way protects
nothing, and asserting otherwise in its docstring would have been worse than
not having it. With eight it fails five runs in six against the old code.

It is a distinct property from the one `test_concurrent_writers_never_both_succeed`
asserts, and deliberately its opposite. Optimistic concurrency on a profile may
abort every writer and leave them to retry; nothing is lost, because the next
attempt recomputes the same result. A lock cannot do that. **Safety is "never
two holders"; liveness is "not zero holders", and only a lock owes both.**

---

## Phase 9 — The agent framework, and two things the tests found on the way

**Date:** 2026-08-21

The submission requires at least one Google agent framework: ADK, the Gen AI
SDK, the Antigravity SDK, or GenKit. **None of them was a dependency.** The
model adapter reached Vertex through `vertexai.generative_models` from
`google-cloud-aiplatform`, which is a Vertex client and not on that list. This
was pass/fail rather than scored, and it had been mis-recorded as satisfied.

### What was built

The Gen AI SDK (`google-genai`) now sits behind the same `ModelClient` port.
`genai.Client(vertexai=True, project=…, location=…)` reaches Vertex AI under
the deployment's own service account rather than the public Gemini API under an
API key -- a distinction worth keeping: a key is a credential that travels and
that nothing can attribute, and a municipal system should only ever hold the
kind a platform can audit and revoke.

| # | Decision | Why |
|---|---|---|
| 1 | Swap the SDK underneath the existing policy rather than rewrite the adapter | Retries, deadlines, parsing, rejection semantics, and telemetry are transport-independent and already tested. The seam was two methods wide, which is the whole reason this was a swap and not a migration. |
| 2 | One client, model named per call | The old code built a second `GenerativeModel` for the triage model. The Gen AI SDK takes the model as an argument, so "which model" stopped being a connection and became a parameter, and `triage_client` disappeared. |
| 3 | Consume the SDK's async iterator directly in `_stream` | The previous implementation pumped a blocking iterator through a worker thread into a queue, because the old SDK had no async surface. That machinery is gone, and with it a cancellation race where a timed-out stream left a thread writing into a queue nobody would read. |
| 4 | Remove `# pragma: no cover` from `_call` and `_stream` | "Live mode only" meant the two methods most likely to break on an SDK upgrade were the two nothing checked. A stub cannot prove the remote service behaves; it can prove we call it with the arguments we think we do, which is the half that breaks silently. |
| 5 | Assert against the **installed SDK's real signature**, not just the stub | A hand-written stub drifts: the SDK renames a parameter, the stub keeps accepting the old one, every test stays green, and the first real call fails. Reading `inspect.signature` needs no credentials and no network, so an upgrade that moves the seam fails in CI. |
| 6 | Bump `httpx` to `>=0.28,<0.29` | The Gen AI SDK's floor. The only httpx surface this project uses is `timeout=`, `headers=`, and `base_url=`; 0.28 dropped the deprecated `app=` and `proxies=` shortcuts, neither of which appears here. Full suite green before and after. |

### A defect the new consistency test found

`AgentDescriptor.approval_threshold` is published metadata; the gateway's
`APPROVAL_THRESHOLDS` is enforcement. Nothing connected them, so a test now
does -- and it immediately failed, on its second assertion rather than the one
it was written for.

**`agency-notifier` under-declared its authority.** Five resource kinds are
approval-gated and they split across two scopes: gas and electric shutoff are
`write:utility-shutoff`; road closure, a county hazmat team, and collapse
rescue are `write:road-closure`. The descriptor declared only the first.

It worked, which is why nobody noticed. The runtime checks that the *grant*
covers what the descriptor *declares* -- not that what the agent *exercises* is
declared -- and the incident grant carries both scopes. So three of the five
commitments ran on authority the catalog never mentioned. Two consequences, and
the second is worse than the first: a department reading the descriptor would
not learn this agent can ask police to close a street, and the day anyone
narrowed the incident grant to the declared scopes -- the obvious
least-privilege hardening -- road closure, hazmat, and collapse rescue would
all have started failing at once, in an incident.

Fixed by declaring both scopes. `infra/terraform/policy/agents.json` is derived
from the descriptors and Terraform reads it, so the infra conformance tests
failed until the policy file and the scope-to-role map were regenerated. That
is the mechanism working exactly as phase 8 intended.

### A larger finding, deliberately left open

Chasing a contradiction between the README and the gateway table turned up
something neither document says.

**`PolicyEngine.decide` has exactly one caller in the entire system:** the
incident resource request. Nothing in the slow loop calls it. So:

* the incident thresholds are genuinely enforced by the gateway;
* the referral gate is real but lives in `ActionFlow._stage_referral`, not in
  the gateway;
* the work order has **no gate at all** -- and that is deliberate, matches the
  README's argument, and is pinned by two tests.

The behaviour is right. What is wrong is that two *declarations* over-state it:
`survey-ranker` publishes `SUPERVISOR`, and `APPROVAL_THRESHOLDS` maps
`write:work-order` to `SUPERVISOR`, while nothing evaluates either on that path.
The catalog claims a human approves work orders when no human does.

That is the same failure as rendering an absent record as "none present": a
safeguard asserted rather than held. It is also a **design decision about the
security posture**, with three defensible resolutions, so it was written up in
`CONTEXT.md` rather than settled unilaterally. The consistency test states
plainly in its docstring that it compares two declarations and proves neither
is reached.

### Verification actually run

| Check | Result |
|---|---|
| `uv run pytest` | **880 passed**, 47 skipped (860 before) |
| `uv run ruff check . && ruff format --check .` | clean, 205 files |
| `uv run mypy` (strict) | no issues, 150 source files |
| `npm run test` (console) | 87 passed |
| OpenAPI drift | none |
| `firstdue seed && firstdue verify-seed` | deterministic, hash unchanged |
| New SDK seam tests, mutation-checked | 3 deliberate mutations (wrong triage model, dropped `vertexai=True`, temperature drift) each produced exactly one failure |

**Not verified: any call against a real Vertex endpoint.** This machine has no
credentials and `gcloud` is not installed. What is proven is that the client is
constructed with the arguments intended and that the installed SDK accepts
them. Whether `gemini-3.5-flash` and `gemma-3-4b-it` resolve against a real
project is still the first thing to check before a live run.

---

## Phase 10 — The emulators come out, and a switch that bundled two auth models

**Date:** 2026-08-21

Two changes that turned out to be the same shape: a switch bundling things that
do not belong together.

### The emulators

`make up` started Firestore and Pub/Sub emulators in Docker so `make
test-emulator` could run the contract suite with no credentials. CI had already
stopped using them for the reasons recorded above. That left them as a
local-only convenience whose cost nobody was paying attention to: a green local
run against an emulator looked like the same evidence CI produced and was
weaker. The livelock in `FirestoreLockRepository.acquire` is the standing proof
— it survived every emulator run this project ever did and was found by
hammering a real backend.

Removed from `tests/contract/`, the Makefile, `docker-compose.yml`,
`.env.example`, and the docs. `docker-compose.yml` now runs only the app, in
fake mode. Written up as [ADR 0009](adr/0009-no-emulators.md), which amends
ADR 0006 — the "one contract, two backends" decision is unchanged; what changed
is which Firestore satisfies the second parametrisation.

**What this costs, stated plainly.** Running the contract suite now needs a
Google project and `gcloud auth application-default login`. `make demo`,
`make test`, and the 884-test default suite still need no credentials, and the
in-memory half of every contract test still runs there — what is lost offline is
the Firestore parametrisation, not the coverage of the behaviour.

### `WORKSPACE_WRITES`, and why it had to be its own setting

`USE_FAKE_AGENTS=false` built six live integrations at once. Five of them —
Firestore, Pub/Sub, Cloud Storage, Vertex, and the source fetchers —
authenticate as the deployment's own principal. Calendar and Gmail do not: both
act *as a user*, so a service account with no calendar and no mailbox reaches
neither without domain-wide delegation on a Workspace domain or an interactive
OAuth consent.

So a deployment holding entirely valid credentials for five integrations could
not use any of them without also constructing two clients that raise on first
call — in the middle of a survey dispatch, not at startup where a configuration
problem belongs.

`WORKSPACE_WRITES` is now separate, `fake` by default, read only when
`USE_FAKE_AGENTS=false`. Cloud Storage stays with the rest of live mode, because
a pre-incident plan is written by the deployment itself and has no user to act
as. `_build_office` builds the three independently.

| # | Decision | Why |
|---|---|---|
| 1 | Three switches, not one | `USE_FAKE_AGENTS`, `STORAGE_BACKEND`/`EVENT_BACKEND`, and `WORKSPACE_WRITES` gate three genuinely different auth models. One flag over three of them forces a deployment to lie about at least one. |
| 2 | `fake` is the default | The safe direction. A deployment that has not said it holds Workspace authority should not be assumed to. |
| 3 | Fake Workspace still gets a **real** plan store | The whole point of the split. GCS does not follow Calendar down. Pinned by a test. |
| 4 | Fake mode ignores the setting entirely | A stray `WORKSPACE_WRITES=google` in a shell must not drag a credential-free demo live. Pinned by a test. |

### A claim written before it was true

The ADR draft said "the console labels those two actions simulated." It did
not. Nothing in the frontend or the backend distinguished a recorded calendar
event from a sent one.

That is precisely the failure this project exists to refuse — a work order, a
referral, and a pre-plan genuinely execute, and a crew notification sitting
beside them looking identical asserts a notification nobody received. It is the
same shape as rendering an absent record as "none present".

Fixed rather than softened: `Container.workspace_label`, a `workspace_writes`
field on `GET /api/v1/system/status`, and a `disputed`-tone pill reading
**calendar + mail: simulated**, shown only when the mode is live and the
authority is absent — in fake mode the mode pill already carries it, and a
second badge would be noise. Three console tests hold all three cases.

### Verification actually run

| Check | Result |
|---|---|
| `uv run pytest` | **884 passed**, 47 skipped (880 before; +4 `WorkspaceWrites` tests) |
| `uv run ruff check . && ruff format --check .` | clean, 205 files |
| `uv run mypy` (strict) | no issues, 151 source files |
| `npm run lint && typecheck && test && build` | clean; **90 console tests** (87 before); build succeeds |
| `make infra-check` | clean; 38 infra tests |
| `make slow-loop` | unchanged: severity-4 conflict, 450 Hayes rank 1 at 0.871, referral staged, one case number |
| OpenAPI drift | regenerated; `workspace_writes` is the only addition |
| Mutation check on the office split | forcing the `WorkspaceWrites.FAKE` branch off failed exactly one test |

### What phase 10 did **not** verify

**Still nothing has run against a real Google project.** `gcloud` on this
machine remains unauthenticated with no ADC file. The emulator removal makes the
contract suite *require* credentials it has never had here, so those 78 tests now
skip locally where they previously could be run — that is a real reduction in
what this machine can prove, accepted deliberately, and it reverses the moment
somebody runs `gcloud auth application-default login`.

`WORKSPACE_WRITES=google` is written and unrun. Nobody on this project has a
Workspace domain to delegate from, so that branch is tested for *which clients
it constructs* and never for whether they authenticate.

---

## Phase 11 — First contact with a real Google project

**Date:** 2026-08-21

Every phase before this one ended with the same sentence: nothing has run
against a real Google project. That is no longer true. This phase is what the
emulator removal was for, and it found three things that ten phases of local
green ticks could not.

### The contract suite, against real Firestore and real Pub/Sub

**80 passed, 0 errors, 4m07s**, project `firstdue-test`. Transactions serialise
a read-compare-write, `create` on an existing document fails at the database,
fence counters survive a release, ordered delivery stays ordered — against the
real thing rather than a reimplementation of it. Cleanup verified afterwards:
zero leftover root collections, zero topics, zero subscriptions.

### Three defects the first live run found

**1 · Both configured model ids were wrong, in different ways.**

`GEMINI_MODEL=gemini-3.5-flash` is a real model and 404s in `us-central1`. It
answers on `global`. `gemini-2.5-flash` is the opposite — it resolves regionally
and not globally — which is the trap: a developer debugging the 404 by trying an
older model would have found one that worked, and shipped a build that fails the
submission's Gemini-3.5-or-newer requirement while appearing to work.

`GEMMA_MODEL=gemma-3-4b-it` does not exist on Vertex **at all**. The live
publisher catalogue lists `gemma`, `gemma2`, `gemma3`, `gemma3n`, `gemma4` and
`gemma-4-26b-a4b-it-maas`; the first five are deployable Model Garden artifacts
that are not callable through `generateContent`, and the `-maas` suffix is what
marks the managed endpoint that is.

Defaults are now `VERTEX_LOCATION=global`, `GEMINI_MODEL=gemini-3.5-flash`,
`GEMMA_MODEL=gemma-4-26b-a4b-it-maas`, each with the verification recorded
beside it in `settings.py`.

**2 · The namespace purge could not survive its own teardown.**

`_purge` ran `asyncio.run()` over an `AsyncClient` from inside a fixture
finaliser — a place where the loop the test ran on is already being torn down.
Handing a fresh loop a gRPC channel there produced `RuntimeError: Event loop is
closed` at teardown of every Firestore test: **80 assertions passing and 39
errors, from cleanup alone**, which is a red job caused entirely by tidying up.

It had never fired before because phase 8 guarded it with `if not
EMULATOR_HOST`, and no local run had credentials. Removing the emulators is
what pointed it at a real database for the first time. Rewritten against the
**synchronous** Firestore client: a blocking client has no loop to lose.

Worth naming precisely: the docstring promised cleanup was best-effort and
could not fail a passing test. It was wrong. `except Exception` around
`asyncio.run` does not catch what the loop raises on the way out.

**3 · Gemma accepts a response schema and ignores it.**

The triage verb sends `response_mime_type=application/json` plus a
`response_schema` requiring `extract` and `reason`. Gemma returns JSON — and
returns `{"answer": "Yes. The permit explicitly mentions..."}`, its own shape,
not the requested one. The parse fails, and triage **fails open** exactly as
phase 8 designed it to, so every document goes to Gemini and nothing is lost.

Tested three ways to be sure it is the model and not the transport: with schema,
with mime type only, and plain. All three return prose or `{"answer": ...}`;
only the plain call is honest about what it is doing.

So the system is safe and the **cost saving Gemma exists to provide is not
being provided** — every triage answers "extract" and the cheap model is a
round trip that changes nothing. Left open deliberately: the fix is a design
decision about whether a one-token answer counts as a structured contract, and
that belongs to whoever owns the model boundary. Recorded in `CONTEXT.md`.

### Verified live, through the application's own adapter

Not curl — `VertexModelClient`, the class the fleet actually runs:

| Verb | Model | Result |
|---|---|---|
| `extract` | gemini-3.5-flash | accepted; `structure.stories = "3"` and `structure.floor_system = "Lightweight parallel chord truss floor system"` pulled from raw permit prose |
| `compose` | gemini-3.5-flash | accepted, prose returned |
| `compose_stream` | gemini-3.5-flash | 3 chunks, streaming confirmed |
| `triage` | gemma-4-26b-a4b-it-maas | reached, answered, output rejected → fails open (see defect 3) |

The extraction is the demo's centerpiece fact — the unpermitted third storey and
the lightweight truss — produced by a real model from real permit text.

### Verification actually run

| Check | Result |
|---|---|
| `make test-cloud GCP_TEST_PROJECT_ID=firstdue-test` | **80 passed**, 0 errors, against real Firestore + Pub/Sub |
| Post-run cleanup | 0 collections, 0 topics, 0 subscriptions left behind |
| `uv run pytest` | 884 passed, 47 skipped |
| `ruff` / `mypy` strict | clean, 205 files / 151 source files |
| `npm run test` | 90 console tests |
| `make infra-check` | clean, 38 infra tests |

### What phase 11 still did **not** do

No Cloud Run service exists, no Terraform has been applied, and no image has
been built — Docker is still not installed on this machine. Solar and NREL
remain unreachable without keys. `WORKSPACE_WRITES=google` is still unrun and
still has no domain to run against.

What changed is narrower than "it is deployed" and larger than it sounds: the
storage, event, and model layers are no longer written-and-unverified.

---

## Phase 12 — Model Armor, and four bugs behind one missing package

**Date:** 2026-08-21

Answering "does anything else need configuring for the agents to work?" meant
building a live container and reading what it refused to start without. It
refused for a good reason, and behind that reason were four defects stacked on
top of each other — each one hiding the next.

### What live mode actually demands

Probed by constructing the real container, not by reading the code:

| Missing | Refused at | Note |
|---|---|---|
| `INTERNAL_PUSH_AUDIENCE`, `INTERNAL_PUSH_SERVICE_ACCOUNT` | settings validation | only when `EVENT_BACKEND=pubsub`; both are outputs of the deploy |
| `MODEL_ARMOR_TEMPLATE` | container build | hard blocker — the slow loop screens every ingested document |
| `CALLBACK_SECRET` | settings validation | operator-generated, no external service |

Nothing else. No third-party key is required by any agent: the only external
keys in the system are Maps (Solar) and NREL, and a source without its key
reports `UNCONFIGURED` rather than failing the fleet.

### Four defects, innermost first

**1 · `google-cloud-modelarmor` was never in the `google` extra.** Directly
beneath a comment reading *"Every package below is named by a
ConfigurationError that tells the operator to install the 'google' extra.
Following that instruction has to actually fix the error, so the extra ships
all of them."* It did not ship this one. The invariant was stated and not held.

**2 · That `ConfigurationError` was being reported as a transient outage.**
`_service()` raises `ConfigurationError` for the missing package; `inspect()`
wrapped the call in `except Exception` and re-raised `SourceUnavailableError`.
So "nobody installed the screen" arrived as "the screen is having an outage" —
a class a circuit breaker retries forever and an operator waits out. Package
resolution now happens *outside* that try.

**3 · The client could never reach a regional template.** `ModelArmorClient()`
defaults to `modelarmor.googleapis.com`, which does not serve regional
templates, and every Model Armor template is regional. It answers
`TEMPLATE_NOT_FOUND` — which defect 2 then dressed up as an outage. The
endpoint is now derived from the template's own name at construction, and a
template name carrying no region is refused at startup rather than on the first
ingested document.

**4 · The match state was read inverted, so every document was blocked.** The
enum is `UNSPECIFIED=0, NO_MATCH_FOUND=1, MATCH_FOUND=2`, and the code did
`bool(filter_match_state)`. A clean document reports `NO_MATCH_FOUND`, which is
truthy. **In live mode the slow loop would have blocked every permit, assessor
row and inspection narrative it ingested, written zero facts, and reported a
screen working perfectly.** Now compared against `MATCH_FOUND` by name, so an
SDK renumbering cannot restore it.

A fifth, smaller: `findings` listed every key in `filter_results`, which
contains an entry per *configured* filter whether or not it fired. An ordinary
building permit came back carrying a `csam` finding. An audit record naming a
filter that did not match is a finding that never happened, about a document
written by a member of the public. Now only filters whose sub-result is
`MATCH_FOUND` are reported.

Defects 1–3 were invisible because fake mode uses `LocalInjectionDetector` and
never constructs a response object; defect 4 was invisible for the same reason.
All five are now pinned by unit tests against a stub shaped like the SDK's, so
they fail without credentials. Mutation-checked: restoring `bool(state)` fails
two of them.

### What Model Armor actually does with the red-team fixture

Verified against the real service, template
`projects/firstdue-dev/locations/us-central1/templates/firstdue-ingest` with
`pi_and_jailbreak` at `LOW_AND_ABOVE`:

| Document | Blocked | Findings |
|---|---|---|
| `malicious-permit.json` | **yes** | `directive-to-assert`, `fenced-directive`, `instruction-override`, `role-reassignment`, `system-prompt-mimicry` |
| benign permit prose | **no** | none |

**Every one of those findings is the local detector's. Model Armor returned
`NO_MATCH_FOUND` on the fixture.** That is the two-screen design doing exactly
what it was built for — *"two screens with different failure modes are what a
document has to get past"* — and it is also a correction to how this has been
described. The demo beat is "the poisoned permit is blocked and the block is
audited", which is true. "Model Armor blocks it" is not; the local detector
blocks it and Model Armor does not object. Say the former.

Whether the fixture should also trip Model Armor is a fair question to put to
the fixture rather than to the screen: it is written to read as a system prompt
inside a permit, which is what the local detector recognises structurally.

### A latent import cycle

`import firstdue.security.armor` as the *first* `firstdue` import raises
`ImportError` — `security/__init__` → `armor` → `extraction.screening` →
`extraction/__init__` → `extractor` → `security.armor`, partially initialised.
Any other entry point primes the package first, which is why nothing has hit
it. Recorded rather than fixed: untangling it touches four modules for a
failure no caller currently produces.

### Strict mypy was clean because the SDKs were absent

Installing the `google` extra to get Model Armor surfaced two real type errors
in files nothing had changed — `CloudTraceSpanExporter` is untyped, and
`neighbour.distance` on a Vector Search match is `float | None`, where
`float(None)` raises. The second is a live-path crash waiting on the first
neighbour returned without a distance; it now sorts last instead. **A strict
build that never installs its optional dependencies is not checking them.** CI
should install the extra; it does not yet.

### Verification actually run

| Check | Result |
|---|---|
| `uv run pytest` | **894 passed**, 47 skipped (884 before; +10 armor tests) |
| Mutation check | restoring `bool(state)` failed exactly 2 tests |
| `ruff` / strict `mypy` **with the google extra installed** | clean, 205 files / 151 source files |
| `make infra-check` | clean, 38 infra tests |
| `make slow-loop` | unchanged |
| Live Model Armor | malicious blocked, benign passed, verified against the real service |

### Still not done

Cloud Run remains empty and the Model Armor API is enabled on `firstdue-dev`
only. `VECTOR_SEARCH_ENABLED` is off, so semantic recall is still the in-memory
lexical index — no Vector Search index has been created.
