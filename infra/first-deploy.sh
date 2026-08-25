#!/usr/bin/env bash
# The first staging deployment, end to end.
#
# `deploy-staging.sh` is the steady-state command and assumes the ground it
# stands on already exists. This runs the things that are only true the first
# time, then hands over to it:
#
#   1. Create the API enablements, the nine agent identities, and the secret
#      *containers* -- before anything that mounts one exists.
#   2. Add a CALLBACK_SECRET version. Cloud Run resolves a secret reference when
#      a container starts and refuses to start when the secret has no version,
#      so every service would fail to become ready without this. Terraform
#      creates the container and never the value, which is why this step is here
#      and not in a .tf file.
#   3. Give the build identity the three roles a build needs. A project created
#      after mid-2024 leaves the default compute service account with none.
#   4. Build both images and apply the rest.
#
# Safe to re-run: 1 and 3 are convergent, 2 is skipped when a version already
# exists, and 4 is the ordinary deploy path.
#
# Optional third-party keys (Google Solar, NREL, Resend, Socrata) are NOT added
# here. Without them those sources report UNCONFIGURED, which is a designed and
# visible state -- they never fall back to fixtures. To add one later:
#
#   printf %s "$KEY" | gcloud secrets versions add firstdue-staging-nrel-api-key \
#     --project PROJECT --data-file=-
#
# then add its name to `live_source_keys` in terraform.tfvars and redeploy. Both
# halves are required: listing a key whose secret has no version takes the whole
# service down instead of degrading one source.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
ENV_DIR="${HERE}/terraform/envs/staging"

PROJECT_ID="${PROJECT_ID:-firstdue-dev}"
TOFU="${TOFU:-tofu}"

fail() { echo "error: $*" >&2; exit 1; }

command -v gcloud >/dev/null 2>&1 || fail "gcloud is not installed"
command -v "${TOFU}" >/dev/null 2>&1 || fail "${TOFU} is not installed (brew install opentofu)"
gcloud auth print-access-token >/dev/null 2>&1 || fail "gcloud is not authenticated; run: gcloud auth login"
[[ -f "${ENV_DIR}/terraform.tfvars" ]] || fail "missing ${ENV_DIR}/terraform.tfvars"
[[ -f "${ENV_DIR}/backend.hcl" ]] || fail "missing ${ENV_DIR}/backend.hcl (run infra/bootstrap.sh)"

echo "==> 1/4  identities, API enablements, and secret containers"
"${TOFU}" -chdir="${ENV_DIR}" init -backend-config=backend.hcl -input=false >/dev/null
# Targeting is the documented exception here for the same reason it is in
# deploy-staging.sh: these have to exist before the things that consume them,
# and a single apply would try to start a service against a secret that has no
# version yet.
"${TOFU}" -chdir="${ENV_DIR}" apply -input=false -auto-approve \
  -target=module.services -target=module.iam -target=module.secrets

echo "==> 2/4  CALLBACK_SECRET"
SECRET_NAME="firstdue-staging-callback-secret"
if gcloud secrets versions list "${SECRET_NAME}" --project "${PROJECT_ID}" \
     --filter="state=ENABLED" --format="value(name)" 2>/dev/null | grep -q .; then
  echo "    a version already exists; leaving it alone"
else
  # Generated here and never written to disk or echoed. It signs internal
  # write callbacks; rotating it means adding a new version and redeploying.
  python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" \
    | gcloud secrets versions add "${SECRET_NAME}" --project "${PROJECT_ID}" --data-file=-
  echo "    added"
fi

echo "==> 3/4  the build identity"
# `gcloud builds submit` runs as the project's default compute service account,
# which on a project created after mid-2024 holds *no roles at all* -- so the
# first build uploads its source tarball successfully and then fails to read it
# back, with a 403 on a bucket it just wrote to. Found exactly that way.
#
# Granted surgically rather than with `roles/cloudbuild.builds.builder`, which
# is Google's blanket remedy and carries considerably more than a build needs.
# This is a *build* identity and deliberately not one of the nine runtime ones:
# those are derived from the agent descriptors in `modules/iam` and a
# conformance test fails if they drift, so a build role does not belong there.
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
BUILDER="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
BUILD_BUCKET="gs://${PROJECT_ID}_cloudbuild"

# Cloud Build creates this on first submit; created here when absent so the
# binding below has something to bind to on a genuinely first run.
if ! gcloud storage buckets describe "${BUILD_BUCKET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "    creating ${BUILD_BUCKET}"
  gcloud storage buckets create "${BUILD_BUCKET}" \
    --project "${PROJECT_ID}" --location "${REGION:-us-central1}" \
    --uniform-bucket-level-access --public-access-prevention
fi

# Source tarball in, build logs out -- scoped to the build bucket, not project
# wide, so this says nothing about the state bucket or the pre-incident plans.
gcloud storage buckets add-iam-policy-binding "${BUILD_BUCKET}" \
  --project "${PROJECT_ID}" --member="${BUILDER}" \
  --role="roles/storage.objectAdmin" >/dev/null
# Push the two images.
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="${BUILDER}" --role="roles/artifactregistry.writer" \
  --condition=None >/dev/null
# Write build logs. Without it a build fails with no readable explanation.
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="${BUILDER}" --role="roles/logging.logWriter" \
  --condition=None >/dev/null
echo "    granted"

echo "==> 4/4  images and the rest of the infrastructure"
PROJECT_ID="${PROJECT_ID}" AUTO_APPROVE="${AUTO_APPROVE:-false}" "${HERE}/deploy-staging.sh"
