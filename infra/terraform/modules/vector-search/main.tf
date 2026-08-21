# Vector Search, off by default.
#
# The index itself is nearly free; the *endpoint* bills for provisioned serving
# nodes around the clock, whether or not anything queries it, and that does not
# fit under a 50 USD cap. So `enabled = false` creates nothing at all, and the
# application's semantic search degrades to the deterministic keyword path --
# which is a documented, tested state rather than a failure.
#
# Nothing classified above PUBLIC is ever embedded. That rule lives in
# `domain/vectors.py` as VECTOR_FORBIDDEN_CLASSIFICATIONS, is enforced by the
# serializer, and is re-checked by the adapter before any upsert. Terraform
# cannot enforce it; it is noted here so that turning this on does not read as
# permission to index everything.

variable "project_id" { type = string }
variable "region" { type = string }
variable "environment" { type = string }
variable "enabled" {
  type    = bool
  default = false
}
variable "dimensions" {
  description = "Must match the embedding model named in VECTOR_EMBEDDING_MODEL."
  type        = number
  default     = 768
}
variable "min_replica_count" {
  type    = number
  default = 1
}
variable "max_replica_count" {
  type    = number
  default = 1
}
variable "machine_type" {
  type    = string
  default = "e2-standard-2"
}

resource "google_vertex_ai_index" "index" {
  count = var.enabled ? 1 : 0

  project             = var.project_id
  region              = var.region
  display_name        = "firstdue-${var.environment}"
  description         = "PUBLIC-classification structural text only"
  index_update_method = "STREAM_UPDATE"

  metadata {
    config {
      dimensions                  = var.dimensions
      approximate_neighbors_count = 20
      distance_measure_type       = "DOT_PRODUCT_DISTANCE"
      algorithm_config {
        tree_ah_config {
          leaf_node_embedding_count    = 500
          leaf_nodes_to_search_percent = 10
        }
      }
    }
  }
}

resource "google_vertex_ai_index_endpoint" "endpoint" {
  count = var.enabled ? 1 : 0

  project                 = var.project_id
  region                  = var.region
  display_name            = "firstdue-${var.environment}"
  public_endpoint_enabled = true
}

output "index_id" {
  value = var.enabled ? google_vertex_ai_index.index[0].id : ""
}

output "endpoint_id" {
  value = var.enabled ? google_vertex_ai_index_endpoint.endpoint[0].id : ""
}
