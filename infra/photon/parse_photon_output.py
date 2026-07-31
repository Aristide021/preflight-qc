#!/usr/bin/env python3
"""
Parse raw Photon output text → structured error_taxonomy.json.

Photon outputs errors in varying formats depending on version:
  - Plain text: "ERROR [IMF_CPL_ERROR] Missing ApplicationIdentification"
  - Java exception style: "IMFException: ... IMF_AM_ERROR ..."
  - Structured: "Error: code=IMF_ESSENCE_EXCEPTION message=..."

This parser handles all three patterns and produces a canonical taxonomy file.

Usage:
    python parse_photon_output.py --input raw/photon_run_*.txt \
                                   --output error_taxonomy.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Known Photon error codes with their categories and typical severities ────
# Sourced from: Netflix/photon source, SMPTE ST 2067, Netflix delivery spec
# This is the "seed" vocabulary; actual harvest from Photon output extends it.
KNOWN_ERROR_CODES: dict[str, dict] = {
    "IMF_CPL_ERROR": {
        "category": "structural",
        "severity": "blocking",
        "description": "Composition Playlist structural or schema violation",
    },
    "IMF_AM_ERROR": {
        "category": "structural",
        "severity": "blocking",
        "description": "ASSETMAP validation error (missing assets, bad UUIDs)",
    },
    "IMF_PKL_ERROR": {
        "category": "structural",
        "severity": "blocking",
        "description": "Packing List validation error (hash mismatch, missing entries)",
    },
    "IMF_ESSENCE_EXCEPTION": {
        "category": "essence",
        "severity": "blocking",
        "description": "MXF essence file read or structural error",
    },
    "IMF_MASTER_PACKAGE_ERROR": {
        "category": "structural",
        "severity": "blocking",
        "description": "IMF Master Package level validation failure",
    },
    "IMF_CORE_CONSTRAINTS_EXCEPTION": {
        "category": "structural",
        "severity": "blocking",
        "description": "SMPTE ST 2067-2 core constraints violation",
    },
    "IMF_APP_CONSTRAINTS_EXCEPTION": {
        "category": "structural",
        "severity": "blocking",
        "description": "Application-level constraints violation (e.g., App #2E profile)",
    },
    "IMF_HASH_ERROR": {
        "category": "integrity",
        "severity": "blocking",
        "description": "File hash mismatch — package integrity compromised",
    },
    "IMF_INTEROP_ERROR": {
        "category": "structural",
        "severity": "advisory",
        "description": "Interoperability issue between components",
    },
    "IMF_EDIT_RATE_ERROR": {
        "category": "essence",
        "severity": "blocking",
        "description": "Edit rate inconsistency between CPL and essence",
    },
    "IMF_TIMECODE_ERROR": {
        "category": "essence",
        "severity": "cosmetic",
        "description": "Timecode track error or inconsistency",
    },
    "IMF_AUDIO_CHANNEL_ERROR": {
        "category": "audio",
        "severity": "blocking",
        "description": "Audio channel count or layout does not match CPL declaration",
    },
    "IMF_AUDIO_LOUDNESS_ERROR": {
        "category": "audio",
        "severity": "blocking",
        "description": "Integrated loudness outside spec range (−24 LKFS / −27 LUFS)",
    },
    "IMF_SUBTITLE_ERROR": {
        "category": "subtitle",
        "severity": "blocking",
        "description": "Subtitle/caption track missing or malformed",
    },
    "IMF_DOLBY_VISION_ERROR": {
        "category": "metadata",
        "severity": "blocking",
        "description": "Dolby Vision metadata missing, invalid, or not matching essence",
    },
    "IMF_HDR_ERROR": {
        "category": "metadata",
        "severity": "blocking",
        "description": "HDR metadata (SMPTE ST 2086 / 2094) validation failure",
    },
    "IMF_COLOR_SPACE_ERROR": {
        "category": "essence",
        "severity": "blocking",
        "description": "Color space declaration inconsistent with essence encoding",
    },
    "IMF_FRAME_RATE_ERROR": {
        "category": "essence",
        "severity": "blocking",
        "description": "Frame rate does not match spec or CPL declaration",
    },
    "IMF_RESOLUTION_ERROR": {
        "category": "essence",
        "severity": "blocking",
        "description": "Image resolution does not match CPL or platform spec",
    },
    "IMF_CODEC_ERROR": {
        "category": "essence",
        "severity": "blocking",
        "description": "Codec profile or level not compliant with spec",
    },
    "IMF_BITRATE_ERROR": {
        "category": "essence",
        "severity": "advisory",
        "description": "Bitrate outside recommended range",
    },
    "IMF_METADATA_ERROR": {
        "category": "metadata",
        "severity": "cosmetic",
        "description": "Ancillary metadata issue (title, language tag, etc.)",
    },
    "IMF_UUID_ERROR": {
        "category": "structural",
        "severity": "blocking",
        "description": "Invalid or duplicate UUID in package",
    },
    "IMF_VERSION_ERROR": {
        "category": "structural",
        "severity": "blocking",
        "description": "Schema version mismatch or unsupported IMF version",
    },
}

# ── Regex patterns for extracting error codes from Photon output ─────────────
# Pattern 1: [IMF_CPL_ERROR] or IMF_CPL_ERROR in brackets or bare
CODE_PATTERN = re.compile(r"\b(IMF_[A-Z_]+(?:ERROR|EXCEPTION|FAILURE))\b")

# Pattern 2: Java exception class names that map to error categories
EXCEPTION_PATTERN = re.compile(
    r"(IMFException|MXFException|CPLException|PKLException|AssetMapException)",
    re.IGNORECASE,
)

# Pattern 3: SMPTE error codes in message context
SMPTE_PATTERN = re.compile(r"SMPTE\s+ST\s+(\d{4}(?:-\d+)?)", re.IGNORECASE)


def parse_photon_output(raw_text: str) -> list[dict]:
    """Extract error entries from raw Photon stdout/stderr."""
    found_errors: list[dict] = []
    seen: set[str] = set()

    for line_num, line in enumerate(raw_text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        # Extract IMF_* error codes
        for match in CODE_PATTERN.finditer(line):
            code = match.group(1)
            if code not in seen:
                seen.add(code)
                known = KNOWN_ERROR_CODES.get(code, {})
                found_errors.append(
                    {
                        "error_code": code,
                        "category": known.get("category", "unknown"),
                        "severity": known.get("severity", "blocking"),
                        "description": known.get("description", ""),
                        "example_message": line[:200],  # first 200 chars of context
                        "source_line": line_num,
                        "source": "photon_harvest",
                    }
                )

        # If no IMF_ code found, check for exception class names
        if not CODE_PATTERN.search(line):
            for match in EXCEPTION_PATTERN.finditer(line):
                exc_name = match.group(1)
                synthetic_code = exc_name.upper().replace("EXCEPTION", "_EXCEPTION")
                if synthetic_code not in seen:
                    seen.add(synthetic_code)
                    found_errors.append(
                        {
                            "error_code": synthetic_code,
                            "category": "unknown",
                            "severity": "blocking",
                            "description": f"Java exception: {exc_name}",
                            "example_message": line[:200],
                            "source_line": line_num,
                            "source": "photon_harvest_exception",
                        }
                    )

    return found_errors


def build_taxonomy(
    harvested: list[dict],
    include_seed: bool = True,
) -> dict:
    """
    Merge harvested errors with the seed vocabulary.
    Seed entries for codes not found in the harvest are included but flagged.
    """
    harvested_codes = {e["error_code"] for e in harvested}
    errors = list(harvested)

    if include_seed:
        for code, meta in KNOWN_ERROR_CODES.items():
            if code not in harvested_codes:
                errors.append(
                    {
                        "error_code": code,
                        "category": meta["category"],
                        "severity": meta["severity"],
                        "description": meta["description"],
                        "example_message": "",
                        "source_line": None,
                        "source": "seed_vocabulary",  # not from live Photon run
                    }
                )

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "harvest_summary": {
            "total_codes": len(errors),
            "live_harvested": len(harvested),
            "seed_only": len(errors) - len(harvested),
        },
        "categories": sorted(
            {
                "structural": "Package/schema structural violations",
                "essence": "MXF essence (video/audio track) violations",
                "audio": "Audio-specific compliance failures",
                "subtitle": "Subtitle/caption track failures",
                "metadata": "Ancillary metadata violations",
                "integrity": "File integrity / hash failures",
                "advisory": "Non-blocking advisory notices",
                "unknown": "Unclassified errors",
            }.items()
        ),
        "errors": sorted(errors, key=lambda e: (e["category"], e["error_code"])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse Photon output → error taxonomy JSON")
    parser.add_argument("--input", required=True, help="Path to raw Photon output text file")
    parser.add_argument(
        "--output",
        default="data/photon_harvest/error_taxonomy.json",
        help="Output JSON file path",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="Only include codes found in live harvest (no seed vocabulary)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    raw_text = input_path.read_text(encoding="utf-8", errors="replace")
    print(f"▶ Parsing {len(raw_text)} characters from {input_path.name}...")

    harvested = parse_photon_output(raw_text)
    print(f"✓ Extracted {len(harvested)} distinct error codes from live run")

    taxonomy = build_taxonomy(harvested, include_seed=not args.no_seed)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(taxonomy, indent=2), encoding="utf-8")

    print(f"✓ Taxonomy written: {output_path}")
    print(f"  Total codes: {taxonomy['harvest_summary']['total_codes']}")
    print(f"  Live harvested: {taxonomy['harvest_summary']['live_harvested']}")
    print(f"  Seed-only: {taxonomy['harvest_summary']['seed_only']}")

    if taxonomy["harvest_summary"]["live_harvested"] >= 10:
        print("\n✅ Gate 1 criterion met (≥10 distinct codes from live Photon run)")
    else:
        print(
            f"\n⚠️  Gate 1 criterion NOT met — only "
            f"{taxonomy['harvest_summary']['live_harvested']} codes from live run. "
            f"Try more test vectors."
        )


if __name__ == "__main__":
    main()
