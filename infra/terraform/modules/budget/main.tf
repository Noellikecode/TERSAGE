# A hard spend ceiling, alerted in three steps.
#
# The alert is the second line of defence, not the first. It arrives by email,
# minutes late, and cannot stop anything -- the thing that actually bounds spend
# is `max_instance_count` on each Cloud Run service and the Vector Search
# endpoint being off by default. This makes the overrun visible when those fail.

variable "billing_account" {
  description = "Billing account id, e.g. 000000-AAAAAA-BBBBBB."
  type        = string
}
variable "project_id" { type = string }
variable "environment" { type = string }
variable "amount_usd" {
  type    = number
  default = 50
}
variable "notification_channels" {
  type    = list(string)
  default = []
}
variable "thresholds" {
  type    = list(number)
  default = [0.5, 0.9, 1.0]
}

data "google_project" "this" {
  project_id = var.project_id
}

resource "google_billing_budget" "budget" {
  billing_account = var.billing_account
  display_name    = "FIRST DUE ${var.environment} (${var.amount_usd} USD/month)"

  budget_filter {
    projects               = ["projects/${data.google_project.this.number}"]
    calendar_period        = "MONTH"
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.amount_usd)
    }
  }

  dynamic "threshold_rules" {
    for_each = var.thresholds
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  # Forecast crossing the cap matters more than current spend: it is the only
  # signal that arrives before the money is gone.
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  dynamic "all_updates_rule" {
    for_each = length(var.notification_channels) == 0 ? [] : [1]
    content {
      monitoring_notification_channels = var.notification_channels
      disable_default_iam_recipients   = false
      schema_version                   = "1.0"
    }
  }
}

output "budget_name" {
  value = google_billing_budget.budget.display_name
}
