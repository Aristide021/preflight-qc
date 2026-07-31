#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Gate 1 — harvest Photon's error taxonomy.
#
# Two sources, kept separate on purpose:
#
#   1. DECLARED  — every constant of IMFErrorLogger.IMFErrors.ErrorCodes and
#                  .ErrorLevels, dumped from the loaded classes. This is the
#                  complete, authoritative vocabulary.
#   2. OBSERVED  — real (code, level, message) triples produced by running
#                  IMPAnalyzer.analyzeDelivery over the IMF packages Photon
#                  ships in src/test/resources/TestIMP.
#
# Why not scrape the CLI: IMPAnalyzer prints prose ("ERROR-UUID ... is not
# same as ...") and never prints the enum constants. Text scraping recovers
# zero real codes. PhotonHarvester calls the same API the CLI does and reads
# the structured ErrorObject list instead.
#
# USAGE:
#   ./infra/photon/harvest_all.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HARVEST_DIR="$PROJECT_ROOT/data/photon_harvest"
RAW_DIR="$HARVEST_DIR/raw"
IMAGE_NAME="photon-validator"

mkdir -p "$RAW_DIR"

# ── 1. Build the image if it is not present ─────────────────────────────────
if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "▶ Building $IMAGE_NAME (first run takes ~5 minutes)..."
    docker build -t "$IMAGE_NAME" -f "$SCRIPT_DIR/Dockerfile.photon" "$PROJECT_ROOT"
fi
echo "✓ Image present: $IMAGE_NAME"

# ── 2. Dump the declared enum constants ─────────────────────────────────────
echo "▶ Dumping declared ErrorCodes / ErrorLevels..."
docker run --rm --entrypoint PhotonHarvester "$IMAGE_NAME" --dump-codes \
    2>/dev/null > "$RAW_DIR/declared_codes.jsonl"
echo "✓ Declared constants: $(wc -l < "$RAW_DIR/declared_codes.jsonl" | tr -d ' ')"

# ── 3. Sweep every bundled IMF package ──────────────────────────────────────
# A directory counts as a package when it contains an ASSETMAP.xml.
echo "▶ Running IMPAnalyzer.analyzeDelivery over bundled IMF test packages..."
docker run --rm --entrypoint sh "$IMAGE_NAME" -c '
    find /photon/test_vectors -mindepth 1 -maxdepth 2 -type d \
        -exec test -e "{}/ASSETMAP.xml" \; -print \
    | sort | xargs PhotonHarvester 2>/dev/null
' > "$RAW_DIR/observed_errors.jsonl"

ROWS=$(wc -l < "$RAW_DIR/observed_errors.jsonl" | tr -d ' ')
echo "✓ Harvested $ROWS rows → $RAW_DIR/observed_errors.jsonl"

# ── 4. Build the taxonomy ───────────────────────────────────────────────────
echo "▶ Building error_taxonomy.json..."
PY="$PROJECT_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3

"$PY" "$SCRIPT_DIR/build_taxonomy.py" \
    --declared "$RAW_DIR/declared_codes.jsonl" \
    --observed "$RAW_DIR/observed_errors.jsonl" \
    --output   "$HARVEST_DIR/error_taxonomy.json"

# ── 5. Gate 1 check ─────────────────────────────────────────────────────────
# The gate is on AUTHENTICITY, not on hitting a code count. The earlier version
# counted every entry in the taxonomy including a hand-written seed list, so it
# reported GREEN while harvesting zero real codes. Both conditions below are
# measured against live Photon output only.
echo ""
"$PY" - "$HARVEST_DIR/error_taxonomy.json" <<'GATE'
import json, sys

taxonomy = json.load(open(sys.argv[1]))
s = taxonomy["harvest_summary"]

declared  = s["declared_codes"]
observed  = s["observed_codes"]
instances = s["observed_error_instances"]

checks = [
    ("complete declared vocabulary dumped from Photon", declared  >= 10, f"{declared} codes"),
    ("codes exercised by live analyzeDelivery runs",    observed  >= 5,  f"{observed} codes"),
    ("real error instances harvested",                  instances >= 100, f"{instances} instances"),
]

width = max(len(label) for label, _, _ in checks)
for label, ok, detail in checks:
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<{width}}  {detail}")

print()
if all(ok for _, ok, _ in checks):
    print("=" * 60)
    print("  GATE 1 GREEN — error_taxonomy.json is Photon-authentic.")
    print("=" * 60)
    sys.exit(0)

print("=" * 60)
print("  GATE 1 NOT GREEN — see failing checks above.")
print("=" * 60)
sys.exit(1)
GATE
