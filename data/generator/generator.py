#!/usr/bin/env python3
"""
Synthetic QC inspection log generator.

Generates a realistic, Photon-grounded corpus of IMF QC inspection events.
Output: Parquet files ready for ClickHouse local load.

Design philosophy:
  - Error codes sourced from data/photon_harvest/error_taxonomy.json (Photon-grounded)
  - Failure distributions weighted toward documented-common failures
    (CPL errors are the most frequent automated failure category)
  - Realistic redelivery loops: titles fail → redeliver → fail again → resolve
    (narratively shaped history that agents can reason over)
  - Volume: 10–50M rows across 500+ titles / 20+ vendors / 3+ spec versions

Usage:
    # Install generator deps (separate from runtime deps)
    uv sync --extra generator

    # Generate 10M rows (default)
    uv run python data/generator/generator.py --rows 10_000_000 --out data/generator/output/

    # Generate 50M rows for full corpus
    uv run python data/generator/generator.py --rows 50_000_000 --out data/generator/output/ \
        --chunk-size 1_000_000

    # Quick dev test (100k rows)
    uv run python data/generator/generator.py --rows 100_000 --out /tmp/qc_dev/
"""

from __future__ import annotations

import argparse
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from faker import Faker
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────────────────────
# Load Photon-grounded error taxonomy
# ─────────────────────────────────────────────────────────────────────────────
_TAXONOMY_PATH = Path(__file__).parent.parent / "photon_harvest" / "error_taxonomy.json"

def _load_taxonomy() -> list[dict]:
    """Load error taxonomy from Photon harvest output."""
    if _TAXONOMY_PATH.exists():
        data = json.loads(_TAXONOMY_PATH.read_text())
        return data.get("errors", [])
    # Fallback: minimal seed if harvest hasn't run yet
    print(f"⚠️  Taxonomy not found at {_TAXONOMY_PATH}. Using minimal seed.")
    print("    Run infra/photon/run_photon.sh first for full grounding.")
    return [
        {"error_code": "IMF_CPL_ERROR", "category": "structural", "severity": "blocking"},
        {"error_code": "IMF_AM_ERROR", "category": "structural", "severity": "blocking"},
        {"error_code": "IMF_PKL_ERROR", "category": "structural", "severity": "blocking"},
        {"error_code": "IMF_ESSENCE_EXCEPTION", "category": "essence", "severity": "blocking"},
        {"error_code": "IMF_AUDIO_LOUDNESS_ERROR", "category": "audio", "severity": "blocking"},
        {"error_code": "IMF_SUBTITLE_ERROR", "category": "subtitle", "severity": "blocking"},
        {"error_code": "IMF_DOLBY_VISION_ERROR", "category": "metadata", "severity": "blocking"},
        {"error_code": "IMF_COLOR_SPACE_ERROR", "category": "essence", "severity": "blocking"},
        {"error_code": "IMF_HASH_ERROR", "category": "integrity", "severity": "blocking"},
        {"error_code": "IMF_METADATA_ERROR", "category": "metadata", "severity": "cosmetic"},
    ]


TAXONOMY = _load_taxonomy()

# ─────────────────────────────────────────────────────────────────────────────
# Failure weight distribution
# Grounded in documented Netflix IMF submission patterns:
#   - CPL/structural errors: most common automated failure (~40%)
#   - Audio compliance: 2nd most common (~20%)
#   - Essence/codec: ~15%
#   - Metadata: ~10%
#   - Subtitle: ~8%
#   - Integrity/hash: ~5%
#   - Advisory: ~2%
# ─────────────────────────────────────────────────────────────────────────────
CATEGORY_WEIGHTS: dict[str, float] = {
    "structural": 0.40,
    "audio":      0.20,
    "essence":    0.15,
    "metadata":   0.10,
    "subtitle":   0.08,
    "integrity":  0.05,
    "advisory":   0.02,
}

# Group error codes by category for weighted sampling
_CODES_BY_CATEGORY: dict[str, list[str]] = {}
for entry in TAXONOMY:
    cat = entry.get("category", "unknown")
    _CODES_BY_CATEGORY.setdefault(cat, []).append(entry["error_code"])

