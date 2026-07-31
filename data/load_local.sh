#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Load the generated Parquet corpus into a LOCAL ClickHouse instance.
#
# Purpose: iterate on schema and query latency WITHOUT burning cloud credits.
# The local instance is throw-away — it's the sandbox before the paid cluster.
#
# Start the local server (once):
#   docker run -d --name preflight-ch -p 8123:8123 -p 9000:9000 \
#       -e CLICKHOUSE_PASSWORD=preflight-local -e CLICKHOUSE_DB=preflight \
#       clickhouse/clickhouse-server:latest
#
# Then:
#   ./data/load_local.sh                        # loads from data/generator/output/
#   ./data/load_local.sh /path/to/parquet/dir   # loads from a custom path
#
# ── WHY THIS SCRIPT IGNORES $CLICKHOUSE_HOST ────────────────────────────────
# An earlier version resolved its target with ${CLICKHOUSE_HOST:-localhost}.
# Because .env sets CLICKHOUSE_HOST to the ClickHouse *Cloud* endpoint, anyone
# who had sourced .env would have had this script push 50M rows into the paid
# cluster. The local connection now uses its own PREFLIGHT_LOCAL_* variables
# and is checked to be loopback before anything is written.
#
# Talks to ClickHouse over HTTP with curl, so no clickhouse-client binary is
# needed on the host and the server needs no filesystem access to the Parquet
# files — it reads them from the request body.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PARQUET_DIR="${1:-$SCRIPT_DIR/generator/output}"
SCHEMA_FILE="$SCRIPT_DIR/schema.sql"

CH_HOST="${PREFLIGHT_LOCAL_CH_HOST:-localhost}"
CH_PORT="${PREFLIGHT_LOCAL_CH_PORT:-8123}"
CH_USER="${PREFLIGHT_LOCAL_CH_USER:-default}"
CH_PASSWORD="${PREFLIGHT_LOCAL_CH_PASSWORD:-preflight-local}"

# ── Guard: refuse to run against anything that is not loopback ──────────────
case "$CH_HOST" in
    localhost|127.0.0.1|::1) ;;
    *)
        echo "REFUSING TO RUN: PREFLIGHT_LOCAL_CH_HOST is '$CH_HOST', which is not loopback."
        echo "This script bulk-loads tens of millions of rows and is for the local"
        echo "throw-away instance only. Load ClickHouse Cloud deliberately, separately."
        exit 1
        ;;
esac

CH_URL="http://${CH_HOST}:${CH_PORT}/"
ch() { curl -sS -u "${CH_USER}:${CH_PASSWORD}" "$CH_URL" --data-binary "$1"; }

echo "════════════════════════════════════════════════════════"
echo "  Loading QC corpus into LOCAL ClickHouse"
echo "  Parquet dir : $PARQUET_DIR"
echo "  CH endpoint : $CH_URL (loopback verified)"
echo "════════════════════════════════════════════════════════"
echo ""

# ── 1. Connectivity ─────────────────────────────────────────────────────────
echo "▶ Checking ClickHouse connectivity..."
if ! ch "SELECT 1" >/dev/null 2>&1; then
    echo "Cannot reach ClickHouse at $CH_URL"
    echo "Start it with:"
    echo "  docker run -d --name preflight-ch -p 8123:8123 -p 9000:9000 \\"
    echo "      -e CLICKHOUSE_PASSWORD=preflight-local -e CLICKHOUSE_DB=preflight \\"
    echo "      clickhouse/clickhouse-server:latest"
    exit 1
fi
echo "✓ ClickHouse up — version $(ch 'SELECT version()')"

# ── 2. Schema ───────────────────────────────────────────────────────────────
# The HTTP interface is stateless, so a bare `USE preflight` is meaningless and
# is skipped; every object in schema.sql is already database-qualified or
# created inside the preflight database by the CREATE DATABASE statement.
echo ""
echo "▶ Applying schema..."
while IFS= read -r stmt; do
    [[ -z "$stmt" ]] && continue
    ch "$stmt" >/dev/null
done < <(python3 - "$SCHEMA_FILE" <<'PYEOF'
import re, sys
sql = re.sub(r'^\s*--.*$', '', open(sys.argv[1]).read(), flags=re.M)
for s in (s.strip() for s in sql.split(';')):
    if s and not s.upper().startswith('USE '):
        print(' '.join(s.split()))
PYEOF
)
echo "✓ Schema applied"

# ── 3. Load Parquet ─────────────────────────────────────────────────────────
echo ""
shopt -s nullglob
PARQUET_FILES=("$PARQUET_DIR"/*.parquet)
TOTAL=${#PARQUET_FILES[@]}

if (( TOTAL == 0 )); then
    echo "No Parquet files found in $PARQUET_DIR"
    echo "   Run: uv run python data/generator/generator.py --rows 50000000"
    exit 1
fi

EXISTING=$(ch "SELECT count() FROM preflight.qc_inspections")
if [[ "$EXISTING" != "0" ]]; then
    echo "   Table already holds $EXISTING rows — truncating for a clean load."
    ch "TRUNCATE TABLE preflight.qc_inspections" >/dev/null
    ch "TRUNCATE TABLE preflight.vendor_failure_rates_mv_state" >/dev/null
fi

echo "▶ Loading $TOTAL Parquet files..."
# Column names match the table 1:1, so FORMAT Parquet maps them by name.
LOADED=0
START=$SECONDS
for f in "${PARQUET_FILES[@]}"; do
    curl -sS -u "${CH_USER}:${CH_PASSWORD}" \
        "${CH_URL}?query=INSERT%20INTO%20preflight.qc_inspections%20FORMAT%20Parquet" \
        --data-binary "@$f"
    LOADED=$((LOADED + 1))
    printf "\r   Loaded %d / %d files (%ds elapsed)" "$LOADED" "$TOTAL" "$((SECONDS - START))"
done
echo ""
echo "✓ Load complete in $((SECONDS - START))s"

# ── 4. Verify ───────────────────────────────────────────────────────────────
echo ""
echo "▶ Corpus summary:"
ch "
SELECT
    formatReadableQuantity(count())                    AS rows,
    uniq(title_id)                                     AS titles,
    uniq(vendor_id)                                    AS vendors,
    uniqExact(error_code)                              AS distinct_error_codes,
    round(countIf(result = 'pass') / count() * 100, 2) AS pass_pct,
    round(countIf(result = 'warn') / count() * 100, 2) AS warn_pct,
    round(countIf(result = 'fail') / count() * 100, 2) AS fail_pct,
    max(redelivery_attempt)                            AS max_redelivery_attempt
FROM preflight.qc_inspections
FORMAT Vertical"

echo ""
echo "▶ On-disk footprint (ClickHouse compression):"
ch "
SELECT
    formatReadableSize(sum(data_compressed_bytes))   AS compressed,
    formatReadableSize(sum(data_uncompressed_bytes)) AS uncompressed,
    round(sum(data_uncompressed_bytes) / sum(data_compressed_bytes), 1) AS ratio
FROM system.columns
WHERE database = 'preflight' AND table = 'qc_inspections'
FORMAT Vertical"

echo ""
echo "════════════════════════════════════════════════════════"
echo "  Corpus loaded. Next: uv run python data/benchmark_local.py"
echo "════════════════════════════════════════════════════════"
