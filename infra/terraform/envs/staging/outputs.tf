output "incident_url" {
  description = "STAGING_BASE_URL for the smoke test."
  value       = module.incident_service.url
}

output "slow_url" {
  value = module.slow_service.url
}

output "console_url" {
  value = module.console_service.url
}

output "image_repository" {
  value = module.registry.repository_url
}

output "agent_service_accounts" {
  description = "Review before apply: one identity per agent, roles from its own scopes."
  value       = module.iam.agent_roles
}

output "firestore_indexes" {
  value = module.firestore.index_count
}

output "dead_letter_topics" {
  value = module.pubsub.dead_letter_topic_names
}