# ─────────────────────────────────────────────────────────────────────────────
# Platform / vendor / codec universe
# ─────────────────────────────────────────────────────────────────────────────
PLATFORMS = [
    {"spec": "netflix-imf-2.0", "version": "netflix-imf-2.0-20230601"},
    {"spec": "netflix-imf-2.1", "version": "netflix-imf-2.1-20240101"},
    {"spec": "netflix-imf-2.2", "version": "netflix-imf-2.2-20250101"},
]

VENDORS = [
    {"id": "VND_DELUXE", "name": "Deluxe Media"},
    {"id": "VND_TECHNICOLOR", "name": "Technicolor"},
    {"id": "VND_HARBOR", "name": "Harbor Picture Company"},
    {"id": "VND_PUREPOST", "name": "Pure Post"},
    {"id": "VND_EFILM", "name": "eFilm"},
    {"id": "VND_STREAMLAND", "name": "Streamland Media"},
    {"id": "VND_FOTOKEM", "name": "FotoKem"},
    {"id": "VND_ROUNDABOUT", "name": "Roundabout Entertainment"},
    {"id": "VND_ASCENT", "name": "Ascent Media"},
    {"id": "VND_PIKSEL", "name": "Piksel"},
    {"id": "VND_BRIGHTCOVE", "name": "Brightcove Post"},
    {"id": "VND_IYUNO", "name": "Iyuno"},
    {"id": "VND_MELS", "name": "MELS Studios"},
    {"id": "VND_CINELAB", "name": "Cinelab London"},
    {"id": "VND_CHAINSAW", "name": "Chainsaw Editorial"},
    {"id": "VND_FRAMESTORE", "name": "Framestore"},
    {"id": "VND_MPC", "name": "Moving Picture Company"},
    {"id": "VND_GOLDCREST", "name": "Goldcrest Post"},
    {"id": "VND_SOHONET", "name": "Sohonet"},
    {"id": "VND_INDEPENDENT", "name": "Independent Facilities"},
]

# Vendor quality profiles: base_fail_rate, preferred_codec, common_error_category
VENDOR_PROFILES: dict[str, dict] = {
    "VND_DELUXE":      {"base_fail_rate": 0.08, "codec": "JPEG2000", "weakness": "audio"},
    "VND_TECHNICOLOR": {"base_fail_rate": 0.06, "codec": "JPEG2000", "weakness": "metadata"},
    "VND_HARBOR":      {"base_fail_rate": 0.04, "codec": "JPEG2000", "weakness": "structural"},
    "VND_PUREPOST":    {"base_fail_rate": 0.12, "codec": "H.265",    "weakness": "structural"},
    "VND_EFILM":       {"base_fail_rate": 0.07, "codec": "JPEG2000", "weakness": "audio"},
    "VND_STREAMLAND":  {"base_fail_rate": 0.09, "codec": "H.264",    "weakness": "essence"},
    "VND_FOTOKEM":     {"base_fail_rate": 0.05, "codec": "JPEG2000", "weakness": "subtitle"},
    "VND_ROUNDABOUT":  {"base_fail_rate": 0.15, "codec": "ProRes",   "weakness": "audio"},
    "VND_ASCENT":      {"base_fail_rate": 0.06, "codec": "JPEG2000", "weakness": "metadata"},
    "VND_PIKSEL":      {"base_fail_rate": 0.18, "codec": "H.265",    "weakness": "structural"},
    "VND_BRIGHTCOVE":  {"base_fail_rate": 0.11, "codec": "H.264",    "weakness": "audio"},
    "VND_IYUNO":       {"base_fail_rate": 0.08, "codec": "ProRes",   "weakness": "subtitle"},
    "VND_MELS":        {"base_fail_rate": 0.07, "codec": "JPEG2000", "weakness": "essence"},
    "VND_CINELAB":     {"base_fail_rate": 0.05, "codec": "JPEG2000", "weakness": "metadata"},
    "VND_CHAINSAW":    {"base_fail_rate": 0.13, "codec": "ProRes",   "weakness": "audio"},
    "VND_FRAMESTORE":  {"base_fail_rate": 0.04, "codec": "JPEG2000", "weakness": "metadata"},
    "VND_MPC":         {"base_fail_rate": 0.05, "codec": "JPEG2000", "weakness": "essence"},
    "VND_GOLDCREST":   {"base_fail_rate": 0.06, "codec": "JPEG2000", "weakness": "subtitle"},
    "VND_SOHONET":     {"base_fail_rate": 0.09, "codec": "H.265",    "weakness": "structural"},
    "VND_INDEPENDENT": {"base_fail_rate": 0.22, "codec": "ProRes",   "weakness": "audio"},
}

