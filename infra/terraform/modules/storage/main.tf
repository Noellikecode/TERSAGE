# Two buckets: pre-incident plans, and Terraform remote state.
#
# Plans are versioned because a pre-incident plan is a document a crew may have
# read. Overwriting one silently would make "what did the plan say at 02:14"
# unanswerable, and that question gets asked after an incident.

variable "project_id" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "plans_bucket" { type = string }

variable "retention_days" {
  description = "How long a superseded plan version is kept."
  type        = number
  default     = 365
}

resource "google_storage_bucket" "plans" {
  project                     = var.project_id
  name                        = var.plans_bucket
  location                    = var.region
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age                = var.retention_days
      with_state         = "ARCHIVED"
      num_newer_versions = 3
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    system      = "firstdue"
    environment = var.environment
  }
}

output "plans_bucket" {
  value = google_storage_bucket.plans.name
}
