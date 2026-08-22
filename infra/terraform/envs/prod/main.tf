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

  # Keys are static; the emails resolve at apply time. See the module's
  # `accessors` description for why that distinction matters.
  accessors = {
    callback-secret = {
      slow     = module.iam.service_emails["firstdue-slow"]
      incident = module.iam.service_emails["firstdue-incident"]
    }
    console-token-secret = {
      slow     = module.iam.service_emails["firstdue-slow"]
      incident = module.iam.service_emails["firstdue-incident"]
      console  = module.iam.service_emails["firstdue-console"]
    }
  }

  depends_on = [module.services]
}

locals {
  common_env = {
    APP_ENV                       = local.environment
    USE_FAKE_AGENTS               = "false"
    STORAGE_BACKEND               = "firestore"
    EVENT_BACKEND                 = "pubsub"
    GCP_PROJECT_ID                = var.project_id
    VERTEX_LOCATION               = var.region
    GEMINI_MODEL                  = var.gemini_model
    GCS_PLANS_BUCKET              = module.storage.plans_bucket
    MODEL_ARMOR_TEMPLATE          = var.model_armor_template
    OTEL_ENABLED                  = "true"
    OTEL_SERVICE_NAME             = "firstdue"
    VECTOR_SEARCH_ENABLED         = tostring(var.vector_search_enabled)
    VECTOR_SEARCH_ENDPOINT        = module.vectors.endpoint_id
    LOG_JSON                      = "true"
    INTERNAL_PUSH_SERVICE_ACCOUNT = module.iam.service_emails["firstdue-pubsub-push"]
  }

  secret_env = {
    CALLBACK_SECRET = {
      secret  = module.secrets.secret_names["callback-secret"]
      version = "latest"
    }
    CONSOLE_TOKEN_SECRET = {
      secret  = module.secrets.secret_names["console-token-secret"]
      version = "latest"
    }
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

  environment_variables        = merge(local.common_env, { FIRSTDUE_LOOP = "slow" })
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

  environment_variables        = merge(local.common_env, { FIRSTDUE_LOOP = "incident" })
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
    NODE_ENV              = "production"
    FIRSTDUE_API_BASE_URL = module.incident_service.url
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
  push_audience        = module.slow_service.url

  # Each agent's subscriptions push to that agent's own worker.
  agent_push_endpoints = {
    for id, url in module.agent_workers.worker_urls :
    id => "${url}/api/v1/internal/events/push"
  }

  depends_on = [module.slow_service, module.agent_workers]
}

module "scheduler" {
  source          = "../../modules/scheduler"
  project_id      = var.project_id
  region          = var.region
  environment     = local.environment
  target_url      = "${module.slow_service.url}/api/v1/internal/scheduler/tick"
  service_account = module.iam.service_emails["firstdue-scheduler"]
  audience        = module.slow_service.url
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