CODECS = ["JPEG2000", "H.264", "H.265", "ProRes", "DNxHR"]
FRAME_RATES = ["23.976", "24", "25", "29.97", "30", "47.952", "48", "50", "59.94", "60"]
RESOLUTIONS = ["1920x1080", "3840x2160", "4096x2160", "1280x720"]
HDR_FORMATS = ["SDR", "HDR10", "HDR10+", "DolbyVision", "HLG"]
AUDIO_CONFIGS = ["5.1", "7.1", "Atmos", "Stereo", "Binaural", "5.1+Stereo"]
APP_PROFILES = ["App2E", "App2", "App5", "App5E"]
QC_STAGES = ["photon", "backlot", "iaas", "auto-qc", "manual-qc"]
PACKAGE_TYPES = ["IMF", "ProRes", "MXF", "DCP"]

ERROR_MESSAGES: dict[str, list[str]] = {
    "IMF_CPL_ERROR": [
        "Missing ApplicationIdentification element in CPL",
        "CPL EditRate does not match essence track EditRate",
        "Duplicate CPL UUID detected across package",
        "CPL SegmentList contains empty ResourceList",
    ],
    "IMF_AM_ERROR": [
        "ASSETMAP references asset not found on disk",
        "Invalid UUID format in ASSETMAP asset entry",
        "Missing required Creator element in ASSETMAP",
        "ASSETMAP VolumeCount mismatch",
    ],
    "IMF_AUDIO_LOUDNESS_ERROR": [
        "Integrated loudness -31.2 LKFS exceeds maximum -24.0 LKFS",
        "True peak level +0.8 dBTP above maximum -2.0 dBTP",
        "Integrated loudness -18.4 LKFS above maximum -24.0 LKFS",
        "Loudness measurement missing for audio track",
    ],
    "IMF_ESSENCE_EXCEPTION": [
        "MXF essence descriptor does not match track file header",
        "Partition pack start code invalid in track MXF",
        "Index table missing from video MXF track file",
        "Essence container label not recognized",
    ],
    "IMF_SUBTITLE_ERROR": [
        "Forced subtitle track required but not present",
        "Subtitle burn-in detected in video essence",
        "Timed text TTML schema validation failure",
        "Subtitle language tag does not match CPL declaration",
    ],
    "IMF_DOLBY_VISION_ERROR": [
        "Dolby Vision RPU missing from video track",
        "Dolby Vision profile 8 requires base layer in BL+RPU configuration",
        "Dolby Vision metadata version 1.0 not supported; minimum 2.x required",
    ],
}

_DEFAULT_MESSAGES = ["Validation error detected during QC inspection"]

# ─────────────────────────────────────────────────────────────────────────────
# Generator
# ─────────────────────────────────────────────────────────────────────────────
fake = Faker()
rng = np.random.default_rng(seed=42)


def _weighted_category() -> str:
    """Sample an error category according to documented failure distribution."""
    cats = list(CATEGORY_WEIGHTS.keys())
    weights = list(CATEGORY_WEIGHTS.values())
    return rng.choice(cats, p=weights / np.array(weights).sum())


