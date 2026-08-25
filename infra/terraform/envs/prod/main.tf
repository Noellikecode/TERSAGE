# The production environment.
#
# Same modules, different tfvars. This directory is checked in and has never
# been applied: production for a system that has not been through public-safety
# validation would be a claim this project does not make. It exists so that the
# staging configuration is demonstrably an environment rather than a one-off,
# and so the differences that matter -- multi-region Firestore, more incident
# headroom, an unpaused scheduler -- are written down rather than remembered.
#
# Ordering is not incidental. Identities exist before the services that run as
# them; the backend exists before the Pub/Sub push subscriptions and the
# scheduler job that call it. Terraform derives most of this from references,
# and the two places it cannot -- API enablement, and the push endpoint's
# dependence on a URL that does not exist until the service is created -- are
# handled with explicit `depends_on` rather than an apply that has to be run
# twice.

locals {
  environment = "prod"
  policy_dir  = "${path.module}/../../policy"

  # Cloud Run custom audiences: one per service, fixed before apply.
  #
  # The backend refuses to start in live mode without INTERNAL_PUSH_AUDIENCE,
  # and the value has to be the audience of the service being started -- a push
  # token minted for the slow loop must not open the incident loop. The
  # generated URL cannot be that value, because it does not exist until the
  # service is created and feeding it back into the same service's environment
  # is a cycle Terraform will not resolve.
  #
  # A custom audience is a stable string, so both sides of the check -- the env
  # var the service verifies against, and the audience every caller mints for
  # -- are known at plan time. It is additive: the generated URL keeps working
  # as an audience too.
  slow_audience     = "https://firstdue-slow"
  incident_audience = "https://firstdue-incident"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

module "services" {
  source     = "../../modules/project-services"
  project_id = var.project_id
}

module "iam" {
  source      = "../../modules/iam"
  project_id  = var.project_id
  policy_file = "${local.policy_dir}/agents.json"
  environment = local.environment

  depends_on = [module.services]
}

module "registry" {
  source      = "../../modules/artifact-registry"
  project_id  = var.project_id
  region      = var.region
  environment = local.environment

  depends_on = [module.services]
}

module "firestore" {
  source      = "../../modules/firestore"
  project_id  = var.project_id
  location    = var.firestore_location
  policy_file = "${local.policy_dir}/firestore.json"

  depends_on = [module.services]
}

module "storage" {
  source       = "../../modules/storage"
  project_id   = var.project_id
  region       = var.region
  environment  = local.environment
  plans_bucket = var.plans_bucket

  depends_on = [module.services]
}

# Containers, not values. Add versions with `gcloud secrets versions add`.
module "secrets" {
  source      = "../../modules/secrets"
  project_id  = var.project_id
  environment = local.environment

  # Every service that mounts a secret must be able to read it, agent workers
  # included: a worker whose identity cannot open CALLBACK_SECRET never becomes
  # ready, and the failure surfaces as a revision that will not start rather
  # than as a permission error anywhere obvious.
  #
  # `resend-api-key` is the outbound one: `referral-clerk` emails the building
  # department, and only after a captain approves -- never on its own. It reuses
  # `local.secret_readers`, which already covers the slow loop, the incident
  # loop, and all nine agent identities. That is a superset of "could run
  # referral-clerk", and one list that cannot drift is worth more here than a
  # second, narrower one that can.
  accessors = {
    callback-secret     = local.secret_readers
    google-maps-api-key = local.secret_readers
    nrel-api-key        = local.secret_readers
    resend-api-key      = local.secret_readers
    socrata-app-token   = local.secret_readers
  }

  depends_on = [module.services]
}

locals {
  # Accessor keys are static; only the emails resolve at apply time. See the
  # secrets module's `accessors` description for why that distinction matters.
  secret_readers = merge(
    {
      slow     = module.iam.service_emails["firstdue-slow"]
      incident = module.iam.service_emails["firstdue-incident"]
    },
    { for id, email in module.iam.agent_emails : "agent-${id}" => email },
  )
}

locals {
  common_env = {
    APP_ENV                = local.environment
    USE_FAKE_AGENTS        = "false"
    STORAGE_BACKEND        = "firestore"
    EVENT_BACKEND          = "pubsub"
    GCP_PROJECT_ID         = var.project_id
    VERTEX_LOCATION        = var.vertex_location
    GEMINI_MODEL           = var.gemini_model
    GCS_PLANS_BUCKET       = module.storage.plans_bucket
    MODEL_ARMOR_TEMPLATE   = var.model_armor_template
    OTEL_ENABLED           = "true"
    OTEL_SERVICE_NAME      = "firstdue"
    VECTOR_SEARCH_ENABLED  = tostring(var.vector_search_enabled)
    VECTOR_SEARCH_ENDPOINT = module.vectors.endpoint_id
    MEMORY_BANK_ENGINE_ID  = module.memory_bank.engine_id
    # Regional, unlike VERTEX_LOCATION above, which is `global` for the models.
    MEMORY_BANK_LOCATION = var.memory_bank_location
    LOG_JSON             = "true"

    # Backend feature switches, one plain name/value line each and deliberately
    # so: the setting names are still settling on the backend side, and a rename
    # should be a one-line change per environment rather than an edit buried in
    # a merge() or a conditional.
    #
    # Shared rather than per-service, like CONSOLE_AUDIENCE above and unlike
    # INTERNAL_PUSH_AUDIENCE: which service happens to run the grounding call or
    # open a memory-bank question is the backend's routing decision, not
    # something this file should track.
    GROUNDING_SEARCH_ENABLED = tostring(var.grounding_search_enabled)
    MEMORY_BANK_ENABLED      = tostring(var.memory_bank_enabled)
    # Emitted only when the key it pairs with is actually mounted -- see the
    # staging comment. `Settings` refuses to start with one and not the other,
    # and the default combination (address defaulted, key opt-in) is the
    # illegal one.
    RESEND_FROM_ADDRESS = contains(var.live_source_keys, "resend-api-key") ? var.resend_from_address : ""
    # Two identities, comma-separated, because two different callers reach the
    # internal endpoints: Pub/Sub pushes events, Cloud Scheduler ticks the slow
    # loop. They are deliberately separate service accounts -- the bus and the
    # clock are not the same principal, and one being compromised should not
    # confer the other's reach -- so the authorized list has two entries rather
    # than the scheduler borrowing the push identity. Collapsing them would have
    # traded a real separation for a one-line config change.
    #
    # Parsed at startup: every entry must be an email or the process refuses to
    # boot, and an empty list refuses all traffic rather than failing open. A
    # malformed join is therefore a dead service, not a quiet 401 on every tick.
    INTERNAL_PUSH_SERVICE_ACCOUNT = join(",", [
      module.iam.service_emails["firstdue-pubsub-push"],
      module.iam.service_emails["firstdue-scheduler"],
    ])

    # Console auth is a different trust boundary from the push endpoint, and
    # deliberately a separate setting even though the string is currently the
    # same one: the push endpoint admits a single service account minting for
    # the fleet, the console admits people.
    #
    # Shared rather than per-service on purpose. INTERNAL_PUSH_AUDIENCE has to
    # differ per service -- a push token for one service must not open another
    # -- but the console audience is one audience for one console, and which
    # services answer console traffic is the backend's decision, not something
    # this file should have to track. A per-service copy would silently go
    # stale the next time that routing changes.
    CONSOLE_AUDIENCE      = local.incident_audience
    CONSOLE_ROLE_BINDINGS = var.console_role_bindings
  }

  # CONSOLE_TOKEN_SECRET used to be mounted here and was never read: `Settings`
  # has no such field and ignores extras, so it was a secret handed to three
  # services for nothing. The console authenticates with an OIDC token it mints
  # from the metadata server, not a static shared token.
  secret_env = merge(
    {
      CALLBACK_SECRET = {
        secret  = module.secrets.secret_names["callback-secret"]
        version = "latest"
      }
    },
    local.source_key_env,
  )

  # Optional third-party keys. Without them the Google Solar API (roof geometry)
  # and the NREL EV hazard registry report UNCONFIGURED and the system degrades
  # silently -- every dashboard healthy, two sources quietly absent. Without
  # `resend-api-key` the referral email is unconfigured the same way: the
  # captain's approval still lands and the referral is still staged, it just is
  # not delivered by mail.
  #
  # Cloud Run resolves a secret reference when the container starts and refuses
  # to start if the secret has no version, so these cannot simply be mounted
  # and left empty: an unset key would take the whole service down instead of
  # degrading one source. Listing a key in `live_source_keys` is the operator's
  # statement that `gcloud secrets versions add` has already run for it.
  source_key_env = {
    for key in var.live_source_keys :
    local.source_key_env_names[key] => {
      secret  = module.secrets.secret_names[key]
      version = "latest"
    }
  }

  source_key_env_names = {
    google-maps-api-key = "GOOGLE_MAPS_API_KEY"
    nrel-api-key        = "NREL_API_KEY"
    resend-api-key      = "RESEND_API_KEY"
    socrata-app-token   = "SOCRATA_APP_TOKEN"
  }
}

# The slow loop: minutes of work, scales to zero between passes.
module "slow_service" {
  source          = "../../modules/cloud-run"
  project_id      = var.project_id
  region          = var.region
  name            = "firstdue-slow"
  image           = var.backend_image
  service_account = module.iam.service_emails["firstdue-slow"]

  custom_audiences = [local.slow_audience]

  environment_variables = merge(local.common_env, {
    FIRSTDUE_LOOP = "slow"
    # This service's own audience, merged over the shared map: one value across
    # every service would mean a token minted for any of them opened all of them.
    INTERNAL_PUSH_AUDIENCE = local.slow_audience
  })
  secret_environment_variables = local.secret_env

  min_instances   = 0
  max_instances   = 2
  cpu             = "2"
  memory          = "2Gi"
  timeout_seconds = 1800
  concurrency     = 10

  invokers = {
    scheduler   = module.iam.service_emails["firstdue-scheduler"]
    pubsub_push = module.iam.service_emails["firstdue-pubsub-push"]
  }

  depends_on = [module.firestore, module.secrets]
}

# The incident loop: one warm instance, because a cold start on dispatch is the
# one latency this system exists to avoid.
module "incident_service" {
  source          = "../../modules/cloud-run"
  project_id      = var.project_id
  region          = var.region
  name            = "firstdue-incident"
  image           = var.backend_image
  service_account = module.iam.service_emails["firstdue-incident"]

  custom_audiences = [local.incident_audience]

  environment_variables = merge(local.common_env, {
    FIRSTDUE_LOOP          = "incident"
    INTERNAL_PUSH_AUDIENCE = local.incident_audience
  })
  secret_environment_variables = local.secret_env

  min_instances   = 1
  max_instances   = 10
  cpu             = "2"
  memory          = "2Gi"
  timeout_seconds = 900
  concurrency     = 40

  invokers = {
    console     = module.iam.service_emails["firstdue-console"]
    ci_smoke    = module.iam.service_emails["firstdue-ci-smoke"]
    pubsub_push = module.iam.service_emails["firstdue-pubsub-push"]
  }

  depends_on = [module.firestore, module.secrets]
}

# The console is public and authenticates its own users. It holds no cloud
# permissions: it calls the incident service with a console token, and the
# backend credential never reaches the browser.
module "console_service" {
  source          = "../../modules/cloud-run"
  project_id      = var.project_id
  region          = var.region
  name            = "firstdue-console"
  image           = var.console_image
  service_account = module.iam.service_emails["firstdue-console"]

  environment_variables = {
    NODE_ENV = "production"
    # A plain server variable, deliberately not the NEXT_PUBLIC_ one. Next.js
    # inlines `process.env.NEXT_PUBLIC_*` at *build* time -- in server route
    # handlers as well as client bundles -- and the Docker build sets no such
    # value, so a NEXT_PUBLIC name compiles to undefined and whatever Cloud Run
    # supplies at runtime is never read. The gateway route handler reads this
    # name first and treats the NEXT_PUBLIC one only as a local-dev fallback.
    FIRSTDUE_API_BASE_URL = module.incident_service.url
    # The console holds no static token. It mints an OIDC token from the
    # metadata server for the incident service's audience, which is what that
    # service verifies against and what Cloud Run checks before admitting the
    # request. Its invoker binding is on the incident service below.
    FIRSTDUE_API_AUDIENCE = local.incident_audience
  }

  min_instances         = 0
  max_instances         = 2
  cpu                   = "1"
  memory                = "512Mi"
  timeout_seconds       = 300
  allow_unauthenticated = true
  health_path           = "/api/health"
  ready_path            = "/api/health"
}

# One Cloud Run service per agent, each running as its own service account.
# Without these the per-agent identities in the iam module bind roles nothing
# runs as, and least privilege is a property of the bindings rather than of the
# processes.
module "agent_workers" {
  source      = "../../modules/agent-workers"
  project_id  = var.project_id
  region      = var.region
  image       = var.backend_image
  policy_file = "${local.policy_dir}/subscriptions.json"

  agent_service_accounts = module.iam.agent_emails

  environment_variables        = local.common_env
  secret_environment_variables = local.secret_env

  invokers = {
    pubsub_push = module.iam.service_emails["firstdue-pubsub-push"]
    scheduler   = module.iam.service_emails["firstdue-scheduler"]
  }

  depends_on = [module.firestore, module.secrets]
}

module "pubsub" {
  source                    = "../../modules/pubsub"
  project_id                = var.project_id
  policy_file               = "${local.policy_dir}/topics.json"
  subscriptions_policy_file = "${local.policy_dir}/subscriptions.json"

  push_endpoint        = "${module.slow_service.url}/api/v1/internal/events/push"
  push_service_account = module.iam.service_emails["firstdue-pubsub-push"]
  push_audience        = local.slow_audience

  # Each agent's subscriptions push to that agent's own worker.
  agent_push_endpoints = {
    for id, url in module.agent_workers.worker_urls :
    id => "${url}/api/v1/internal/events/push"
  }

  # Endpoint and audience are different strings. A subscription pushes to the
  # worker's generated URL and mints its token for the worker's own audience;
  # minting for the slow loop's audience -- which is what one shared value did
  # -- means Cloud Run rejects the push before the app ever sees it.
  agent_push_audiences = module.agent_workers.worker_audiences

  depends_on = [module.slow_service, module.agent_workers]
}

module "scheduler" {
  source          = "../../modules/scheduler"
  project_id      = var.project_id
  region          = var.region
  environment     = local.environment
  target_url      = "${module.slow_service.url}/api/v1/internal/scheduler/tick"
  service_account = module.iam.service_emails["firstdue-scheduler"]
  audience        = local.slow_audience
  district_id     = var.district_id
  paused          = var.scheduler_paused

  depends_on = [module.slow_service]
}

module "vectors" {
  source      = "../../modules/vector-search"
  project_id  = var.project_id
  region      = var.region
  environment = local.environment
  enabled     = var.vector_search_enabled

  depends_on = [module.services]
}

module "memory_bank" {
  source      = "../../modules/memory-bank"
  project_id  = var.project_id
  environment = local.environment
  engine_id   = var.memory_bank_engine_id

  depends_on = [module.services]
}

module "observability" {
  source                = "../../modules/observability"
  project_id            = var.project_id
  environment           = local.environment
  notification_emails   = var.alert_emails
  incident_service_name = module.incident_service.name

  depends_on = [module.services]
}

module "budget" {
  source                = "../../modules/budget"
  project_id            = var.project_id
  environment           = local.environment
  billing_account       = var.billing_account
  amount_usd            = var.budget_usd
  notification_channels = module.observability.notification_channels

  depends_on = [module.services]
}
