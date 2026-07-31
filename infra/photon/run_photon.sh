#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Run Photon against a single IMF package.
#
# This is the ad-hoc inspection utility. Gate 1 — harvesting the full error
# taxonomy — lives in ./harvest_all.sh, which sweeps every bundled test package
# and writes data/photon_harvest/error_taxonomy.json.
#
# USAGE:
#   ./infra/photon/run_photon.sh /path/to/IMF_Package     # your own package
#   ./infra/photon/run_photon.sh --list                   # bundled vectors
#   ./infra/photon/run_photon.sh --bundled <NAME>         # a bundled vector
#   ./infra/photon/run_photon.sh --json /path/to/Package  # structured JSONL
#
# Text mode runs IMPAnalyzer (human-readable prose). JSON mode runs
# PhotonHarvester, which reads structured ErrorObjects via the API and emits
# the real (code, level, description) triples — the CLI text never contains
# the enum constants.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
IMAGE_NAME="photon-validator"

if ! docker image inspect "$IMAGE_NAME" >/dev/null 2>&1; then
    echo "▶ Building $IMAGE_NAME (first run takes ~5 minutes)..."
    docker build -t "$IMAGE_NAME" -f "$SCRIPT_DIR/Dockerfile.photon" "$PROJECT_ROOT"
fi

MODE="text"
case "${1:-}" in
    --list)
        docker run --rm --entrypoint sh "$IMAGE_NAME" -c \
            'find /photon/test_vectors -mindepth 1 -maxdepth 2 -type d \
                 -exec test -e "{}/ASSETMAP.xml" \; -print \
             | sed "s|/photon/test_vectors/||" | sort'
        exit 0
        ;;
    --bundled)
        [[ $# -ge 2 ]] || { echo "usage: $0 --bundled <NAME>" >&2; exit 2; }
        docker run --rm --entrypoint IMPAnalyzer "$IMAGE_NAME" "/photon/test_vectors/$2"
        exit 0
        ;;
    --json)
        MODE="json"
        shift
        ;;
    "")
        echo "usage: $0 [--json] <IMF_PACKAGE_PATH>" >&2
        echo "       $0 --list" >&2
        echo "       $0 --bundled <NAME>" >&2
        exit 2
        ;;
esac

IMF_PATH="$1"
[[ -d "$IMF_PATH" ]] || { echo "ERROR: not a directory: $IMF_PATH" >&2; exit 1; }

ABS_PATH="$(cd "$IMF_PATH" && pwd)"

if [[ "$MODE" == "json" ]]; then
    docker run --rm -v "$ABS_PATH:/imf:ro" \
        --entrypoint PhotonHarvester "$IMAGE_NAME" /imf 2>/dev/null
else
    docker run --rm -v "$ABS_PATH:/imf:ro" "$IMAGE_NAME" /imf
fi