def _error_code_for_category(category: str, vendor_weakness: str) -> str:
    """
    Sample an error code, biasing toward the vendor's known weakness category.
    This creates the realistic vendor-specific failure signatures that make
    historical reasoning meaningful.
    """
    # 60% chance: draw from vendor weakness category; 40%: draw from weighted dist
    if rng.random() < 0.60:
        use_category = vendor_weakness
    else:
        use_category = category

    codes = _CODES_BY_CATEGORY.get(use_category, [])
    if not codes:
        codes = _CODES_BY_CATEGORY.get("structural", ["IMF_CPL_ERROR"])

    return str(rng.choice(codes))


class TitleFactory:
    """
    Generates a universe of titles with realistic metadata.
    Each title has a vendor assignment and a "fragility score" that drives
    how many redelivery attempts it takes to resolve failures.
    """

    def __init__(self, n_titles: int = 500, n_vendors: int = 20) -> None:
        self.titles: list[dict] = []
        vendor_ids = [v["id"] for v in VENDORS[:n_vendors]]

        for i in range(n_titles):
            title_id = f"NFLX_{100000 + i}"
            vendor_id = str(rng.choice(vendor_ids))
            platform = PLATFORMS[rng.integers(len(PLATFORMS))]
            codec = VENDOR_PROFILES.get(vendor_id, {}).get(
                "codec", str(rng.choice(CODECS))
            )
            self.titles.append(
                {
                    "title_id": title_id,
                    "title_name": self._gen_title_name(),
                    "vendor_id": vendor_id,
                    "platform_spec": platform["spec"],
                    "spec_version": platform["version"],
                    "app_profile": str(rng.choice(APP_PROFILES)),
                    "codec": codec,
                    "frame_rate": str(rng.choice(FRAME_RATES)),
                    "resolution": str(rng.choice(RESOLUTIONS)),
                    "hdr_format": str(rng.choice(HDR_FORMATS)),
                    "audio_config": str(rng.choice(AUDIO_CONFIGS)),
                    # How error-prone this title's delivery chain is (0=clean, 1=very messy)
                    "fragility": float(rng.beta(2, 5)),
                }
            )

    @staticmethod
    def _gen_title_name() -> str:
        """Generate a plausible title name."""
        adjectives = ["The", "A", "Last", "First", "Dark", "Blue", "Red", "Lost", "New", "Old"]
        nouns = ["Kingdom", "Garden", "Machine", "Dream", "Ocean", "Storm", "Light", "Edge", "Wave"]
        return f"{random.choice(adjectives)} {fake.last_name()} {random.choice(nouns)}"


