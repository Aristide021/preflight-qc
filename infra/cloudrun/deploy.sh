#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# PreFlight QC — Google Cloud Run Deployment Script
# Builds the container image and deploys to Cloud Run with Secret Manager envs.
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_ID="${GOOGLE_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${GOOGLE_REGION:-us-central1}"
SERVICE_NAME="preflight-qc"
IMAGE_TAG="gcr.io/${PROJECT_ID}/${SERVICE_NAME}:latest"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "Error: GOOGLE_PROJECT_ID is not set and no default gcloud project found." >&2
  echo "Set GOOGLE_PROJECT_ID=<your-project-id> and rerun." >&2
  exit 1
fi

echo "==> Deploying PreFlight QC to Cloud Run"
echo "    Project: ${PROJECT_ID}"
echo "    Region:  ${REGION}"
echo "    Image:   ${IMAGE_TAG}"

# Step 1: Submit build via Google Cloud Build
echo "==> 1. Building container image with Cloud Build..."
gcloud builds submit --tag "${IMAGE_TAG}" --project "${PROJECT_ID}" .

# Step 2: Deploy to Cloud Run
echo "==> 2. Deploying service to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE_TAG}" \
  --platform managed \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --allow-unauthenticated \
  --service-account "preflight-qc-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars "APP_ENV=production,GEMINI_MODEL=gemini-2.5-flash,GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},CLICKHOUSE_SECURE=true,CLICKHOUSE_DATABASE=preflight" \
  --set-secrets "CLICKHOUSE_HOST=clickhouse-host:latest,CLICKHOUSE_USER=clickhouse-user:latest,CLICKHOUSE_PASSWORD=clickhouse-password:latest" \
  --cpu 2 \
  --memory 2Gi \
  --timeout 300

URL=$(gcloud run services describe "${SERVICE_NAME}" --platform managed --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)')
echo "==> Deployment complete!"
echo "==> Public URL: ${URL}"
