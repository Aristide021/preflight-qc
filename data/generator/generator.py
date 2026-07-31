#!/usr/bin/env python3
"""
Synthetic QC inspection log generator.

Generates a realistic, Photon-grounded corpus of IMF QC inspection events.
Output: Parquet files ready for ClickHouse local load.

Design philosophy:
  - Every error code is a real constant of Photon's IMFErrorLogger ErrorCodes
    enum, read from data/photon_harvest/error_taxonomy.json. The generator
    refuses to run without that file rather than inventing codes.
  - Failure distributions are measured, not asserted: code and category weights
    come from the frequencies Photon actually produced across the bundled IMF
    test packages. (Measured order is essence-component and core-constraints
    first, ahead of CPL errors.)
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
from datetime import UTC, datetime, timedelta
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
    """
    Load the Photon-harvested error taxonomy.

    There is deliberately no fallback seed list. The previous version fell back
    to hand-written codes — most of which do not exist in Photon — which meant a
    missing harvest silently produced a corpus of invented error codes. Failing
    loudly is the point: the taxonomy is the grounding, and a corpus built
    without it is worthless.
    """
    if not _TAXONOMY_PATH.exists():
        raise SystemExit(
            f"ERROR: error taxonomy not found at {_TAXONOMY_PATH}\n"
            "       Run ./infra/photon/harvest_all.sh first (Gate 1).\n"
            "       The generator will not invent error codes."
        )

    data = json.loads(_TAXONOMY_PATH.read_text())
    entries = data.get("errors", [])
    if not entries:
        raise SystemExit(f"ERROR: taxonomy at {_TAXONOMY_PATH} contains no errors.")
    return entries


TAXONOMY = _load_taxonomy()

# Every code sampled below is a real constant of Photon's ErrorCodes enum —
# nothing here is invented. Codes the harvest actually exercised are weighted by
# their measured frequency; codes Photon declares but the bundled test packages
# never triggered get a floor weight so they appear rarely rather than never.
# That floor is a modeling assumption (a real delivery pipeline sees the whole
# vocabulary, not just what 37 test packages happen to cover) — it is the only
# assumption in the frequency model, and it is confined to this constant.
UNOBSERVED_FLOOR_WEIGHT = 1.0

_OBSERVED = [e for e in TAXONOMY if e.get("observed") and e.get("observed_count", 0) > 0]
if not _OBSERVED:
    raise SystemExit("ERROR: taxonomy contains no observed codes — re-run the harvest.")

# Skip INTERNAL_ERROR: it signals a Photon processing fault, not a delivery
# defect, so it does not belong in a corpus of delivery QC results.
_SAMPLABLE = [e for e in TAXONOMY if e.get("category") != "internal"]

# ─────────────────────────────────────────────────────────────────────────────
# Failure weight distribution — derived, not asserted.
#
# Category and code frequencies come from the observed counts in the harvest
# (188 real error instances across 37 IMF packages). The previous version hard
# coded percentages ("CPL errors ~40%, audio ~20%") that were not measured from
# anything, and referenced audio/subtitle categories Photon does not validate.
#
# NOTE (open scope): Photon is a structural/essence IMF validator. It does not
# check loudness, subtitle presence, or Dolby Vision metadata. A real delivery
# pipeline runs those as separate checks against the platform delivery spec.
# Those spec-derived checks are a SECOND source and are intentionally absent
# here rather than invented — see data/photon_harvest/README.md.
# ─────────────────────────────────────────────────────────────────────────────
_CODES_BY_CATEGORY: dict[str, list[str]] = {}
_CODE_WEIGHTS_BY_CATEGORY: dict[str, list[float]] = {}
CATEGORY_WEIGHTS: dict[str, float] = {}

for entry in _SAMPLABLE:
    cat = entry.get("category", "unknown")
    weight = float(entry.get("observed_count", 0)) or UNOBSERVED_FLOOR_WEIGHT
    _CODES_BY_CATEGORY.setdefault(cat, []).append(entry["error_code"])
    _CODE_WEIGHTS_BY_CATEGORY.setdefault(cat, []).append(weight)
    CATEGORY_WEIGHTS[cat] = CATEGORY_WEIGHTS.get(cat, 0.0) + weight

_total_observed = sum(CATEGORY_WEIGHTS.values())
CATEGORY_WEIGHTS = {k: v / _total_observed for k, v in CATEGORY_WEIGHTS.items()}

# Normalise per-category code weights so each category sums to 1.0
for _cat, _weights in _CODE_WEIGHTS_BY_CATEGORY.items():
    _sum = sum(_weights)
    _CODE_WEIGHTS_BY_CATEGORY[_cat] = [w / _sum for w in _weights]

# Severity per code, taken from Photon's own error levels (FATAL / NON_FATAL →
# blocking, WARNING → advisory). "cosmetic" is not assigned here: it is a
# platform-spec judgment the Spec-Reader agent makes, not a validator verdict.
SEVERITY_BY_CODE: dict[str, str] = {
    e["error_code"]: (e.get("severity") or "blocking") for e in _SAMPLABLE
}

# Verbatim Photon messages, harvested per code. Codes the harvest never
# exercised have no example text and fall back to their enum label rather than
# to invented prose.
ERROR_MESSAGES: dict[str, list[str]] = {
    e["error_code"]: (e.get("example_messages") or [e.get("error_label") or e["error_code"]])
    for e in _SAMPLABLE
}

_DEFAULT_MESSAGES = ["Validation error reported by Photon"]

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
    "VND_DELUXE":      {"base_fail_rate": 0.08, "codec": "JPEG2000", "weakness": "essence"},
    "VND_TECHNICOLOR": {"base_fail_rate": 0.06, "codec": "JPEG2000", "weakness": "metadata"},
    "VND_HARBOR":      {"base_fail_rate": 0.04, "codec": "JPEG2000", "weakness": "structural"},
    "VND_PUREPOST":    {"base_fail_rate": 0.12, "codec": "H.265",    "weakness": "structural"},
    "VND_EFILM":       {"base_fail_rate": 0.07, "codec": "JPEG2000", "weakness": "essence"},
    "VND_STREAMLAND":  {"base_fail_rate": 0.09, "codec": "H.264",    "weakness": "essence"},
    "VND_FOTOKEM":     {"base_fail_rate": 0.05, "codec": "JPEG2000", "weakness": "integrity"},
    "VND_ROUNDABOUT":  {"base_fail_rate": 0.15, "codec": "ProRes",   "weakness": "essence"},
    "VND_ASCENT":      {"base_fail_rate": 0.06, "codec": "JPEG2000", "weakness": "metadata"},
    "VND_PIKSEL":      {"base_fail_rate": 0.18, "codec": "H.265",    "weakness": "structural"},
    "VND_BRIGHTCOVE":  {"base_fail_rate": 0.11, "codec": "H.264",    "weakness": "essence"},
    "VND_IYUNO":       {"base_fail_rate": 0.08, "codec": "ProRes",   "weakness": "integrity"},
    "VND_MELS":        {"base_fail_rate": 0.07, "codec": "JPEG2000", "weakness": "essence"},
    "VND_CINELAB":     {"base_fail_rate": 0.05, "codec": "JPEG2000", "weakness": "metadata"},
    "VND_CHAINSAW":    {"base_fail_rate": 0.13, "codec": "ProRes",   "weakness": "essence"},
    "VND_FRAMESTORE":  {"base_fail_rate": 0.04, "codec": "JPEG2000", "weakness": "metadata"},
    "VND_MPC":         {"base_fail_rate": 0.05, "codec": "JPEG2000", "weakness": "essence"},
    "VND_GOLDCREST":   {"base_fail_rate": 0.06, "codec": "JPEG2000", "weakness": "integrity"},
    "VND_SOHONET":     {"base_fail_rate": 0.09, "codec": "H.265",    "weakness": "structural"},
    "VND_INDEPENDENT": {"base_fail_rate": 0.22, "codec": "ProRes",   "weakness": "essence"},
}

CODECS = ["JPEG2000", "H.264", "H.265", "ProRes", "DNxHR"]
FRAME_RATES = ["23.976", "24", "25", "29.97", "30", "47.952", "48", "50", "59.94", "60"]
RESOLUTIONS = ["1920x1080", "3840x2160", "4096x2160", "1280x720"]
HDR_FORMATS = ["SDR", "HDR10", "HDR10+", "DolbyVision", "HLG"]
AUDIO_CONFIGS = ["5.1", "7.1", "Atmos", "Stereo", "Binaural", "5.1+Stereo"]
APP_PROFILES = ["App2E", "App2", "App5", "App5E"]
QC_STAGES = ["photon", "backlot", "iaas", "auto-qc", "manual-qc"]
PACKAGE_TYPES = ["IMF", "ProRes", "MXF", "DCP"]


# ─────────────────────────────────────────────────────────────────────────────
# Generator
# ─────────────────────────────────────────────────────────────────────────────
fake = Faker()
rng = np.random.default_rng(seed=42)


def _weighted_category() -> str:
    """Sample an error category using frequencies measured in the Photon harvest."""
    cats = list(CATEGORY_WEIGHTS.keys())
    weights = np.array(list(CATEGORY_WEIGHTS.values()), dtype=float)
    return str(rng.choice(cats, p=weights / weights.sum()))


def _error_code_for_category(category: str, vendor_weakness: str) -> str:
    """
    Sample an error code, biasing toward the vendor's known weakness category.
    This creates the realistic vendor-specific failure signatures that make
    historical reasoning meaningful.

    Within a category, codes are drawn in proportion to how often Photon
    actually emitted them during the harvest.
    """
    # 60% chance: draw from vendor weakness category; 40%: draw from weighted dist
    use_category = vendor_weakness if rng.random() < 0.60 else category

    codes = _CODES_BY_CATEGORY.get(use_category)
    if not codes:
        use_category = category if category in _CODES_BY_CATEGORY else next(iter(_CODES_BY_CATEGORY))
        codes = _CODES_BY_CATEGORY[use_category]

    weights = np.array(_CODE_WEIGHTS_BY_CATEGORY[use_category], dtype=float)
    return str(rng.choice(codes, p=weights / weights.sum()))


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
            # Severity comes from Photon's own error level for this code, not
            # from a hand-written category→severity table.
            severity = SEVERITY_BY_CODE.get(error_code, "blocking")
            messages = ERROR_MESSAGES.get(error_code, _DEFAULT_MESSAGES)
            error_message = str(rng.choice(messages))

            # An advisory finding does not reject a delivery. Photon raises
            # plenty of WARNING-level errors on packages that are otherwise
            # deliverable, so those land as 'warn' and neither cost remediation
            # nor extend the redelivery chain. Only blocking findings fail.
            if severity == "blocking":
                result = "fail"
                cost = float(rng.uniform(500, 15000) * (1.5 ** attempt))
            else:
                result = "warn"
                cost = 0.0
                resolved = True
        else:
            # Pass or resolve. Severity is blank rather than "blocking":
            # a passing inspection has no severity, and defaulting it to
            # blocking skews any severity aggregate over the corpus.
            error_code = ""
            severity = ""
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
                # Blank only on a clean pass — a 'warn' row keeps its advisory
                # severity, which is what makes advisory-vs-blocking queryable.
                "error_severity": severity,
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
        end_date = datetime.now(UTC)
        start_date = end_date - timedelta(days=365 * 3)

    print(f"\n{'='*60}")
    print("  Delivery-QC Synthetic Corpus Generator")
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

    # Count what was actually written. chunk_num is incremented only by the main
    # loop, so `chunk_num + 1` over-reported by one whenever the target divided
    # evenly into chunk_size and the trailing partial-chunk write was skipped.
    files_written = len(list(output_dir.glob("*.parquet")))
    print(f"\n✅ Generated {rows_written:,} rows across {files_written} Parquet files")
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
    print("\n   Load into local ClickHouse:")
    print("   clickhouse-client --query \"INSERT INTO preflight.qc_inspections")
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
