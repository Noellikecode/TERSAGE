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

## The contract suite against a real database

The same suite runs against a real Firestore and real Pub/Sub, and **that is
what CI does**. The emulators remain the credential-free local path; the real
project is the stronger evidence, because what this suite asserts is precisely
what an emulator is most likely to approximate — that a transaction serialises
a read-compare-write, that `create` on an existing document fails at the
database, that a fence counter survives a release, that ordered delivery stays
ordered.

```bash
gcloud auth application-default login
make test-cloud GCP_TEST_PROJECT_ID=your-test-project
```

Each test gets its own collection namespace and its own topic prefix, and both
are deleted afterwards — a real database does not forget when you stop it.
Emulator variables win when both are set, so an emulator running in your shell
cannot be bypassed into writing at a real project by accident.

### What the project needs

A **test** project, separate from staging and prod. It holds nothing but
throwaway documents.

```bash
PROJECT=your-test-project

gcloud services enable firestore.googleapis.com pubsub.googleapis.com \
  --project="$PROJECT"

# Native mode. The repositories use transactions, which Datastore mode
# exposes differently.
gcloud firestore databases create --location=nam5 --type=firestore-native \
  --project="$PROJECT"
```

Nothing else is created ahead of time: the suite makes its own topics and
subscriptions per test and deletes them.

### What CI needs

Set these as repository secrets (Settings → Secrets and variables → Actions):

| Secret | Required | What it is |
|---|---|---|
| `GCP_TEST_PROJECT_ID` | yes | the test project id |
| `GCP_WIF_PROVIDER` | preferred | full resource name of a Workload Identity provider |
| `GCP_WIF_SERVICE_ACCOUNT` | preferred | the service account CI impersonates |
| `GCP_TEST_SA_KEY` | fallback | a service account JSON key, if not using federation |
| `GCP_TEST_FIRESTORE_DATABASE` | no | a named database; the default is used when unset |

With `GCP_TEST_PROJECT_ID` unset the job **skips and says so in the run
summary**, so a fork stays green without proving anything it did not prove. The
in-memory half of the contract suite still runs, in the backend job.

The service account needs `roles/datastore.user` and `roles/pubsub.editor` on
the test project, and nothing else.

**Federation is preferred over a key.** A long-lived JSON key in a repository
secret is a credential that exists until somebody rotates it; a federated
token expires in minutes and there is no key to leak:

```bash
PROJECT=your-test-project
REPO=Noellikecode/TERSAGE
SA=firstdue-ci-tests

gcloud iam service-accounts create "$SA" --project="$PROJECT"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/datastore.user"
gcloud projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:${SA}@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/pubsub.editor"

gcloud iam workload-identity-pools create github --location=global \
  --project="$PROJECT"
gcloud iam workload-identity-pools providers create-oidc github \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='${REPO}'" \
  --project="$PROJECT"

# Only this repository may impersonate the service account.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
gcloud iam service-accounts add-iam-policy-binding \
  "${SA}@${PROJECT}.iam.gserviceaccount.com" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}" \
  --project="$PROJECT"

# GCP_WIF_PROVIDER is this value:
echo "projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/providers/github"
```

The `attribute-condition` is what stops any other repository on GitHub from
minting a token for this service account. Without it the provider trusts every
repository that asks.

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