def generate_inspection_chain(
    title: dict,
    start_date: datetime,
) -> list[dict[str, Any]]:
    """
    Generate a complete inspection chain for a single title delivery:
      attempt 0 → possibly fail → attempt 1 → possibly fail → ... → resolve

    This is the "narratively shaped history" that QC-Analyst can reason over.
    """
    vendor_id = title["vendor_id"]
    profile = VENDOR_PROFILES.get(vendor_id, {"base_fail_rate": 0.10, "weakness": "structural"})
    base_fail_rate = profile["base_fail_rate"]
    weakness = profile.get("weakness", "structural")

    # Adjust fail rate by title fragility
    fail_rate = min(0.95, base_fail_rate * (1 + title["fragility"] * 2))

    # Max attempts varies: 80% of chains resolve in ≤3 attempts
    max_attempts = int(rng.choice([1, 2, 3, 4, 5, 6], p=[0.45, 0.25, 0.15, 0.08, 0.04, 0.03]))

    chain: list[dict[str, Any]] = []
    current_date = start_date
    resolved = False

    for attempt in range(max_attempts):
        # Simulate inspection duration (1–72 hours)
        submit_offset = timedelta(hours=int(rng.integers(1, 24)))
        inspect_offset = timedelta(hours=int(rng.integers(1, 48)))
        submitted_at = current_date + submit_offset
        inspected_at = submitted_at + inspect_offset

        # Determine pass/fail — fail rate decreases with each attempt (learning)
        attempt_fail_rate = fail_rate * (0.7 ** attempt)
        is_fail = rng.random() < attempt_fail_rate

        if is_fail and not resolved:
            # Sample error for this failure
            category = _weighted_category()
            error_code = _error_code_for_category(category, weakness)
            severity_map = {
                "structural": "blocking",
                "audio": "blocking",
                "essence": "blocking",
                "subtitle": "blocking",
                "integrity": "blocking",
                "metadata": "cosmetic",
                "advisory": "advisory",
            }
            severity = severity_map.get(category, "blocking")
            messages = ERROR_MESSAGES.get(error_code, _DEFAULT_MESSAGES)
            error_message = str(rng.choice(messages))
            result = "fail"
            cost = float(rng.uniform(500, 15000) * (1.5 ** attempt))  # cost escalates
        else:
            # Pass or resolve
            error_code = ""
            error_category_val = ""
            severity = "blocking"
            error_message = ""
            category = ""
            result = "pass"
            cost = 0.0
            resolved = True

        chain.append(
            {
                "inspection_id": str(uuid.uuid4()),
                "title_id": title["title_id"],
                "title_name": title["title_name"],
                "version_label": f"v{attempt + 1}" if attempt > 0 else "OV",
                "package_type": "IMF",
                "package_id": str(uuid.uuid4()),
                "vendor_id": vendor_id,
                "vendor_name": next(
                    v["name"] for v in VENDORS if v["id"] == vendor_id
                ),
                "platform_spec": title["platform_spec"],
                "spec_version": title["spec_version"],
                "app_profile": title["app_profile"],
                "submitted_at": submitted_at,
                "inspected_at": inspected_at,
                "stage": str(rng.choice(QC_STAGES)),
                "result": result,
                "error_code": error_code,
                "error_category": category,
                "error_severity": severity if result == "fail" else "blocking",
                "error_message": error_message,
                "error_context": "{}",
                "redelivery_attempt": attempt,
                "remediation_cost_usd": cost,
                "resolved": resolved or result == "pass",
                "resolved_at": inspected_at if (resolved or result == "pass") else None,
                "codec": title["codec"],
                "frame_rate": title["frame_rate"],
                "resolution": title["resolution"],
                "hdr_format": title["hdr_format"],
                "audio_config": title["audio_config"],
            }
        )

        current_date = inspected_at
        if resolved:
            break

    return chain


# ─────────────────────────────────────────────────────────────────────────────
# Parquet output schema (maps to ClickHouse table columns)
# ─────────────────────────────────────────────────────────────────────────────
PARQUET_SCHEMA = pa.schema(
    [
        pa.field("inspection_id", pa.string()),
        pa.field("title_id", pa.string()),
        pa.field("title_name", pa.string()),
        pa.field("version_label", pa.string()),
        pa.field("package_type", pa.string()),
        pa.field("package_id", pa.string()),
        pa.field("vendor_id", pa.string()),
        pa.field("vendor_name", pa.string()),
        pa.field("platform_spec", pa.string()),
        pa.field("spec_version", pa.string()),
        pa.field("app_profile", pa.string()),
        pa.field("submitted_at", pa.timestamp("us", tz="UTC")),
        pa.field("inspected_at", pa.timestamp("us", tz="UTC")),
        pa.field("stage", pa.string()),
        pa.field("result", pa.string()),
        pa.field("error_code", pa.string()),
        pa.field("error_category", pa.string()),
        pa.field("error_severity", pa.string()),
        pa.field("error_message", pa.string()),
        pa.field("error_context", pa.string()),
        pa.field("redelivery_attempt", pa.uint8()),
        pa.field("remediation_cost_usd", pa.float32()),
        pa.field("resolved", pa.bool_()),
        pa.field("resolved_at", pa.timestamp("us", tz="UTC")),
        pa.field("codec", pa.string()),
        pa.field("frame_rate", pa.string()),
        pa.field("resolution", pa.string()),
        pa.field("hdr_format", pa.string()),
        pa.field("audio_config", pa.string()),
    ]
)


