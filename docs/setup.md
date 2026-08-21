# Setup

## Requirements

| Tool | Version | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | ≥ 0.4 | installs and pins Python 3.12 |
| Node.js | ≥ 20 | console toolchain |
| Docker | optional | for the emulators and image builds (gcloud + a JVM also works) |
| gcloud | optional | only for `make deploy-staging` |

No Google credentials are required for anything below.

## Install

```bash
make setup       # uv python install 3.12 && uv sync, then npm install
```

## Run the credential-free demo

```bash
make demo
```

- API: <http://localhost:8000> (`/healthz`, `/readyz`, `/api/v1/system/status`, `/docs`)
- Console: <http://localhost:3000>

`make demo` seeds deterministic state first, so the console reports real loaded
profiles rather than an invented number.

## Verify everything

```bash
make verify
```

Runs: Ruff lint, Ruff format check, strict mypy, pytest, OpenAPI generation,
seed determinism, console lint/types/tests/build, and a secret scan.

## Reset the demo deterministically

```bash
make reset
```

Clears `.demo-state/`, rebuilds it, and prints the content hash. The same seed
and epoch always produce the same hash — `firstdue verify-seed` asserts it.

## Modes

| | Fake mode (default) | Live mode |
|---|---|---|
| Env | `USE_FAKE_AGENTS=true` | `USE_FAKE_AGENTS=false` |
| Credentials | none | Google application credentials |
| Required config | none | `GCP_PROJECT_ID`, `GCS_PLANS_BUCKET` |

Live mode validates its configuration at startup and **fails loudly** if
anything is missing. It never falls back to fake adapters. Live agent, model,
and source adapters are wired in a later phase; today `USE_FAKE_AGENTS=false`
raises a clear `ConfigurationError` rather than pretending.

### Storage and event backends

These are **independent of fake mode**, because the Firestore repositories and
the Pub/Sub transport run against local emulators with no credentials at all.

| Setting | Values | Default |
|---|---|---|
| `STORAGE_BACKEND` | `memory`, `firestore` | `memory` |
| `EVENT_BACKEND` | `memory`, `pubsub` | `memory` |

`firestore` requires `GCP_PROJECT_ID` — any value works against the emulator.
Both backends are held to one contract suite; see
[ADR 0006](adr/0006-one-contract-two-backends.md).

## The internal push endpoint

`POST /api/v1/internal/events/push` is how Pub/Sub delivers events to the
fleet, which makes it a door into the event stream. It authenticates every
request, and **refuses everything if it cannot** — there is no unauthenticated
path in.

In fake mode the bearer token is derived from `DEMO_SEED`, so no secret exists
in any file:

```bash
uv run firstdue status | grep "push token"
curl -X POST http://localhost:8000/api/v1/internal/events/push \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"message": {"data": "<base64 envelope>"}}'
```

In live mode it verifies a Google-issued OIDC token instead, checking the
signature, the audience (`INTERNAL_PUSH_AUDIENCE`), and the pushing service
account (`INTERNAL_PUSH_SERVICE_ACCOUNT`). Both become required at startup when
`EVENT_BACKEND=pubsub`.

Dead-lettered envelopes are listed at
`GET /api/v1/internal/events/dead-letters`, with the same authentication.

Copy `.env.example` to `.env` to change settings. **Never put a secret value in
either file** — `.env` is gitignored, `.env.example` is scanned by gitleaks.

## Emulators

```bash
make up              # Firestore on :8081, Pub/Sub on :8085 (docker compose)
make test-emulator   # run the contract suite against both backends
make down
```

`make demo` does not need them. They exist so the Firestore repositories and
the Pub/Sub transport are tested against the real client libraries rather than
against a hand-written double.

Without Docker, the emulators also run from the gcloud SDK directly — this is
how phase 2 was verified:

```bash
brew install openjdk                                    # the emulators need a JVM
gcloud components install cloud-firestore-emulator pubsub-emulator beta
gcloud emulators firestore start --host-port=127.0.0.1:8081 --project=firstdue-local &
gcloud beta emulators pubsub start --host-port=127.0.0.1:8085 --project=firstdue-local &
make test-emulator
```

`make test` skips the emulator-backed tests when no emulator is reachable, and
says so. CI fails the job if they skip there — a skipped backend has proved
nothing.

## Containers

```bash
make docker-build   # both images
make docker-smoke   # build, run backend on $PORT, prove non-root, curl /healthz
```

Both images run as uid 10001, honour `PORT`, and terminate on SIGTERM.

## Deploy to staging

```bash
export PROJECT_ID=your-project-id
make deploy-staging
```

See [deploy.md](deploy.md).

## CLI

```bash
uv run firstdue status        # resolved mode and configuration
uv run firstdue seed          # build deterministic demo state
uv run firstdue reset         # clear and rebuild
uv run firstdue verify-seed   # prove determinism
uv run firstdue schema        # write docs/openapi.json
uv run firstdue serve         # run the API
```
