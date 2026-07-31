#!/usr/bin/env python3
"""
Build data/photon_harvest/error_taxonomy.json from a PhotonHarvester run.

Replaces the earlier parse_photon_output.py, which could not work: it regex-
scanned IMPAnalyzer's stdout for `IMF_*_ERROR` tokens, but IMPAnalyzer prints
prose and never emits the enum constants. Every run harvested zero codes and
fell back to a hand-written seed list, most of whose codes do not exist in
Photon (IMF_ESSENCE_EXCEPTION, IMF_SUBTITLE_ERROR, IMF_DOLBY_VISION_ERROR,
IMF_HDR_ERROR, IMF_CODEC_ERROR, ...).

Inputs (both produced by infra/photon/harvest_all.sh):
    --declared  JSONL of every ErrorCodes / ErrorLevels constant
    --observed  JSONL of real (code, level, description) triples

Output: a taxonomy where every code is real, and each carries the evidence
behind it — how often it fired, at what levels, on which assets, with verbatim
example messages.

Usage:
    build_taxonomy.py --declared raw/declared_codes.jsonl \
                      --observed raw/observed_errors.jsonl \
                      --output   error_taxonomy.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

# ── Sentinel codes emitted by the harvester, not real Photon codes ───────────
NON_ERROR_CODES = {"NO_ERRORS", "HARVEST_EXCEPTION", "UNKNOWN"}

# ── Code → corpus category ───────────────────────────────────────────────────
# This mapping is OUR modeling decision, not Photon's — Photon has no category
# axis. It groups the real codes into the analytical buckets the ClickHouse
# corpus and the QC-Analyst agent reason over. Kept explicit so it is
# reviewable rather than buried in the generator.
CATEGORY_BY_CODE: dict[str, str] = {
    "IMF_CPL_ERROR": "structural",
    "IMF_PKL_ERROR": "structural",
    "IMF_AM_ERROR": "structural",
    "IMF_OPL_ERROR": "structural",
    "IMF_MASTER_PACKAGE_ERROR": "structural",
    "IMF_CORE_CONSTRAINTS_ERROR": "structural",
    "IMF_CORE_CONSTRAINTS_ESSENCE_DESCRIPTOR_LIST_MISSING": "structural",
    "APPLICATION_COMPOSITION_ERROR": "structural",
    "UUID_ERROR": "structural",
    "URI_ERROR": "structural",
    "IMF_ESSENCE_COMPONENT_ERROR": "essence",
    "IMF_ESSENCE_METADATA_ERROR": "essence",
    "IMP_VALIDATOR_PAYLOAD_ERROR": "integrity",
    "SMPTE_REGISTER_PARSING_ERROR": "metadata",
    "INTERNAL_ERROR": "internal",
}

# ── Photon error level → delivery severity ───────────────────────────────────
# Photon's own axis is WARNING / NON_FATAL / FATAL. IMPAnalyzer prints both
# FATAL and NON_FATAL as "ERROR-" and only WARNING as "WARNING-", so both map
# to blocking. Note there is deliberately no "cosmetic" here: cosmetic is a
# platform-spec judgment, not a validator judgment, and it is the Spec-Reader
# agent's job to make it against the delivery spec.
SEVERITY_BY_LEVEL: dict[str, str] = {
    "FATAL": "blocking",
    "NON_FATAL": "blocking",
    "WARNING": "advisory",
}

MAX_EXAMPLES_PER_CODE = 3
EXAMPLE_MAX_CHARS = 300


def read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"WARN: {path.name}:{line_no} is not valid JSON ({exc})", file=sys.stderr)
    return rows


def asset_kind(asset: str) -> str:
    """Classify the asset an error was raised against."""
    upper = asset.upper()
    if "ASSETMAP" in upper:
        return "assetmap"
    for token in ("CPL", "PKL", "OPL"):
        if token in upper:
            return token.lower()
    if upper.endswith(".MXF"):
        return "mxf"
    return "other"


def strip_version_suffix(message: str) -> str:
    """Photon appends '[Photon version: x.y.z]' to every message; drop it."""
    marker = " [Photon version:"
    idx = message.find(marker)
    return message[:idx].strip() if idx != -1 else message.strip()


def build(declared_rows: list[dict], observed_rows: list[dict]) -> dict:
    declared_codes = [r for r in declared_rows if r.get("kind") == "error_code"]
    declared_levels = [r for r in declared_rows if r.get("kind") == "error_level"]

    if not declared_codes:
        raise SystemExit("ERROR: no declared error codes found — is the harvester dump empty?")

    errors = [r for r in observed_rows if r.get("error_code") not in NON_ERROR_CODES]

    freq: Counter[str] = Counter(r["error_code"] for r in errors)
    levels_by_code: dict[str, Counter[str]] = defaultdict(Counter)
    assets_by_code: dict[str, Counter[str]] = defaultdict(Counter)
    examples_by_code: dict[str, list[str]] = defaultdict(list)

    for row in errors:
        code = row["error_code"]
        levels_by_code[code][row.get("error_level", "UNKNOWN")] += 1
        assets_by_code[code][asset_kind(row.get("asset", ""))] += 1

        message = strip_version_suffix(row.get("error_description", ""))
        if (
            message
            and len(examples_by_code[code]) < MAX_EXAMPLES_PER_CODE
            and message not in examples_by_code[code]
        ):
            examples_by_code[code].append(message[:EXAMPLE_MAX_CHARS])

    total_observed = sum(freq.values())
    packages = {r.get("package") for r in observed_rows if r.get("package")}
    packages_with_errors = {r.get("package") for r in errors if r.get("package")}

    entries = []
    for row in declared_codes:
        code = row["name"]
        count = freq.get(code, 0)
        level_counts = dict(levels_by_code.get(code, {}))

        # Severity of a code = severity of its most frequently observed level.
        # Codes never observed carry severity null rather than a guess.
        if level_counts:
            dominant_level = max(level_counts.items(), key=lambda kv: kv[1])[0]
            severity = SEVERITY_BY_LEVEL.get(dominant_level)
        else:
            dominant_level = None
            severity = None

        entries.append(
            {
                "error_code": code,
                "error_label": row.get("label", ""),
                "category": CATEGORY_BY_CODE.get(code, "unknown"),
                "observed": count > 0,
                "observed_count": count,
                "observed_share": round(count / total_observed, 6) if total_observed else 0.0,
                "levels_observed": level_counts,
                "dominant_level": dominant_level,
                "severity": severity,
                "asset_kinds": dict(assets_by_code.get(code, {})),
                "example_messages": examples_by_code.get(code, []),
                "source": "photon_declared_enum" + ("+observed" if count else ""),
            }
        )

    entries.sort(key=lambda e: (-e["observed_count"], e["error_code"]))

    return {
        "schema_version": "2.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "provenance": {
            "validator": "Netflix Photon",
            "source_repo": "https://github.com/Netflix/photon",
            "declared_from": "com.netflix.imflibrary.IMFErrorLogger$IMFErrors$ErrorCodes",
            "observed_from": "IMPAnalyzer.analyzeDelivery over IMF packages bundled in "
            "Photon src/test/resources/TestIMP",
            "note": "Every code in this file is a real Photon enum constant. Codes with "
            "observed=false are declared by Photon but not exercised by the bundled "
            "test packages; they are NOT invented. Category is our modeling "
            "decision; severity is derived from Photon's own error levels.",
        },
        "error_levels": [
            {"name": lv["name"], "label": lv.get("label", ""), "severity": SEVERITY_BY_LEVEL.get(lv["name"])}
            for lv in declared_levels
        ],
        "harvest_summary": {
            "declared_codes": len(declared_codes),
            "observed_codes": len([e for e in entries if e["observed"]]),
            "observed_error_instances": total_observed,
            "packages_analyzed": len(packages),
            "packages_with_errors": len(packages_with_errors),
        },
        "errors": entries,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build Photon error taxonomy from harvester output")
    ap.add_argument("--declared", required=True, type=Path)
    ap.add_argument("--observed", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()

    for path in (args.declared, args.observed):
        if not path.exists():
            raise SystemExit(f"ERROR: input not found: {path}")

    taxonomy = build(read_jsonl(args.declared), read_jsonl(args.observed))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(taxonomy, indent=2) + "\n", encoding="utf-8")

    s = taxonomy["harvest_summary"]
    print(f"✓ Taxonomy written: {args.output}")
    print(f"  Declared codes           : {s['declared_codes']}")
    print(f"  Observed in live runs    : {s['observed_codes']}")
    print(f"  Real error instances     : {s['observed_error_instances']}")
    print(f"  Packages analyzed        : {s['packages_analyzed']} "
          f"({s['packages_with_errors']} produced errors)")


if __name__ == "__main__":
    main()
