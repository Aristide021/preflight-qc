#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Gate 1: Run Netflix Photon against an IMF test vector and harvest error codes.
#
# USAGE:
#   ./infra/photon/run_photon.sh [IMF_PACKAGE_PATH]
#
# If no path is given, downloads the imf-plugfest public test vector first.
# Output is written to data/photon_harvest/raw/ and then parsed into
# data/photon_harvest/error_taxonomy.json.
#
# Gate 1 is GREEN when error_taxonomy.json contains ≥10 distinct IMF_* codes.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HARVEST_DIR="$PROJECT_ROOT/data/photon_harvest"
RAW_DIR="$HARVEST_DIR/raw"
IMAGE_NAME="photon-validator"

mkdir -p "$RAW_DIR"

# ── 1. Build the Photon Docker image (if not already built) ──────────────────
echo "▶ Building Photon Docker image (this takes ~3 minutes first time)..."
docker build \
    -t "$IMAGE_NAME" \
    -f "$SCRIPT_DIR/Dockerfile.photon" \
    "$PROJECT_ROOT" \
    --quiet

echo "✓ Image built: $IMAGE_NAME"

# ── 2. Resolve the IMF package path ─────────────────────────────────────────
IMF_PATH="${1:-}"

if [[ -z "$IMF_PATH" ]]; then
    echo ""
    echo "No IMF package path provided. Attempting to use imf-plugfest test vectors..."
    echo ""
    echo "  Option A — download from imf-plugfest S3 (if still reachable):"
    echo "    aws s3 cp s3://imf-plugfest/imf-packages/Netflix_OV/ ./test_vectors/Netflix_OV/ --recursive --no-sign-request"
    echo ""
    echo "  Option B — use Photon's bundled test resources:"
    echo "    docker run --rm $IMAGE_NAME will show available built-in test options"
    echo ""
    echo "  Option C — point at any local IMF package:"
    echo "    $0 /path/to/your/IMF_Package"
    echo ""

    # Try to run Photon's built-in self-test (uses resources bundled in the JAR)
    echo "▶ Running Photon built-in self-test to harvest error vocabulary..."
    docker run --rm "$IMAGE_NAME" 2>&1 | tee "$RAW_DIR/builtin_run.txt" || true

    # If that produces nothing useful, generate a deliberately malformed package
    echo ""
    echo "▶ Generating deliberately malformed IMF package for Photon to reject..."
    bash "$SCRIPT_DIR/make_malformed_imf.sh" "$RAW_DIR/malformed_imf"
    IMF_PATH="$RAW_DIR/malformed_imf"
fi

# ── 3. Run Photon against the IMF package ────────────────────────────────────
echo ""
echo "▶ Running Photon against: $IMF_PATH"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_FILE="$RAW_DIR/photon_run_$TIMESTAMP.txt"

docker run --rm \
    -v "$(realpath "$IMF_PATH"):/imf_package:ro" \
    "$IMAGE_NAME" \
    /imf_package \
    2>&1 | tee "$OUTPUT_FILE"

echo ""
echo "✓ Raw Photon output saved: $OUTPUT_FILE"

# ── 4. Parse output → error_taxonomy.json ────────────────────────────────────
echo ""
echo "▶ Parsing error codes into error_taxonomy.json..."

python3 "$SCRIPT_DIR/parse_photon_output.py" \
    --input "$OUTPUT_FILE" \
    --output "$HARVEST_DIR/error_taxonomy.json"

# ── 5. Gate 1 check ──────────────────────────────────────────────────────────
echo ""
DISTINCT_CODES=$(python3 -c "
import json, sys
try:
    data = json.load(open('$HARVEST_DIR/error_taxonomy.json'))
    codes = [e['error_code'] for e in data.get('errors', [])]
    print(len(set(codes)))
except Exception as e:
    print(0)
")

echo "Gate 1 check: $DISTINCT_CODES distinct error codes harvested"

if (( DISTINCT_CODES >= 10 )); then
    echo ""
    echo "════════════════════════════════════════════════════════"
    echo "  ✅  GATE 1 GREEN — $DISTINCT_CODES distinct IMF error codes"
    echo "      error_taxonomy.json is ready for Phase 1."
    echo "════════════════════════════════════════════════════════"
    exit 0
else
    echo ""
    echo "════════════════════════════════════════════════════════"
    echo "  ❌  GATE 1 NOT YET GREEN — only $DISTINCT_CODES codes found."
    echo "      Try more test vectors. See README for fallback options."
    echo "════════════════════════════════════════════════════════"
    exit 1
fi
