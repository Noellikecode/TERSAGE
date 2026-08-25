# Vertex AI Agent Engine Memory Bank -- the grants, not the instance.
#
# The Memory Bank holds the *prose* half of an open question, so a watcher can
# ask "has anyone asked something like this" before opening a thread another
# agent is already waiting on. The record itself -- eliminations, evidence,
# transitions, checkpoints -- stays in Firestore, because a `Memory.fact` is
# capped at 2048 characters and a long-running thread's accumulated eliminations
# do not fit. See `backend/src/firstdue/adapters/vertex/threads.py`.
#
# WHY THIS MODULE DOES NOT CREATE THE INSTANCE
#
# The Google provider has no `google_vertex_ai_reasoning_engine` resource --
# checked against the schema of the version pinned in `backend.tf`, not assumed.
# Memories hang off a `reasoningEngines/{id}` parent, so that parent has to
# exist before anything here is useful, and Terraform cannot make it.
#
# So it is created out of band and named, the same way secret *values* are:
#
#   .venv/bin/python -c "import vertexai; from vertexai import agent_engines; \
#     vertexai.init(project='PROJECT', location='us-central1'); \
#     print(agent_engines.create(display_name='firstdue-memory-bank').resource_name)"
#
# and the numeric id off the end of that goes into `memory_bank_engine_id` in
# terraform.tfvars. An empty id is a supported state, not a broken one: the
# application falls back to the in-memory thread index, which is what fake mode
# runs, and recall is then per-instance rather than durable.
#
# WHAT IT DOES CREATE
#
# The one grant the service needs and nothing else. `create_memory` embeds the
# fact server-side, under the Reasoning Engine service agent rather than under
# the caller's identity -- so without prediction rights on the project, every
# write fails with a 403 naming a publisher model, which is a confusing way to
# discover a missing role. Found exactly that way.

variable "project_id" { type = string }
variable "environment" { type = string }

variable "engine_id" {
  description = <<-EOT
    Numeric id of the Agent Engine instance whose Memory Bank holds question
    prose. Created out of band -- see the header. Empty disables the managed
    index and leaves recall on the in-memory one.
  EOT
  type        = string
  default     = ""
}

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  enabled = var.engine_id != ""

  # The service agent that runs Agent Engine work, including the embedding call
  # behind every create_memory. Google creates it on first use of the API; it is
  # referenced rather than created because the provider offers no resource for
  # one, and a binding for a principal that does not exist yet is still valid.
  reasoning_engine_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
}

# Prediction rights for the embedding model behind create_memory.
resource "google_project_iam_member" "reasoning_engine_predicts" {
  count   = local.enabled ? 1 : 0
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = local.reasoning_engine_agent
}

output "engine_id" {
  value       = var.engine_id
  description = "Passed to the services as MEMORY_BANK_ENGINE_ID."
}

output "enabled" {
  value       = local.enabled
  description = "Whether a managed Memory Bank backs thread recall."
}
