# One Cloud Run service per agent, each running as its own service account.
#
# Before this existed the deployment had three services and eleven agent service
# accounts, and nothing ran as any of them. The least-privilege IAM was real and
# load-bearing on paper only: every agent's work executed inside a service whose
# identity was the union of all of them, so "IAM prevents one agent from
# assuming another agent's permissions" was true of the bindings and false of
# the processes.
#
# Each worker is the same image with FIRSTDUE_AGENT set. It serves health and
# the internal push endpoint and nothing else -- an agent worker is not a
# console and has no console routes to expose.
#
# Scale to zero is the point. Eleven services that cost nothing at rest is a
# different proposition from eleven that are always warm, and only the incident
# loop keeps an instance alive.

variable "project_id" { type = string }
variable "region" { type = string }
variable "image" { type = string }
variable "policy_file" {
  description = "policy/subscriptions.json -- agent -> the topics it consumes."
  type        = string
}
variable "agent_service_accounts" {
  description = "agent id -> service account email, from the iam module."
  type        = map(string)
}
variable "environment_variables" { type = map(string) }
variable "secret_environment_variables" {
  type    = map(object({ secret = string, version = string }))
  default = {}
}
variable "invokers" {
  description = "Caller key -> SA email allowed to invoke every worker. Keys must be statically known; see the cloud-run module for why."
  type        = map(string)
  default     = {}
}

locals {
  policy = jsondecode(file(var.policy_file))

  # An agent with no subscriptions still gets a service: it is driven by the
  # scheduler or by an HTTP request, and it still needs its own identity.
  agents = local.policy.agents

  # The incident loop keeps one instance warm, because a cold start on dispatch
  # is the one latency this system exists to avoid. The slow loop does not:
  # nothing is waiting on a district poll.
  warm = {
    for id, spec in local.agents : id => spec.loop == "incident" ? 1 : 0
  }

  # Each worker's own OIDC audience, chosen before apply.
  #
  # The backend refuses to start in live mode without INTERNAL_PUSH_AUDIENCE,
  # and it must be *this* worker's audience: a push token minted for one worker
  # must not be accepted by another. The generated URL cannot be that value --
  # it does not exist until the service is created, so feeding it back into the
  # same service's environment is a cycle. A custom audience is a stable string
  # derived from the name, which the URL is not needed for.
  audiences = {
    for id, _ in local.agents : id => "https://firstdue-agent-${id}"
  }
}

resource "google_cloud_run_v2_service" "worker" {
  for_each = local.agents

  project             = var.project_id
  location            = var.region
  name                = "firstdue-agent-${each.key}"
  deletion_protection = false
  custom_audiences    = [local.audiences[each.key]]

  # No unauthenticated ingress. A worker is reached by Pub/Sub push and by the
  # scheduler, both of which authenticate.
  ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    service_account                  = var.agent_service_accounts[each.key]
    max_instance_request_concurrency = 10
    timeout                          = "900s"

    scaling {
      min_instance_count = local.warm[each.key]
      max_instance_count = 3
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
      }

      dynamic "env" {
        for_each = var.environment_variables
        content {
          name  = env.key
          value = env.value
        }
      }

      # The two variables that make this worker *this* agent.
      env {
        name  = "FIRSTDUE_AGENT"
        value = each.key
      }

      env {
        name  = "FIRSTDUE_LOOP"
        value = local.agents[each.key].loop
      }

      # Per worker, and deliberately not part of the shared environment map: a
      # single value across every service would mean any worker accepted a push
      # token minted for any other.
      env {
        name  = "INTERNAL_PUSH_AUDIENCE"
        value = local.audiences[each.key]
      }

      dynamic "env" {
        for_each = var.secret_environment_variables
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = env.value.secret
              version = env.value.version
            }
          }
        }
      }

      startup_probe {
        http_get { path = "/healthz" }
        initial_delay_seconds = 2
        period_seconds        = 3
        failure_threshold     = 10
      }

      liveness_probe {
        http_get { path = "/healthz" }
        period_seconds = 30
      }
    }
  }

  labels = {
    system = "firstdue"
    agent  = replace(each.key, ".", "-")
  }
}

# Explicit invoker bindings, one per (worker, caller). Cloud Run defaults to
# refusing, and this is the only thing that opens it.
resource "google_cloud_run_v2_service_iam_member" "invoker" {
  # "<worker>|<caller key>". Both halves are statically known; only the email
  # in the value resolves at apply time.
  for_each = merge([
    for id, _ in local.agents : {
      for caller, sa in var.invokers : "${id}|${caller}" => { worker = id, member = sa }
    }
  ]...)

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.worker[each.value.worker].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value.member}"
}

output "worker_urls" {
  value = { for id, svc in google_cloud_run_v2_service.worker : id => svc.uri }
}

output "worker_audiences" {
  description = "agent id -> the OIDC audience that worker accepts. Push subscriptions mint tokens for this."
  value       = local.audiences
}

output "worker_names" {
  value = { for id, svc in google_cloud_run_v2_service.worker : id => svc.name }
}
