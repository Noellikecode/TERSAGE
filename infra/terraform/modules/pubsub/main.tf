# One topic per Topic enum member, each with a push subscription and its own
# dead-letter topic.
#
# Push, not pull, because the receiving endpoint is already written and already
# authenticated: `/internal/events/push` verifies an OIDC token minted for a
# specific service account and audience. A pull subscriber would need a
# long-lived worker; a push subscription needs nothing running when the bus is
# quiet, which is what keeps this inside the budget.

variable "project_id" { type = string }
variable "policy_file" { type = string }
variable "push_endpoint" {
  description = "Full https URL of POST /internal/events/push."
  type        = string
}
variable "push_service_account" {
  description = "SA email Pub/Sub authenticates as. The backend checks it."
  type        = string
}
variable "push_audience" {
  description = "OIDC audience the backend requires. Usually the service URL."
  type        = string
}
variable "max_delivery_attempts" {
  description = "Deliveries before a message is a dead letter, not a retry."
  type        = number
  default     = 5
}
variable "message_retention" {
  type    = string
  default = "604800s" # 7 days
}
variable "subscriptions_policy_file" {
  description = "policy/subscriptions.json -- agent -> the topics it consumes."
  type        = string
}
variable "agent_push_endpoints" {
  description = "agent id -> full https URL of that worker's push endpoint."
  type        = map(string)
}

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  topics = jsondecode(file(var.policy_file)).topics

  # Per-agent routing. A subscription has to name the service that actually
  # handles its topic; one pointed at a service that does not is an event that
  # dead-letters forever while every dashboard looks healthy. The map comes
  # from backend/src/firstdue/registry/routing.py, and tests/infra fails if the
  # two disagree.
  routing = jsondecode(file(var.subscriptions_policy_file)).agents

  # One subscription per (agent, topic) pair the agent consumes.
  agent_subscriptions = merge([
    for agent, spec in local.routing : {
      for topic in spec.topics : "${agent}|${topic}" => {
        agent = agent
        topic = topic
      }
    }
  ]...)

  # Pub/Sub's own service agent needs publisher on the dead-letter topic and
  # subscriber on the subscription to move a message across. Without both, the
  # message is retried forever and the dead-letter policy silently does nothing.
  pubsub_agent = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic" "topic" {
  for_each = toset(local.topics)

  project                    = var.project_id
  name                       = replace(each.value, ".", "-")
  message_retention_duration = var.message_retention

  labels = {
    system = "firstdue"
  }
}

# A dead letter that lands nowhere is a lost event. Phase 2 carried that risk
# on an in-memory store; here the letters outlive the process that failed.
resource "google_pubsub_topic" "dead_letter" {
  for_each = toset(local.topics)

  project                    = var.project_id
  name                       = "${replace(each.value, ".", "-")}-dead-letter"
  message_retention_duration = var.message_retention
}

resource "google_pubsub_subscription" "push" {
  for_each = toset(local.topics)

  project = var.project_id
  name    = "${replace(each.value, ".", "-")}-push"
  topic   = google_pubsub_topic.topic[each.value].id

  # The handler is idempotent by construction -- derived ids mean a redelivered
  # event writes the same document -- so at-least-once delivery is safe and the
  # ack deadline can stay short.
  ack_deadline_seconds       = 30
  message_retention_duration = var.message_retention
  retain_acked_messages      = false

  push_config {
    push_endpoint = var.push_endpoint
    oidc_token {
      service_account_email = var.push_service_account
      audience              = var.push_audience
    }
  }

  retry_policy {
    minimum_backoff = "1s"
    maximum_backoff = "60s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter[each.value].id
    max_delivery_attempts = var.max_delivery_attempts
  }

  expiration_policy {
    ttl = "" # never expire; a quiet week is not a reason to delete a subscription
  }
}

# One subscription per agent per topic it consumes, each pushing to that
# agent's own worker. This is the routing phase 2's notes said was "Terraform's
# job and not yet written".
resource "google_pubsub_subscription" "agent_push" {
  for_each = local.agent_subscriptions

  project = var.project_id
  name    = "${replace(each.value.topic, ".", "-")}-${each.value.agent}"
  topic   = google_pubsub_topic.topic[each.value.topic].id

  ack_deadline_seconds       = 30
  message_retention_duration = var.message_retention
  retain_acked_messages      = false

  push_config {
    push_endpoint = var.agent_push_endpoints[each.value.agent]
    oidc_token {
      service_account_email = var.push_service_account
      audience              = var.push_audience
    }
  }

  retry_policy {
    minimum_backoff = "1s"
    maximum_backoff = "60s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter[each.value.topic].id
    max_delivery_attempts = var.max_delivery_attempts
  }

  expiration_policy {
    ttl = ""
  }
}

resource "google_pubsub_subscription_iam_member" "agent_dead_letter_subscriber" {
  for_each = local.agent_subscriptions

  project      = var.project_id
  subscription = google_pubsub_subscription.agent_push[each.key].name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}

resource "google_pubsub_topic_iam_member" "dead_letter_publisher" {
  for_each = toset(local.topics)

  project = var.project_id
  topic   = google_pubsub_topic.dead_letter[each.value].name
  role    = "roles/pubsub.publisher"
  member  = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "dead_letter_subscriber" {
  for_each = toset(local.topics)

  project      = var.project_id
  subscription = google_pubsub_subscription.push[each.value].name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}

output "topic_names" {
  value = [for t in google_pubsub_topic.topic : t.name]
}

output "dead_letter_topic_names" {
  value = [for t in google_pubsub_topic.dead_letter : t.name]
}

output "agent_subscription_names" {
  value = { for k, s in google_pubsub_subscription.agent_push : k => s.name }
}
