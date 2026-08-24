# Setup

## Requirements

| Tool | Version | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | ≥ 0.4 | installs and pins Python 3.12 |
| Node.js | ≥ 20 | console toolchain |
| Docker | optional | only for `make docker-build` and `make deploy-staging` |
| gcloud | optional | for the contract suite, live mode, and deployment |

No Google credentials are required for `make demo`, `make test`, or
`make verify`. They are required for the contract suite and live mode; see
[Real Google credentials](#real-google-credentials).

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

These are **independent of fake mode**, so the Firestore repositories and the
Pub/Sub transport can run against real Google services without also turning on
live models and live municipal sources.

| Setting | Values | Default |
|---|---|---|
| `STORAGE_BACKEND` | `memory`, `firestore` | `memory` |
| `EVENT_BACKEND` | `memory`, `pubsub` | `memory` |

`firestore` requires `GCP_PROJECT_ID` and Application Default Credentials.
There is no emulator — see [ADR 0009](adr/0009-no-emulators.md). Both backends
are held to one contract suite; see
[ADR 0006](adr/0006-one-contract-two-backends.md).

### Workspace writes

`WORKSPACE_WRITES` is a **third** switch, separate from the other two, and it
exists because Calendar and Gmail do not authenticate the way everything else
does.

| Setting | Values | Default |
|---|---|---|
| `WORKSPACE_WRITES` | `fake`, `google` | `fake` |

Firestore, Pub/Sub, Cloud Storage, and Vertex all authenticate as the
deployment's own principal — a Cloud Run service account, or your ADC login.
Calendar and Gmail act **as a user**: a service account has no calendar and no
mailbox, so `google` needs domain-wide delegation on a Google Workspace domain
or an interactive OAuth consent. A personal `@gmail.com` account cannot provide
either.

`fake` is not a no-op. The calendar event and the crew mail are recorded through
the same durable idempotency store and emit the same audit events as the live
clients would, and the console labels both actions simulated. Leave it at `fake`
unless you administer a Workspace domain.

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
signature, the audience (`INTERNAL_PUSH_AUDIENCE`), and the calling service
account against `INTERNAL_PUSH_SERVICE_ACCOUNT`. Both become required at
startup when `EVENT_BACKEND=pubsub`.

**`INTERNAL_PUSH_SERVICE_ACCOUNT` is a comma-separated list, and configuring a
single account is the mistake the setting is shaped to prevent.** Two Google
services call in, as two separate IAM identities on purpose: Pub/Sub pushes
events as the push service account, and Cloud Scheduler ticks the slow loop as
its own. Set one value and Pub/Sub authenticates perfectly while every
scheduled tick is refused — a 401 nobody is watching, and a slow loop that has
quietly not run for a week. Collapsing the two identities in IAM would have
"fixed" it by deleting the separation.

```bash
INTERNAL_PUSH_SERVICE_ACCOUNT=fd-pubsub-push@PROJECT.iam.gserviceaccount.com,fd-scheduler@PROJECT.iam.gserviceaccount.com
```

Terraform derives that value from the two service accounts it creates, so a
deployed environment sets it for you; you write it by hand only when running
live mode outside Terraform. The parsing is strict, and each rule is there
because the alternative fails invisibly:

- **Every entry must be an email address.** One that is not is a *startup*
  failure, not a runtime 401 — a principal that can never match is a refusal
  nobody would think to look for.
- **An empty list refuses all traffic.** There is no wildcard and no
  fail-open: an internal endpoint that accepts anyone is an open door into the
  event stream. A value of `", "` is set, is unusable, and is refused at
  startup for exactly that reason rather than starting a process that turns
  away every caller it has.
- Whitespace and repeated commas are formatting artifacts and are ignored.

Dead-lettered envelopes are listed at
`GET /api/v1/internal/events/dead-letters`, with the same authentication.

Copy `.env.example` to `.env` to change settings. **Never put a secret value in
either file** — `.env` is gitignored, `.env.example` is scanned by gitleaks.

## Console authentication

Two settings, and the second one is the one that will bite you.

### `CONSOLE_AUDIENCE`

The OIDC audience the console API verifies a caller's token against. **Live
mode will not start without it on any process that serves the console** — which
is every process where `FIRSTDUE_AGENT` is unset. An agent worker is not a
console: it holds one agent's identity, is not publicly invokable, and has no
operator in front of it, so it is not held hostage to an audience it never
verifies against.

In the deployed topology the value is the Cloud Run **custom audience**,
`https://firstdue-incident`. A custom audience is used rather than the service
URL because it survives the service being torn down and recreated at a
different address.

This is deliberately **not** `INTERNAL_PUSH_AUDIENCE`. That one is Pub/Sub
calling the fleet; this one is an officer's browser calling the incident
service. They are separate trust boundaries and separate settings, and each has
to be set on its own even in a deployment that happens to give them the same
string.

### `CONSOLE_ROLE_BINDINGS`

Comma-separated `email:role`, where a role is `viewer`, `captain`, or `chief`.

```bash
CONSOLE_ROLE_BINDINGS=captain@example.gov:captain,chief@example.gov:chief
```

**This one is deliberately not required at startup, and that is the sharp
edge.** A live deployment that leaves it empty comes up, serves, and lets every
authenticated caller sign in and read — and **no referral and no utility
shutoff can be approved by anyone**, because an unbound principal is a viewer.
The backend logs `console_role_bindings_empty` and keeps serving, on the
judgment that a read-only console is still worth having and refusing to start
would take it down too. But say it plainly, because this project's whole
argument rests on the human approval gates being real: **with an empty binding
map those gates exist and are unreachable.** A gate nobody can pass is not a
gate.

The parsing is strict in the other direction:

- A **typo'd role name** is a startup failure. So is an entry that is not
  `email:role`, and so is binding one principal to two different roles —
  last-wins would decide silently which authority an officer holds.
- An **unbound principal gets `viewer`**, the least authority the system has,
  which is the right answer for a caller nobody has vouched for.
- Emails are lower-cased on both sides of the comparison.

### What this is, and what it is not

**This is principal-level binding, not per-user single sign-on.** What a
verified token establishes is that Google issued it for this audience and that
the email on it is one `CONSOLE_ROLE_BINDINGS` names. There is no session, no
group membership, and no directory lookup, and the binding list is
configuration an operator maintains by hand. The role is never read from a
token claim — a Google-issued ID token carries no custom claims at all, so a
`firstdue_role` claim could never be present, and defaulting off one meant
every live caller was a viewer.

A console that needs real per-user authentication needs an **identity-aware
proxy in front of it**. That is a deployment decision this process cannot make
for itself, and claiming otherwise would be claiming an authentication story
the code does not have.

## Real Google credentials

Everything above runs with no Google auth at all. Two things need it: the
contract suite, and live mode. Both authenticate the same way — **Application
Default Credentials** — and there is no emulator path for either. See
[ADR 0009](adr/0009-no-emulators.md) for why the emulators were removed.

### One-time: authenticate this machine

```bash
gcloud auth login                        # the CLI
gcloud auth application-default login    # every Google client library
```

The second writes `~/.config/gcloud/application_default_credentials.json`. That
file is what Firestore, Pub/Sub, Cloud Storage, and Vertex read. Having the
first without the second is the most common way to get a confusing
`DefaultCredentialsError` from a `gcloud` shell that looks logged in.

### One-time: two projects

| Project | Holds | Suggested id |
|---|---|---|
| dev | the app: Firestore data, Pub/Sub topics, plan bucket, Vertex calls | `firstdue-dev` |
| test | nothing but throwaway contract-suite documents | `firstdue-test` |

Separate, because the contract suite creates and deletes topics and collection
namespaces on every run and should never do that next to data anybody cares
about. Create them at
<https://console.cloud.google.com/projectcreate>, then:

```bash
for P in firstdue-dev firstdue-test; do
  gcloud services enable \
    firestore.googleapis.com pubsub.googleapis.com storage.googleapis.com \
    aiplatform.googleapis.com cloudresourcemanager.googleapis.com \
    --project="$P"

  # Native mode, not Datastore mode. The repositories use transactions, which
  # Datastore mode exposes differently.
  #
  # `nam5` is multi-region. Note that the staging Terraform defaults
  # `firestore_location` to `us-central1` for cost, and **changing the location
  # of an existing database forces Terraform to destroy and recreate it**. If
  # you create it here and then deploy staging, set `firestore_location =
  # "nam5"` in `terraform.tfvars` so the config matches what exists.
  gcloud firestore databases create --location=nam5 --type=firestore-native \
    --project="$P"
done
```

### One-time: a bucket and a callback secret

```bash
gcloud storage buckets create gs://firstdue-plans-dev \
  --location=us-central1 --project=firstdue-dev

openssl rand -hex 32     # becomes CALLBACK_SECRET; never commit it
```

### Verify the two model ids before the first live call

`GEMINI_MODEL` and `GEMMA_MODEL` default to `gemini-3.5-flash` and
`gemma-4-26b-a4b-it-maas`. **Both defaults were wrong once, in different ways,
and both were corrected against a real Vertex endpoint on Aug 21.**
`gemini-3.5-flash` is real but 404s in `us-central1` — it answers on `global`,
which is why `VERTEX_LOCATION` defaults to `global`. `gemma-3-4b-it`, the
previous default, does not exist on Vertex at all; the `-maas` suffix marks the
managed endpoint that is callable through `generateContent`. If either id is
wrong, every live model call fails at once and nothing else will tell you why:

```bash
gcloud ai model-garden models list --project=firstdue-dev --region=us-central1 \
  | grep -iE "gemini|gemma"
```

### Model Armor: enable the API and create a template

Live mode **will not start** without `MODEL_ARMOR_TEMPLATE` — the slow loop
screens every ingested document, and a screen that is configured but absent is
not a state this system will run in.

```bash
gcloud services enable modelarmor.googleapis.com --project=firstdue-dev

# Model Armor is regional, and gcloud needs the regional host told to it.
gcloud config set api_endpoint_overrides/modelarmor \
  https://modelarmor.us-central1.rep.googleapis.com/

gcloud model-armor templates create firstdue-ingest \
  --location=us-central1 --project=firstdue-dev \
  --pi-and-jailbreak-filter-settings-enforcement=enabled \
  --pi-and-jailbreak-filter-settings-confidence-level=LOW_AND_ABOVE \
  --malicious-uri-filter-settings-enforcement=enabled
```

Then set:

```
MODEL_ARMOR_TEMPLATE=projects/firstdue-dev/locations/us-central1/templates/firstdue-ingest
```

The application derives the regional API host from that name, so the region in
the template path is load-bearing and a name without one is refused at startup.

### Live source keys

Only two sources need a key, and a source whose key is absent reports
`UNCONFIGURED`. It never falls back to a fixture — a live-mode process serving
synthetic records would be lying about where its data came from.

| Setting | Where to get it | What it unlocks |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials), then enable the **Solar API** | Roof segments, pitch, and the DSM height that disagrees with the permit |
| `NREL_API_KEY` | [developer.nrel.gov/signup](https://developer.nrel.gov/signup) — free, instant | The EV charger hazard source |
| `SOCRATA_APP_TOKEN` | [data.sfgov.org](https://data.sfgov.org) → Developer Settings | Optional. Lifts the anonymous DataSF rate limit; authorizes nothing |

## The contract suite against a real database

The suite runs against a real Firestore and real Pub/Sub, and **that is what CI
does**. It is the only path: what this suite asserts is precisely what an
emulator is most likely to approximate — that a transaction serialises a
read-compare-write, that `create` on an existing document fails at the database,
that a fence counter survives a release, that ordered delivery stays ordered.

```bash
make test-cloud GCP_TEST_PROJECT_ID=firstdue-test
```

Each test gets its own collection namespace and its own topic prefix, and both
are deleted afterwards — a real database does not forget when you stop it.
Cleanup failures warn rather than fail the test, because a cleanup error must
not turn a passing contract test red; an accumulating namespace is still
something somebody should notice.

`make test` skips these when `GCP_TEST_PROJECT_ID` is unset, and says so. CI
fails the job if they skip there — a skipped backend has proved nothing.

Nothing is created ahead of time beyond the database itself: the suite makes its
own topics and subscriptions per test and deletes them.

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
