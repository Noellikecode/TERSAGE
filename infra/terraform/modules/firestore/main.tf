# The database and every composite index the query patterns need.
#
# The collection list is not written here. It is read from
# `policy/firestore.json`, which mirrors COLLECTION_NAMES in the Firestore
# adapter, and a Python test fails if the two disagree. An index that exists in
# code and not in Terraform is a query that works locally and fails in staging
# with a link to a console page -- at 3am, on an incident.

variable "project_id" { type = string }
variable "location" {
  description = "Firestore location. Multi-region for prod, single for staging."
  type        = string
}
variable "policy_file" {
  description = "Path to policy/firestore.json."
  type        = string
}
variable "deletion_policy" {
  description = "ABANDON keeps the database when the environment is destroyed."
  type        = string
  default     = "ABANDON"
}

locals {
  policy      = jsondecode(file(var.policy_file))
  collections = local.policy.collections

  # Flatten {collection -> [index -> fields]} into one keyed map, because
  # Terraform needs a stable unique key per resource instance.
  indexes = merge([
    for name, spec in local.collections : {
      for i, idx in spec.indexes :
      "${name}.${i}" => {
        collection = name
        fields     = idx.fields
        order      = idx.order
      }
    }
  ]...)
}

resource "google_firestore_database" "db" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.location
  type        = "FIRESTORE_NATIVE"

  # Point-in-time recovery is the only defence against a bad migration that
  # writes correct-looking data. Audit events are append-only in the
  # application; they are not append-only to a service account with
  # datastore.user.
  point_in_time_recovery_enablement = "POINT_IN_TIME_RECOVERY_ENABLED"
  concurrency_mode                  = "OPTIMISTIC"

  delete_protection_state = "DELETE_PROTECTION_ENABLED"
  deletion_policy         = var.deletion_policy
}

resource "google_firestore_index" "composite" {
  for_each = local.indexes

  project    = var.project_id
  database   = google_firestore_database.db.name
  collection = each.value.collection

  dynamic "fields" {
    for_each = each.value.fields
    content {
      field_path = fields.value
      order      = each.value.order[fields.key]
    }
  }
}

output "collection_count" {
  value = length(local.collections)
}

output "index_count" {
  value = length(local.indexes)
}
