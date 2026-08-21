# Deployment

Terraform owns every cloud resource in this project. `gcloud` is used for three
things and no others: authenticating, creating the state bucket that Terraform
cannot create for itself, and adding secret *values* that must never pass
through a plan file.

> **Nothing in this document has been applied.** The configuration validates
> (`tofu validate`, `tofu fmt -check`) and its conformance to the application is
> tested (`tests/infra/`), but no FIRST DUE environment has been deployed. Every
> cost figure below is an estimate from published list prices, not an
> observation from a bill. See [Build notes](build-notes.md).

---

## Bootstrap, once per project

```bash
export PROJECT_ID=your-project-id
export REGION=us-central1        # optional; this is the default

gcloud auth login
gcloud config set project "$PROJECT_ID"
gcloud auth application-default login

make bootstrap-infra
```

`infra/bootstrap.sh` enables the two APIs it needs, creates
`gs://$PROJECT_ID-firstdue-tfstate` with uniform access, public-access
prevention, and **object versioning**, and writes
`infra/terraform/envs/staging/backend.hcl`.

Versioning is not optional. A truncated state file with no previous version
leaves the project itself as the only record of what exists, and reconciling
that by hand takes a day.

### Remote state

State lives in that bucket under the prefix `firstdue/staging`. It is never
committed: it holds every resource id, and for some providers, values that were
meant to stay in Secret Manager. `.gitignore` covers `*.tfstate`,
`terraform.tfvars`, and `backend.hcl`.

### Enable the APIs

```bash
gcloud services enable run.googleapis.com firestore.googleapis.com \
  pubsub.googleapis.com aiplatform.googleapis.com storage.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com cloudtrace.googleapis.com \
  logging.googleapis.com monitoring.googleapis.com \
  cloudbilling.googleapis.com billingbudgets.googleapis.com
```

The `project-services` module enables these too, so this step only shortens the
first apply.

---

## Configure

```bash
cd infra/terraform/envs/staging
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars
```

