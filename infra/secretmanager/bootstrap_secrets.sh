#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# PreFlight QC — Google Secret Manager Bootstrap Script
# Provisions secrets for ClickHouse Cloud and Gemini API.
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_ID="${GOOGLE_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "Error: GOOGLE_PROJECT_ID is not set." >&2
  exit 1
fi

echo "==> Enabling Secret Manager API..."
gcloud services enable secretmanager.googleapis.com --project "${PROJECT_ID}"

create_or_update_secret() {
  local secret_name="$1"
  local secret_val="$2"

  if gcloud secrets describe "${secret_name}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "Updating secret ${secret_name}..."
    printf "%s" "${secret_val}" | gcloud secrets versions add "${secret_name}" --data-file=- --project "${PROJECT_ID}"
  else
    echo "Creating secret ${secret_name}..."
    printf "%s" "${secret_val}" | gcloud secrets create "${secret_name}" --data-file=- --replication-policy="automatic" --project "${PROJECT_ID}"
  fi
}

echo "Enter values to store in Secret Manager (or leave blank to skip):"

read -r -p "GOOGLE_API_KEY: " GOOGLE_API_KEY
if [[ -n "${GOOGLE_API_KEY}" ]]; then
  create_or_update_secret "google-api-key" "${GOOGLE_API_KEY}"
fi

read -r -p "CLICKHOUSE_HOST: " CLICKHOUSE_HOST
if [[ -n "${CLICKHOUSE_HOST}" ]]; then
  create_or_update_secret "clickhouse-host" "${CLICKHOUSE_HOST}"
fi

read -r -p "CLICKHOUSE_USER (default: default): " CLICKHOUSE_USER
CLICKHOUSE_USER="${CLICKHOUSE_USER:-default}"
create_or_update_secret "clickhouse-user" "${CLICKHOUSE_USER}"

read -r -s -p "CLICKHOUSE_PASSWORD: " CLICKHOUSE_PASSWORD
echo ""
if [[ -n "${CLICKHOUSE_PASSWORD}" ]]; then
  create_or_update_secret "clickhouse-password" "${CLICKHOUSE_PASSWORD}"
fi

echo "==> Secrets bootstrap finished for project ${PROJECT_ID}!"
