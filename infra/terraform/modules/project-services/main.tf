# Every API this system touches, enabled once, before anything that needs it.
#
# `disable_on_destroy = false` is deliberate: tearing down a FIRST DUE
# environment must not switch off an API another workload in the same project
# depends on. Teardown removes what this configuration created, not what it
# merely turned on.

variable "project_id" {
  description = "GCP project that hosts this environment."
  type        = string
}

variable "services" {
  description = "APIs to enable. Defaults cover every service the fleet uses."
  type        = list(string)
  default = [
    "run.googleapis.com",
    "firestore.googleapis.com",
    "pubsub.googleapis.com",
    "aiplatform.googleapis.com",
    "storage.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudbilling.googleapis.com",
    "billingbudgets.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ]
}

resource "google_project_service" "enabled" {
  for_each = toset(var.services)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

output "enabled" {
  description = "Enabled service ids, for depends_on wiring."
  value       = [for s in google_project_service.enabled : s.service]
}
