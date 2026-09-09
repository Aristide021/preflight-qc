#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# PreFlight QC — IAM Service Account Setup
# Creates the dedicated service account with least-privilege access.
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_ID="${GOOGLE_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
SA_NAME="preflight-qc-sa"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "Error: GOOGLE_PROJECT_ID is not set." >&2
  exit 1
fi

echo "==> Setting up Service Account: ${SA_EMAIL}"

# Create Service Account if it does not already exist
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "Creating service account ${SA_NAME}..."
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="PreFlight QC Cloud Run Service Account" \
    --project="${PROJECT_ID}"
else
  echo "Service account ${SA_NAME} already exists."
fi

# Grant Secret Manager Secret Accessor role
echo "Binding roles/secretmanager.secretAccessor..."
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --condition=None

echo "==> IAM configuration complete for ${SA_EMAIL}"
