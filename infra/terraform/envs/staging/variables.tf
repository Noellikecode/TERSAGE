variable "project_id" {
  description = "GCP project for staging."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Artifact Registry, and Vertex."
  type        = string
  default     = "us-central1"
}

variable "firestore_location" {
  description = "Firestore location. Single-region for staging; it is cheaper and staging is not durable."
  type        = string
  default     = "us-central1"
}

variable "billing_account" {
  description = "Billing account id. Required for the budget."
  type        = string
}

variable "backend_image" {
  description = "Backend image. Prefer a digest: a rollback must name the exact image that was running."
  type        = string
}

variable "console_image" {
  description = "Frontend image."
  type        = string
}

variable "plans_bucket" {
  description = "Bucket for pre-incident plans. Globally unique."
  type        = string
}

variable "alert_emails" {
  description = "Who hears about a 5xx or a missed latency budget."
  type        = list(string)
  default     = []
}

variable "budget_usd" {
  type    = number
  default = 50
}

variable "district_id" {
  description = "District the scheduled slow loop walks."
  type        = string
  default     = "sffd-district-03"
}

variable "vector_search_enabled" {
  description = "A running index endpoint costs several hundred USD a month and cannot sit under the budget cap. Off unless deliberately turned on."
  type        = bool
  default     = false
}

variable "gemini_model" {
  type    = string
  default = "gemini-2.5-flash"
}

variable "model_armor_template" {
  description = "Full Model Armor template resource name. Empty disables the live screen and leaves the local detector as the only one -- which the console then reports."
  type        = string
  default     = ""
}

variable "scheduler_paused" {
  description = "Staging starts paused; a tick before the first smoke test is noise."
  type        = bool
  default     = true
}
