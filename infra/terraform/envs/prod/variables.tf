variable "project_id" {
  description = "GCP project for prod."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Artifact Registry, and Vertex."
  type        = string
  default     = "us-central1"
}

variable "firestore_location" {
  description = "Firestore location. Multi-region for production: a single-region outage should not take the profile store with it."
  type        = string
  default     = "nam5"
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

variable "memory_bank_engine_id" {
  description = <<-EOT
    Numeric id of the Vertex AI Agent Engine instance whose Memory Bank holds
    the prose half of every open question, so recall can answer "has anyone
    asked something like this" rather than only "what is this district
    carrying".

    Created out of band, because the provider has no resource for one -- see
    `modules/memory-bank/main.tf`. Empty is a supported state: the application
    falls back to the in-memory thread index, which is what fake mode runs, and
    recall becomes per-instance rather than durable. The *record* is in
    Firestore either way, so an empty id costs findability and never memory.

    Unlike `vector_search_enabled` this is not a cost switch. Memory Bank bills
    per operation with no provisioned serving node, so it sits under the budget
    cap comfortably.
  EOT
  type        = string
  default     = ""
}

variable "memory_bank_location" {
  description = <<-EOT
    Region of the Agent Engine instance. Deliberately separate from
    `vertex_location`, which is `global` because that is the only place the
    Gemini models answer -- an Agent Engine instance is regional and has no
    global endpoint, so sharing the setting would point the parent path at
    nothing.
  EOT
  type        = string
  default     = "us-central1"
}

variable "gemini_model" {
  description = <<-EOT
    Verified 2026-08-21 against a real project, and the two halves are a trap
    for each other: `gemini-3.5-flash` 404s in `us-central1` and answers only on
    `global`, while `gemini-2.5-flash` is the opposite -- it resolves regionally,
    works, and so silently ships a model that does not meet the
    Gemini-3.5-or-newer requirement while every health check stays green.

    So the model and `vertex_location` move together, and neither follows the
    Cloud Run region.
  EOT
  type        = string
  default     = "gemini-3.5-flash"
}

variable "vertex_location" {
  description = <<-EOT
    Vertex endpoint location. `global`, not a region, and deliberately not tied
    to `var.region`: the models this system requires are not served regionally.
    Cloud Run, Artifact Registry, and Firestore keep using `region`.
  EOT
  type        = string
  default     = "global"
}

variable "live_source_keys" {
  description = <<-EOT
    Which optional third-party API keys have a Secret Manager version already
    added. Inbound data sources, and the outbound Resend key the referral mail
    goes through -- one opt-in list, because the failure they share is the one
    that matters.

    The containers are created either way -- Terraform never writes a value --
    but a key is only mounted into the services once it is listed here. Cloud
    Run refuses to start a container whose secret reference resolves to no
    version, so mounting an empty secret would take a service down rather than
    degrade the one capability that needs it. Add the version first:

        gcloud secrets versions add firstdue-<env>-nrel-api-key --data-file=-

    then list the key here. An unlisted data-source key leaves its source
    reporting UNCONFIGURED, which the console shows. An unlisted
    `resend-api-key` leaves referral email unconfigured: `referral-clerk` still
    stages the referral and a captain still approves it, nothing is mailed.
  EOT
  type        = set(string)
  default     = []

  validation {
    condition = length(setsubtract(
      var.live_source_keys,
      ["google-maps-api-key", "nrel-api-key", "resend-api-key", "socrata-app-token"],
    )) == 0
    error_message = "live_source_keys may name only google-maps-api-key, nrel-api-key, resend-api-key, or socrata-app-token."
  }
}

variable "resend_from_address" {
  description = <<-EOT
    From address for the inter-agency referral email Resend sends.

    Resend refuses a From on a domain that is not verified in the account, so
    this default deliberately mails nowhere: a deployment that actually sends
    sets an address on its own verified domain, and one that does not is no
    worse off than it already was.

    The address decides what the building department sees in the From line. It
    does not decide whether anything is sent -- `referral-clerk` never mails on
    its own; a captain approves first.
  EOT
  type        = string
  default     = "firstdue-referrals@example.org"
}

variable "grounding_search_enabled" {
  description = <<-EOT
    Google Search grounding for the GroundingService.

    Off by default for the same reason `vector_search_enabled` is off: every
    grounded generation bills a search alongside the model call, and a slow-loop
    pass over 3,800 structures turns that from a per-question cost into a
    per-pass one that cannot sit under the budget cap. Off, the service grounds
    against the facts already in the profile store and reports that it did.
  EOT
  type        = bool
  default     = false
}

variable "memory_bank_enabled" {
  description = <<-EOT
    Durable agent working memory: `open_questions` and their checkpoints.

    On by default. It is Firestore documents in collections that exist either
    way, so the cost is storage, and the thing it buys is the reason the
    collections are there -- a question opened by a slow-loop agent in March
    survives every restart and scale-to-zero between then and the incident that
    finally closes it. Off makes the loops forget between passes.
  EOT
  type        = bool
  default     = true
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

variable "console_role_bindings" {
  description = <<-EOT
    Who may use the console, and as what: "email:role,email:role".

    Roles are viewer, captain, chief. A viewer reads. A captain files referrals
    and dispatches companies. A chief additionally commits other agencies --
    utility shutoff, road closure, notification.

    Empty is NOT a safe default, and it is not a lockout either, which is what
    makes it dangerous. Every authenticated principal falls back to viewer, so
    the console comes up, everyone can sign in, everyone can read, and NOBODY
    can approve a referral or a utility shutoff -- there is no one holding
    captain or chief, so the approval paths this system exists to run are dead.
    The backend logs a warning and starts anyway, so nothing fails loudly.

    A live deployment sets this. Staging may leave it empty deliberately, as
    long as somebody knows the approval paths are untestable in that state.

    The console mints an OIDC token from the metadata server and the backend
    maps the verified principal through this list, so the role a person holds is
    decided here and never by anything the browser sends. Parsing is strict: a
    malformed entry or an unknown role name stops the process at startup rather
    than leaving an officer silently a viewer.
  EOT
  type        = string
  default     = ""
}
