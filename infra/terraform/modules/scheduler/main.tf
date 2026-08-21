# The slow loop's heartbeat.
#
# Phase 3 built `poll` and left it to be called by something. This is that
# something: an authenticated POST to /internal/scheduler/tick, on a cadence
# measured in hours because the sources it reads are updated in days.

variable "project_id" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "target_url" {
  description = "Full https URL of POST /internal/scheduler/tick."
  type        = string
}
variable "service_account" {
  description = "SA the scheduler authenticates as. Needs run.invoker on the target."
  type        = string
}
variable "audience" { type = string }
variable "district_id" { type = string }
variable "schedule" {
  description = "Default: 03:00 daily, outside the hours a district is busy."
  type        = string
  default     = "0 3 * * *"
}
variable "time_zone" {
  type    = string
  default = "America/Los_Angeles"
}
variable "paused" {
  description = "Staging starts paused; a tick that runs before the first smoke test is noise."
  type        = bool
  default     = false
}

resource "google_cloud_scheduler_job" "slow_loop" {
  project     = var.project_id
  region      = var.region
  name        = "firstdue-${var.environment}-slow-loop"
  description = "One slow-loop pass over ${var.district_id}"
  schedule    = var.schedule
  time_zone   = var.time_zone
  paused      = var.paused

  # A tick is idempotent -- derived ids mean a repeated pass writes nothing new
  # -- so retrying a failed tick is safe rather than merely tolerated.
  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
    max_doublings        = 2
  }

  attempt_deadline = "1800s"

  http_target {
    http_method = "POST"
    uri         = var.target_url
    headers = {
      "Content-Type" = "application/json"
    }
    body = base64encode(jsonencode({
      district_id = var.district_id
      reason      = "scheduled"
    }))

    oidc_token {
      service_account_email = var.service_account
      audience              = var.audience
    }
  }
}

output "job_name" {
  value = google_cloud_scheduler_job.slow_loop.name
}
