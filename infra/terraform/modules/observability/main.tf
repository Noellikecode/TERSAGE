# Log retention, and the alerts worth waking someone for.
#
# The metrics themselves are emitted by the application through OpenTelemetry;
# this module only decides what is kept and what is escalated. Two of the three
# alerts are about *silence* rather than errors -- a slow loop that stops
# running and an incident loop that stops answering both look like health from
# an error-rate dashboard.

variable "project_id" { type = string }
variable "environment" { type = string }
variable "notification_emails" {
  type    = list(string)
  default = []
}
variable "log_retention_days" {
  description = "Audit events live in Firestore; these are the operational logs."
  type        = number
  default     = 30
}
variable "incident_service_name" { type = string }

resource "google_logging_project_bucket_config" "default" {
  project        = var.project_id
  location       = "global"
  bucket_id      = "_Default"
  retention_days = var.log_retention_days
}

resource "google_monitoring_notification_channel" "email" {
  for_each = toset(var.notification_emails)

  project      = var.project_id
  display_name = "FIRST DUE ${var.environment}: ${each.value}"
  type         = "email"
  labels = {
    email_address = each.value
  }
}

locals {
  channels = [for c in google_monitoring_notification_channel.email : c.id]
}

resource "google_monitoring_alert_policy" "incident_5xx" {
  project      = var.project_id
  display_name = "FIRST DUE ${var.environment}: incident loop returning 5xx"
  combiner     = "OR"

  conditions {
    display_name = "5xx rate above zero over 5 minutes"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "resource.labels.service_name = \"${var.incident_service_name}\"",
        "metric.type = \"run.googleapis.com/request_count\"",
        "metric.labels.response_code_class = \"5xx\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = local.channels

  documentation {
    content   = "The incident loop is failing requests. A commander asking for a brief is getting an error page."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_alert_policy" "incident_latency" {
  project      = var.project_id
  display_name = "FIRST DUE ${var.environment}: instant brief slower than 500 ms"
  combiner     = "OR"

  conditions {
    display_name = "p95 request latency above the instant-brief budget"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"cloud_run_revision\"",
        "resource.labels.service_name = \"${var.incident_service_name}\"",
        "metric.type = \"run.googleapis.com/request_latencies\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = 500
      duration        = "300s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_PERCENTILE_95"
      }
    }
  }

  notification_channels = local.channels

  documentation {
    content   = "The instant brief has a local latency target of 500 ms and it is being missed. The brief is the product; a slow one is a commander reading nothing."
    mime_type = "text/markdown"
  }
}

output "notification_channels" {
  value = local.channels
}