| Variable | Notes |
|---|---|
| `project_id` | Required. |
| `billing_account` | Required — the budget resource needs it. `gcloud billing accounts list`. |
| `plans_bucket` | Globally unique. Holds pre-incident plans. |
| `backend_image`, `console_image` | Set by `make deploy-staging`; the example values are placeholders. |
| `alert_emails` | Who hears about a 5xx or a missed latency budget. |
| `model_armor_template` | Leave empty if Model Armor is not allowlisted for your project. The local deterministic detector remains and the console reports which screen ran. |
| `vector_search_enabled` | Leave `false`. See [Cost](#cost). |
| `scheduler_paused` | `true` for staging. An unattended slow-loop pass before the first smoke test is noise. |

### Secrets

Terraform creates the *containers*. Add the values yourself:

```bash
printf '%s' "$(openssl rand -hex 32)" | \
  gcloud secrets versions add firstdue-staging-callback-secret --data-file=-

printf '%s' "$(openssl rand -hex 32)" | \
  gcloud secrets versions add firstdue-staging-console-token-secret --data-file=-
```

No secret value appears in this repository, in a plan file, in the state bucket,
or in `gcloud run services describe` — Cloud Run resolves a reference at start.

`CALLBACK_SECRET` is required in live mode by the settings validator, so a
process without it refuses to start rather than failing at request time on a
fireground.

---

## Deploy

```bash
export PROJECT_ID=your-project-id
make deploy-staging
```

The script:

1. applies `module.registry` alone, because the first push needs somewhere to go
   (the documented exception to "never use `-target`");
2. builds both images with Cloud Build — no local Docker daemon required;
3. resolves each image to a **digest**, because a rollback has to name the exact
   image that was running and `:latest` cannot;
4. runs `tofu plan`, prints it, and **waits for you to read it** (`AUTO_APPROVE=true`
   skips the prompt, for CI where a human already reviewed the plan artifact);
5. applies, then checks `/healthz` on the incident service.

To see a plan without deploying: `make infra-plan`.

### What exists afterwards

| Resource | Notes |
|---|---|
| `firstdue-slow` (Cloud Run) | Slow loop. `min=0`, `max=2`, 1800 s timeout, concurrency 10. |
| `firstdue-incident` (Cloud Run) | Incident loop. `min=1` — a cold start on dispatch is the one latency this system exists to avoid. `max=4`, 900 s timeout. |
| `firstdue-console` (Cloud Run) | Public; authenticates its own users. Holds no cloud permissions. |
| Firestore (native) | PITR on, delete protection on, optimistic concurrency. Composite indexes from `policy/firestore.json`. |
| 16 Pub/Sub topics | One per `Topic` member, each with a push subscription and its own dead-letter topic. |
| Cloud Scheduler | Daily 03:00 tick to `/internal/scheduler/tick`, OIDC-authenticated. Paused in staging. |
| 9 agent + 6 service accounts | Roles derived from each agent's declared scopes. |
| 2 secrets | Containers only. |
| Budget | $50/month, alerts at 50/90/100% plus a forecast alert. |
| Vector Search | **Nothing** unless `vector_search_enabled = true`. |

### Identity

One service account per agent, with roles derived from that agent's
`required_scopes` in `registry/descriptors.py`. Three properties are tested in
`tests/infra/test_terraform_follows_the_code.py`, with no cloud access:

- an agent never receives a role that only another agent's scopes imply;
- no service account holds `serviceAccountTokenCreator`, `serviceAccountUser`,
  `owner`, or `editor` on any other — impersonation is the one way correct roles
  still let an SA act as a different agent, and it is simply absent;
- `read:ems-derived` maps to **no IAM role at all**. PHI is reachable through the
  gateway's `DERIVE` path, at runtime, under an `IncidentGrant` that expires. A
  standing IAM role would make that grant decorative.

---

## Smoke test

```bash
STAGING_BASE_URL=$(tofu -chdir=infra/terraform/envs/staging output -raw incident_url) \
STAGING_TOKEN=$(gcloud auth print-identity-token) \
make smoke-staging
```

Six checks, in `tests/staging/test_smoke.py`:

1. **profile read** — a slow-loop profile is readable through the deployed API;
2. **event handling** — a full pass over real sources into a real database;
3. **instant SSE** — the first frame arrives with `model_invoked: false`. A
   staging environment that reaches Gemini before the first line has lost the
   thing the system is for;
4. **approved write** — a dispatch cuts a work order, and a second dispatch
   returns the same one rather than booking a second company;
5. **audit decision** — a commitment produces an `ALLOW` and a
   `REQUIRE_APPROVAL`, both on the record with their rule ids;
6. **trace correlation** — one `X-Correlation-ID` appears on the response and on
   every audit event the request caused.

The suite skips entirely without `STAGING_BASE_URL`, so it is inert locally, in
the default CI job, and in forks.

---

## Migration

Firestore is schemaless, so a "migration" here means one of three things.

**A new collection.** Add it to `COLLECTION_NAMES`, add an entry to
`policy/firestore.json` — with indexes, or with a `reason` saying why none are
needed — and apply. `tests/infra/` fails if you skip the second step. No data
movement is involved; the collection appears on first write.

**A new index.** Add it to `policy/firestore.json` and apply. Index builds are
online but not instant; a query that needs one fails with a console link until
the build finishes. Apply the index *before* deploying the code that queries it.

**A changed document shape.** Facts, snapshots, and audit events are immutable
and derived-id addressed, so the usual approach is additive: write the new shape,
let the old documents age out, and keep both readable in between. There is no
backfill script and there should not be one — rewriting a fact in place would
change a value that a brief already showed to a commander, and the audit trail
would not record it.

Cloud Run is versioned by revision. `tofu apply` with a previous digest is a
rollback; traffic is `LATEST`, so it takes effect on the next request.

---

## Cost

Estimates from published list prices for a single low-traffic staging
environment. **Not observed from a bill.**

| Service | Assumption | Estimate |
|---|---|---|
| Cloud Run incident | `min=1`, 2 vCPU / 2 GiB idle-billed | ~$25–30/mo |
| Cloud Run slow | `min=0`, one 10-minute pass daily | ~$1–2/mo |
| Cloud Run console | `min=0`, occasional use | <$1/mo |
| Firestore | <1 GiB, a few hundred thousand ops/mo | ~$1–3/mo |
| Pub/Sub | Well under the 10 GiB free tier | $0 |
| Cloud Storage | Plans and state, <1 GiB versioned | <$1/mo |
| Cloud Build | Under the free daily minutes | $0 |
| Artifact Registry | ~2 GiB after cleanup policies | <$1/mo |
| Vertex (Gemini Flash) | Enriched briefs only; the instant brief makes no call | ~$1–5/mo |
| Trace / Logging / Monitoring | Under free tiers at this volume | $0 |
| **Total** | | **~$30–45/mo** |

The single largest line is `min_instances = 1` on the incident service. Setting
it to 0 saves roughly $25/month and costs a cold start on dispatch — which is
the wrong trade for this system, and is why it is written down here rather than
tuned quietly.

**Vector Search is off by default.** The index is nearly free; the *endpoint*
bills for provisioned serving nodes around the clock whether or not anything
queries it — several hundred dollars a month, which cannot sit under a $50 cap.

**The budget alert is the second line of defence, not the first.** It arrives by
email, minutes late, and cannot stop anything. What actually bounds spend is
`max_instance_count` on every service.

---

## Teardown

Order matters, and some of it is irreversible.

```bash
cd infra/terraform/envs/staging

# 1. Stop new work arriving first. Otherwise the scheduler fires into a
#    half-destroyed environment and the run fails in a way that looks like a bug.
tofu destroy -target=module.scheduler
tofu destroy -target=module.pubsub

# 2. Everything else.
tofu destroy
```

**What survives, deliberately:**

- **Firestore.** `delete_protection_state = DELETE_PROTECTION_ENABLED` and
  `deletion_policy = ABANDON`. `destroy` removes it from state and leaves the
  database. Deleting it is a separate, deliberate act:
  `gcloud firestore databases delete --database='(default)'`.
- **The plans bucket.** `force_destroy = false`, so a bucket with objects in it
  refuses to be destroyed. Empty it yourself if you mean it.
- **The state bucket.** Created outside Terraform; delete it last, if at all.
- **Enabled APIs.** `disable_on_destroy = false` — tearing down FIRST DUE must
  not switch off an API another workload in the project depends on.
- **Secret values.** The containers are destroyed; rotate anything that was in
  them, because destruction is not proof they were never read.

**What is irreversible:** the Firestore database and its point-in-time recovery
window, once you delete it manually. Audit events live there. If this
environment ever ran an incident that a person relied on, exporting the audit
collections before teardown is the difference between a record and a story.

---

## Local alternatives

Nothing above is needed to run the system.

```bash
make demo             # the whole fleet, credential-free
gcloud auth application-default login
make test-cloud GCP_TEST_PROJECT_ID=your-test-project   # the contract suite
make infra-check      # tofu fmt, validate, and the conformance tests
```

`make infra-check` needs no cloud project and no credentials — which is the
point of putting the IAM and index claims in tests rather than in prose.
