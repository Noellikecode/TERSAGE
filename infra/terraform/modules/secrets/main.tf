# Secret containers, not secret values.
#
# Terraform creates the container and grants read access to exactly the service
# accounts that need it. The version is added out of band, by a human running
# `gcloud secrets versions add`, so no secret value ever enters this repository,
# a plan file, or the state bucket.

variable "project_id" { type = string }
variable "environment" { type = string }
variable "accessors" {
  description = <<-EOT
    Secret short name -> { accessor key -> service account email }.

    A map rather than a list, and the reason is load-bearing: the IAM grants
    below are created with `for_each`, and Terraform must know every *key* at
    plan time. Service account emails are resolved during apply, so keying the
    grants by email made the whole plan unresolvable -- `tofu import` and any
    single-pass apply failed with "Invalid for_each argument".

    So the caller supplies a statically-known accessor key, and only the email
    it maps to is allowed to be an apply-time value.
  EOT
  type        = map(map(string))
}
variable "replication_locations" {
  description = "Empty means automatic replication."
  type        = list(string)
  default     = []
}

resource "google_secret_manager_secret" "secret" {
  for_each = var.accessors

  project   = var.project_id
  secret_id = "firstdue-${var.environment}-${each.key}"

  labels = {
    system = "firstdue"
  }

  dynamic "replication" {
    for_each = length(var.replication_locations) == 0 ? [1] : []
    content {
      auto {}
    }
  }

  dynamic "replication" {
    for_each = length(var.replication_locations) == 0 ? [] : [1]
    content {
      user_managed {
        dynamic "replicas" {
          for_each = var.replication_locations
          content {
            location = replicas.value
          }
        }
      }
    }
  }
}

locals {
  # Keys are static: "<secret>|<accessor key>". Only `member` is unknown until
  # the service accounts exist, and an unknown *value* is fine.
  grants = merge([
    for name, members in var.accessors : {
      for key, email in members : "${name}|${key}" => { secret = name, member = email }
    }
  ]...)
}

resource "google_secret_manager_secret_iam_member" "accessor" {
  for_each = local.grants

  project   = var.project_id
  secret_id = google_secret_manager_secret.secret[each.value.secret].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value.member}"
}

output "secret_names" {
  description = "Full resource names, for the *_SECRET_NAME settings."
  value       = { for k, s in google_secret_manager_secret.secret : k => s.name }
}
