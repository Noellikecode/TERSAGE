# One Cloud Run service, parameterised. The environment instantiates it three
# times: the slow loop, the incident loop, and the console.
#
# The two loops are separate services rather than one process with two roles,
# because they have opposite shapes. The slow loop runs for minutes over 3,800
# structures and can scale to zero between passes. The incident loop must answer
# in under 500 ms and cannot afford a cold start on dispatch. Separating them
# also separates their identities, which is what makes the per-agent IAM above
# mean anything at runtime.

variable "project_id" { type = string }
variable "region" { type = string }
variable "name" { type = string }
variable "image" {
  description = "Full image reference. Prefer a digest over a tag."
  type        = string
}
variable "service_account" { type = string }
variable "environment_variables" {
  type    = map(string)
  default = {}
}
variable "secret_environment_variables" {
  description = "env var name -> {secret = resource name, version = 'latest'}"
  type = map(object({
    secret  = string
    version = string
  }))
  default = {}
}
variable "min_instances" {
  description = "Zero for the slow loop; one for the incident loop."
  type        = number
  default     = 0
}
variable "max_instances" {
  description = "A hard ceiling, not a target. A runaway loop cannot outrun the budget alert, so the ceiling is what actually bounds spend."
  type        = number
  default     = 4
}
variable "cpu" {
  type    = string
  default = "1"
}
variable "memory" {
  type    = string
  default = "1Gi"
}
variable "timeout_seconds" {
  description = "Long enough for an SSE brief to stream, short enough that a wedged request is reclaimed."
  type        = number
  default     = 900
}
variable "concurrency" {
  type    = number
  default = 40
}
variable "health_path" {
  type    = string
  default = "/healthz"
}
variable "ready_path" {
  type    = string
  default = "/readyz"
}
variable "allow_unauthenticated" {
  description = "The console is public and authenticates its own users; the backends are not."
  type        = bool
  default     = false
}
variable "invokers" {
  description = "SA emails allowed to invoke this service (scheduler, Pub/Sub push)."
  type        = list(string)
  default     = []
}
variable "cpu_boost" {
  type    = bool
  default = true
}

resource "google_cloud_run_v2_service" "service" {
  project             = var.project_id
  location            = var.region
  name                = var.name
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = var.service_account
    timeout                          = "${var.timeout_seconds}s"
    max_instance_request_concurrency = var.concurrency

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        # A cold instant brief is the one latency that matters, so the first
        # request on a new instance gets the extra CPU.
        startup_cpu_boost = var.cpu_boost
      }

      dynamic "env" {
        for_each = var.environment_variables
        content {
          name  = env.key
          value = env.value
        }
      }

      # Secrets arrive as a reference, resolved by Cloud Run at start. The
      # value never appears in the service description, so
      # `gcloud run services describe` is safe to paste into a ticket.
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

      ports {
        container_port = 8000
      }

      startup_probe {
        http_get {
          path = var.ready_path
        }
        initial_delay_seconds = 2
        period_seconds        = 3
        timeout_seconds       = 3
        failure_threshold     = 10
      }

      liveness_probe {
        http_get {
          path = var.health_path
        }
        period_seconds    = 30
        timeout_seconds   = 5
        failure_threshold = 3
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count = var.allow_unauthenticated ? 1 : 0

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.service.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "invoker" {
  for_each = toset(var.invokers)

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.service.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${each.value}"
}

output "url" {
  value = google_cloud_run_v2_service.service.uri
}

output "name" {
  value = google_cloud_run_v2_service.service.name
}
