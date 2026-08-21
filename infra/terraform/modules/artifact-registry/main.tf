# One Docker repository per environment. Images are addressed by digest at
# deploy time, never by a moving tag -- a rollback has to name the exact image
# that was running, and `:latest` cannot.

variable "project_id" { type = string }
variable "region" { type = string }
variable "environment" { type = string }

variable "keep_image_count" {
  description = "Recent images retained. Older untagged images are deleted."
  type        = number
  default     = 10
}

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = "firstdue-${var.environment}"
  description   = "FIRST DUE container images (${var.environment})"
  format        = "DOCKER"

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = var.keep_image_count
    }
  }

  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s" # 7 days
    }
  }
}

output "repository_url" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}
