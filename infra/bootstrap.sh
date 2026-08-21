#!/usr/bin/env bash
# One-time bootstrap: the state bucket, before the first apply.
#
# Terraform cannot create the bucket that holds its own state, so this is the
# one piece of infrastructure created imperatively. It runs once per project and
# is safe to re-run: every step is a create-if-absent.
#
# Versioning on the state bucket is not optional. A corrupted or truncated state
# file with no previous version means the only record of what exists in the
# project is the project itself, and reconciling that by hand is a day's work.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
BUCKET="${BUCKET:-${PROJECT_ID}-firstdue-tfstate}"

fail() { echo "error: $*" >&2; exit 1; }

command -v gcloud >/dev/null 2>&1 || fail "gcloud is not installed"
[[ -n "${PROJECT_ID}" ]] || fail "PROJECT_ID is required (export PROJECT_ID=your-project-id)"
gcloud auth print-access-token >/dev/null 2>&1 || fail "gcloud is not authenticated; run: gcloud auth login"

echo "==> enabling the APIs bootstrap itself needs"
gcloud services enable storage.googleapis.com cloudresourcemanager.googleapis.com \
  --project "${PROJECT_ID}"

if gcloud storage buckets describe "gs://${BUCKET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "==> state bucket gs://${BUCKET} already exists"
else
  echo "==> creating state bucket gs://${BUCKET}"
  gcloud storage buckets create "gs://${BUCKET}" \
    --project "${PROJECT_ID}" \
    --location "${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi

echo "==> enabling object versioning"
gcloud storage buckets update "gs://${BUCKET}" --versioning

echo "==> writing backend.hcl for staging"
cat > "$(dirname "$0")/terraform/envs/staging/backend.hcl" <<HCL
bucket = "${BUCKET}"
HCL

cat <<NEXT

state bucket ready: gs://${BUCKET}

next:
  cd infra/terraform/envs/staging
  cp terraform.tfvars.example terraform.tfvars   # then fill it in
  tofu init -backend-config=backend.hcl
  tofu plan -out tfplan                          # read this before applying
  tofu apply tfplan
NEXT
