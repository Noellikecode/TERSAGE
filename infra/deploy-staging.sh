#!/usr/bin/env bash
# Staging deployment for FIRST DUE.
#
# One documented command: `make deploy-staging`.
#
# What changed in phase 7: this script no longer creates infrastructure with
# `gcloud run deploy`. Terraform owns every resource, so the script's job is now
# to build and push images and then hand their digests to `tofu apply`. Two
# tools creating the same Cloud Run service is how a configuration drifts from
# its state file, and the drift is only discovered during the next incident.
#
# It builds by digest, not by tag. A rollback has to name the exact image that
# was running, and `:latest` cannot.
#
# Preconditions (checked below, never assumed):
#   - `infra/bootstrap.sh` has been run once for this project
#   - terraform.tfvars exists and names the project and billing account
#   - gcloud is authenticated against the staging project
#
# It never writes a secret to disk and never echoes credential material. Secret
# *values* are added out of band with `gcloud secrets versions add`; Terraform
# creates only the containers.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
ENV_DIR="${HERE}/terraform/envs/staging"

PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-us-central1}"
ENVIRONMENT="staging"
REPO="firstdue-${ENVIRONMENT}"
TAG="${TAG:-$(git -C "${ROOT}" rev-parse --short HEAD 2>/dev/null || echo manual)}"
TOFU="${TOFU:-tofu}"

fail() { echo "error: $*" >&2; exit 1; }

command -v gcloud >/dev/null 2>&1 || fail "gcloud is not installed"
command -v "${TOFU}" >/dev/null 2>&1 || fail "${TOFU} is not installed (brew install opentofu)"
[[ -n "${PROJECT_ID}" ]] || fail "PROJECT_ID is required (export PROJECT_ID=your-project-id)"
gcloud auth print-access-token >/dev/null 2>&1 || fail "gcloud is not authenticated; run: gcloud auth login"
[[ -f "${ENV_DIR}/terraform.tfvars" ]] || fail "missing ${ENV_DIR}/terraform.tfvars (copy terraform.tfvars.example)"
[[ -f "${ENV_DIR}/backend.hcl" ]] || fail "missing ${ENV_DIR}/backend.hcl (run infra/bootstrap.sh)"

REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"

# The registry is a Terraform resource, so the very first deployment has to
# create it before there is anywhere to push. Targeting one module is the
# documented exception to "never target", and it is why it is written here
# rather than left to be improvised.
echo "==> ensuring the image repository exists"
"${TOFU}" -chdir="${ENV_DIR}" init -backend-config=backend.hcl -input=false >/dev/null
"${TOFU}" -chdir="${ENV_DIR}" apply -input=false -auto-approve -target=module.registry \
  -var="backend_image=placeholder" -var="console_image=placeholder"

echo "==> building images (${TAG})"
gcloud builds submit "${ROOT}" \
  --project "${PROJECT_ID}" \
  --config "${HERE}/cloudbuild.yaml" \
  --substitutions "_REGISTRY=${REGISTRY},_TAG=${TAG}"

digest_of() {
  gcloud artifacts docker images describe "${REGISTRY}/$1:${TAG}" \
    --project "${PROJECT_ID}" --format='value(image_summary.digest)'
}

BACKEND_IMAGE="${REGISTRY}/backend@$(digest_of backend)"
CONSOLE_IMAGE="${REGISTRY}/console@$(digest_of console)"
echo "==> backend  ${BACKEND_IMAGE}"
echo "==> console  ${CONSOLE_IMAGE}"

echo "==> planning"
"${TOFU}" -chdir="${ENV_DIR}" plan -input=false -out=tfplan \
  -var="backend_image=${BACKEND_IMAGE}" \
  -var="console_image=${CONSOLE_IMAGE}"

# A plan that is applied without being read is a plan that deletes a Firestore
# database at some point. AUTO_APPROVE exists for CI, where the plan is an
# artifact a human already reviewed.
if [[ "${AUTO_APPROVE:-false}" != "true" ]]; then
  echo
  read -r -p "apply this plan? [y/N] " reply
  [[ "${reply}" == "y" || "${reply}" == "Y" ]] || fail "aborted"
fi

echo "==> applying"
"${TOFU}" -chdir="${ENV_DIR}" apply -input=false tfplan
rm -f "${ENV_DIR}/tfplan"

INCIDENT_URL="$("${TOFU}" -chdir="${ENV_DIR}" output -raw incident_url)"
CONSOLE_URL="$("${TOFU}" -chdir="${ENV_DIR}" output -raw console_url)"

echo "==> health"
TOKEN="$(gcloud auth print-identity-token)"
curl -fsS -H "Authorization: Bearer ${TOKEN}" "${INCIDENT_URL}/healthz" >/dev/null \
  && echo "incident loop healthy at ${INCIDENT_URL}"

cat <<NEXT

deployed ${TAG}

  incident loop  ${INCIDENT_URL}
  console        ${CONSOLE_URL}

smoke test:
  STAGING_BASE_URL=${INCIDENT_URL} \\
  STAGING_TOKEN=\$(gcloud auth print-identity-token) \\
  make smoke-staging
NEXT
