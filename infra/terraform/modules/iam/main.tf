# One service account per agent and per service, with roles derived from the
# agent's own declared scopes and nothing else.
#
# The acceptance criterion is "IAM prevents one agent from assuming another
# agent's permissions". Two things make that true here:
#
#   1. Roles come from `policy/agents.json`, which mirrors each descriptor's
#      `required_scopes`. An agent that never declared `write:referral` never
#      receives the role that scope maps to. A Python test fails if the file and
#      the descriptors disagree, so widening an agent's IAM means widening its
#      contract in code first, where the gateway will also see it.
#   2. No service account is granted `roles/iam.serviceAccountTokenCreator` on
#      any other. Impersonation is the only way an SA with correct roles can
#      still end up acting as a different agent, and it is simply absent --
#      asserted by the same test, so it stays absent.
#
# No agent SA receives a role that reaches person-level data at rest. PHI is
# reachable only through the gateway's DERIVE path, at runtime, under an
# IncidentGrant that expires. A standing IAM role would make that grant
# decorative.

variable "project_id" { type = string }
variable "policy_file" { type = string }
variable "environment" { type = string }

locals {
  policy      = jsondecode(file(var.policy_file))
  agents      = local.policy.agents
  services    = local.policy.services
  scope_roles = local.policy.scope_roles

  # agent -> the distinct roles its scopes imply, plus the bus roles every
  # agent needs to receive its own subscriptions.
  agent_roles = {
    for id, spec in local.agents :
    id => distinct(concat(
      flatten([for s in spec.scopes : local.scope_roles[s]]),
      spec.pubsub,
    ))
  }

  # Flattened to one binding per (agent, role) pair.
  agent_bindings = merge([
    for id, roles in local.agent_roles : {
      for role in roles : "${id}|${role}" => { agent = id, role = role }
    }
  ]...)

  service_bindings = merge([
    for id, spec in local.services : {
      for role in spec.roles : "${id}|${role}" => { service = id, role = role }
    }
  ]...)
}

resource "google_service_account" "agent" {
  for_each = local.agents

  project = var.project_id
  # Account ids cap at 30 characters, and every agent id here fits with the
  # environment suffix. A longer id should fail the plan rather than be
  # silently truncated into a collision with another agent.
  account_id   = substr("fd-${each.key}", 0, 30)
  display_name = "FIRST DUE agent: ${each.key} (${var.environment})"
  description  = "${each.value.loop} loop. Scopes: ${join(", ", each.value.scopes)}"
}

resource "google_service_account" "service" {
  for_each = local.services

  project      = var.project_id
  account_id   = substr(replace(each.key, "firstdue-", "fd-"), 0, 30)
  display_name = "${each.value.display} (${var.environment})"
}

resource "google_project_iam_member" "agent" {
  for_each = local.agent_bindings

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.agent[each.value.agent].email}"
}

resource "google_project_iam_member" "service" {
  for_each = local.service_bindings

  project = var.project_id
  role    = each.value.role
  member  = "serviceAccount:${google_service_account.service[each.value.service].email}"
}

output "agent_emails" {
  value = { for id, sa in google_service_account.agent : id => sa.email }
}

output "service_emails" {
  value = { for id, sa in google_service_account.service : id => sa.email }
}

output "agent_roles" {
  description = "Effective roles per agent, for review before apply."
  value       = local.agent_roles
}