def generate_corpus(
    target_rows: int,
    output_dir: Path,
    chunk_size: int = 500_000,
    n_titles: int = 500,
    n_vendors: int = 20,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> None:
    """
    Generate the full inspection corpus and write to Parquet files.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    if start_date is None:
        # 3 years of history
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=365 * 3)

    print(f"\n{'='*60}")
    print(f"  Delivery-QC Synthetic Corpus Generator")
    print(f"{'='*60}")
    print(f"  Target rows : {target_rows:,}")
    print(f"  Titles      : {n_titles}")
    print(f"  Vendors     : {n_vendors}")
    print(f"  Date range  : {start_date.date()} → {end_date.date()}")
    print(f"  Output dir  : {output_dir}")
    print(f"{'='*60}\n")

    factory = TitleFactory(n_titles=n_titles, n_vendors=n_vendors)
    date_range_days = (end_date - start_date).days

    rows_written = 0
    chunk_num = 0
    buffer: list[dict[str, Any]] = []

    with tqdm(total=target_rows, unit="rows", unit_scale=True) as pbar:
        while rows_written < target_rows:
            # Pick a random title and a random start date within the range
            title = factory.titles[rng.integers(len(factory.titles))]
            offset_days = int(rng.integers(0, date_range_days - 30))
            chain_start = start_date + timedelta(days=offset_days)

            chain = generate_inspection_chain(title, chain_start)
            buffer.extend(chain)

            if len(buffer) >= chunk_size:
                _write_chunk(buffer[:chunk_size], output_dir, chunk_num)
                rows_written += len(buffer[:chunk_size])
                pbar.update(len(buffer[:chunk_size]))
                buffer = buffer[chunk_size:]
                chunk_num += 1

        # Write final partial chunk
        if buffer and rows_written < target_rows:
            remaining = min(len(buffer), target_rows - rows_written)
            _write_chunk(buffer[:remaining], output_dir, chunk_num)
            rows_written += remaining
            pbar.update(remaining)

    print(f"\n✅ Generated {rows_written:,} rows across {chunk_num + 1} Parquet files")
    print(f"   Output: {output_dir}")
    _print_summary(output_dir)


def _write_chunk(rows: list[dict], output_dir: Path, chunk_num: int) -> None:
    """Write a chunk of rows to a Parquet file."""
    df = pd.DataFrame(rows)
    # Ensure timestamp columns are UTC-aware
    for col in ["submitted_at", "inspected_at", "resolved_at"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], utc=True)

    table = pa.Table.from_pandas(df, schema=PARQUET_SCHEMA, safe=False)
    out_path = output_dir / f"qc_inspections_{chunk_num:04d}.parquet"
    pq.write_table(table, out_path, compression="snappy")


def _print_summary(output_dir: Path) -> None:
    """Print a quick summary of the generated corpus."""
    files = list(output_dir.glob("*.parquet"))
    total_bytes = sum(f.stat().st_size for f in files)
    print(f"\n   Files   : {len(files)}")
    print(f"   Size    : {total_bytes / 1024 / 1024:.1f} MB")
    print(f"\n   Load into local ClickHouse:")
    print(f"   clickhouse-client --query \"INSERT INTO preflight.qc_inspections")
    print(f"     SELECT * FROM file('{output_dir}/*.parquet', Parquet)\"")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic QC inspection corpus (Photon-grounded)"
    )
    parser.add_argument(
        "--rows", type=int, default=10_000_000,
        help="Total target rows (default: 10M)"
    )
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).parent / "output",
        help="Output directory for Parquet files"
    )
    parser.add_argument(
        "--chunk-size", type=int, default=500_000,
        help="Rows per Parquet file (default: 500k)"
    )
    parser.add_argument(
        "--titles", type=int, default=500,
        help="Number of distinct titles (default: 500)"
    )
    parser.add_argument(
        "--vendors", type=int, default=20,
        help="Number of distinct vendors (default: 20)"
    )
    args = parser.parse_args()

    generate_corpus(
        target_rows=args.rows,
        output_dir=args.out,
        chunk_size=args.chunk_size,
        n_titles=args.titles,
        n_vendors=args.vendors,
    )


if __name__ == "__main__":
    main()
