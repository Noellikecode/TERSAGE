# Remote state in a versioned bucket, created by `infra/bootstrap.sh` before the
# first apply. State is not in this repository and never should be: it contains
# every resource id and, for some providers, values that were meant to stay in
# Secret Manager.
#
# Fill the bucket name in `backend.hcl` and run:
#   tofu init -backend-config=backend.hcl

terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  backend "gcs" {
    prefix = "firstdue/staging"
  }
}
