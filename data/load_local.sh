#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Load the generated Parquet corpus into a LOCAL ClickHouse instance.
#
# Purpose: iterate on schema and query latency WITHOUT burning cloud credits.
# The local instance is throw-away — it's the sandbox before the paid cluster.
#
# Prerequisites:
#   - ClickHouse installed locally: brew install clickhouse
#     OR running in Docker: docker run -d -p 8123:8123 -p 9000:9000 clickhouse/clickhouse-server
#   - Generator has been run: uv run python data/generator/generator.py
#
# Usage:
#   chmod +x data/load_local.sh
#   ./data/load_local.sh                        # loads from data/generator/output/
#   ./data/load_local.sh /path/to/parquet/dir   # loads from a custom path
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PARQUET_DIR="${1:-$(dirname "$0")/generator/output}"
CH_HOST="${CLICKHOUSE_HOST:-localhost}"
CH_PORT="${CLICKHOUSE_PORT:-9000}"
CH_USER="${CLICKHOUSE_USER:-default}"
CH_PASSWORD="${CLICKHOUSE_PASSWORD:-}"

# ClickHouse client command (works for both brew-installed and Docker)
CH="clickhouse-client --host=$CH_HOST --port=$CH_PORT --user=$CH_USER"
if [[ -n "$CH_PASSWORD" ]]; then
    CH="$CH --password=$CH_PASSWORD"
fi

echo "════════════════════════════════════════════════════════"
echo "  Loading QC corpus into local ClickHouse"
echo "  Parquet dir : $PARQUET_DIR"
echo "  CH host     : $CH_HOST:$CH_PORT"
echo "════════════════════════════════════════════════════════"
echo ""

# ── 1. Check ClickHouse is reachable ─────────────────────────────────────────
echo "▶ Checking ClickHouse connectivity..."
if ! $CH --query "SELECT 1" > /dev/null 2>&1; then
    echo "❌ Cannot reach ClickHouse at $CH_HOST:$CH_PORT"
    echo "   Start it with: docker run -d -p 8123:8123 -p 9000:9000 clickhouse/clickhouse-server"
    echo "   Or: brew install clickhouse && clickhouse server"
    exit 1
fi
echo "✓ ClickHouse is up"

# ── 2. Apply schema ───────────────────────────────────────────────────────────
echo ""
echo "▶ Creating database and tables..."
$CH --multiquery < "$(dirname "$0")/schema.sql"
echo "✓ Schema applied"

# ── 3. Load Parquet files ─────────────────────────────────────────────────────
echo ""
echo "▶ Loading Parquet files from $PARQUET_DIR ..."
PARQUET_FILES=("$PARQUET_DIR"/*.parquet)
TOTAL=${#PARQUET_FILES[@]}

if [[ "$TOTAL" -eq 0 ]]; then
    echo "❌ No Parquet files found in $PARQUET_DIR"
    echo "   Run: uv run python data/generator/generator.py"
    exit 1
fi

echo "   Found $TOTAL Parquet files"
LOADED=0
for f in "${PARQUET_FILES[@]}"; do
    $CH --query "
        INSERT INTO preflight.qc_inspections
        SELECT
            inspection_id,
            title_id, title_name, version_label,
            package_type, package_id,
            vendor_id, vendor_name,
            platform_spec, spec_version, app_profile,
            submitted_at, inspected_at,
            stage,
            result,
            error_code, error_category, error_severity,
            error_message, error_context,
            redelivery_attempt, remediation_cost_usd,
            resolved, resolved_at,
            codec, frame_rate, resolution, hdr_format, audio_config
        FROM file('$f', Parquet)
    "
    LOADED=$((LOADED + 1))
    printf "\r   Loaded %d / %d files" "$LOADED" "$TOTAL"
done
echo ""
echo "✓ All Parquet files loaded"

# ── 4. Verify row count ───────────────────────────────────────────────────────
echo ""
echo "▶ Verifying corpus..."
ROW_COUNT=$($CH --query "SELECT formatReadableQuantity(count()) FROM preflight.qc_inspections" --format=TSV)
VENDOR_COUNT=$($CH --query "SELECT count(DISTINCT vendor_id) FROM preflight.qc_inspections" --format=TSV)
TITLE_COUNT=$($CH --query "SELECT count(DISTINCT title_id) FROM preflight.qc_inspections" --format=TSV)
FAIL_RATE=$($CH --query "SELECT round(countIf(result='fail') / count() * 100, 1) FROM preflight.qc_inspections" --format=TSV)

echo ""
echo "════════════════════════════════════════════════════════"
echo "  ✅  Corpus loaded successfully"
echo "  Rows     : $ROW_COUNT"
echo "  Vendors  : $VENDOR_COUNT"
echo "  Titles   : $TITLE_COUNT"
echo "  Fail rate: ${FAIL_RATE}%"
echo "════════════════════════════════════════════════════════"

# ── 5. Run Checkpoint 1 latency test ─────────────────────────────────────────
echo ""
echo "▶ Running Checkpoint 1 latency test (Q1 — vendor failure rates)..."
TIME_MS=$($CH --time --query "
    SELECT vendor_id, error_code, round(countIf(result='fail') / count(), 3) AS fail_rate
    FROM preflight.qc_inspections
    WHERE inspected_at >= now() - INTERVAL 90 DAY AND error_code != ''
    GROUP BY vendor_id, error_code
    ORDER BY fail_rate DESC
    LIMIT 20
" --format=Null 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)

echo "   Q1 latency: ${TIME_MS}s"

if (( $(echo "$TIME_MS < 2.0" | bc -l) )); then
    echo ""
    echo "════════════════════════════════════════════════════════"
    echo "  ✅  CHECKPOINT 1 GREEN"
    echo "      Corpus is large AND queries fast (<2s)."
    echo "      Ready to load into ClickHouse Cloud."
    echo "════════════════════════════════════════════════════════"
else
    echo ""
    echo "⚠️   Q1 took ${TIME_MS}s — check index / partition settings in schema.sql"
fi
