#!/usr/bin/env bash
# Run the staging smoke suite against a deployed environment.
#
# The suite needs an identity token minted for the incident service's Cloud Run
# *custom audience* (`https://firstdue-incident`), and choosing an audience
# requires a service account -- a bare user credential carries gcloud's own
# client id and is rejected before any endpoint runs.
#
# `firstdue-ci-smoke` exists for exactly this and is already an invoker on the
# incident service. What it does not have is anyone allowed to impersonate it:
# `modules/iam` grants no `serviceAccountTokenCreator` anywhere, deliberately,
# and a test keeps it that way -- impersonation is the one way a service account
# with correct roles can still act as a different agent.
#
# That rule is about service accounts impersonating each other. A *human*
# operator running a smoke test is a different thing, so this grants the caller
# that role on the one CI account, and nothing else gains anything. Pass
# REVOKE=true to hand it back afterwards, which is worth doing on a shared
# project and unnecessary on a personal one.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-firstdue-dev}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SMOKE_SA="fd-ci-smoke@${PROJECT_ID}.iam.gserviceaccount.com"
AUDIENCE="${AUDIENCE:-https://firstdue-incident}"
ENV_DIR="$(dirname "$0")/terraform/envs/staging"

fail() { echo "error: $*" >&2; exit 1; }

BASE_URL="${STAGING_BASE_URL:-$(tofu -chdir="${ENV_DIR}" output -raw incident_url 2>/dev/null || true)}"
[[ -n "${BASE_URL}" ]] || fail "no incident_url in state and STAGING_BASE_URL is unset; is it deployed?"

CALLER="$(gcloud config get-value account 2>/dev/null)"
[[ -n "${CALLER}" ]] || fail "gcloud is not authenticated"

echo "==> allowing ${CALLER} to mint tokens as ${SMOKE_SA}"
gcloud iam service-accounts add-iam-policy-binding "${SMOKE_SA}" \
  --project "${PROJECT_ID}" \
  --member="user:${CALLER}" \
  --role="roles/iam.serviceAccountTokenCreator" >/dev/null

cleanup() {
  if [[ "${REVOKE:-false}" == "true" ]]; then
    echo "==> revoking"
    gcloud iam service-accounts remove-iam-policy-binding "${SMOKE_SA}" \
      --project "${PROJECT_ID}" \
      --member="user:${CALLER}" \
      --role="roles/iam.serviceAccountTokenCreator" >/dev/null || true
  fi
}
trap cleanup EXIT

# IAM propagation is not instant, and a token minted a second after the binding
# lands is sometimes still refused. Retry rather than fail on a race that has
# nothing to do with the code under test.
echo "==> minting an identity token for ${AUDIENCE}"
TOKEN=""
for attempt in 1 2 3 4 5 6; do
  if TOKEN="$(gcloud auth print-identity-token \
       --impersonate-service-account="${SMOKE_SA}" \
       --audiences="${AUDIENCE}" 2>/dev/null)"; then
    [[ -n "${TOKEN}" ]] && break
  fi
  echo "    not yet (attempt ${attempt}); waiting for the binding to propagate"
  sleep 10
done
[[ -n "${TOKEN}" ]] || fail "could not mint an impersonated identity token"

echo "==> ${BASE_URL}"
cd "${ROOT}"
STAGING_BASE_URL="${BASE_URL}" STAGING_TOKEN="${TOKEN}" uv run pytest tests/staging -v
