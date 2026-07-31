#!/usr/bin/env python3
"""
Gate 3 — prove the corpus is both large and fast.

Runs every query in sample_queries.sql against the LOCAL ClickHouse instance
and reports wall-clock latency plus the rows and bytes ClickHouse actually
read. Latency alone is not evidence: a query that scans 2,000 rows in 3ms says
nothing about a 50M-row table, so rows_read is reported alongside it.

Queries use ClickHouse's native {name:Type} parameter binding. Rather than
inventing parameter values, this samples real ones out of the loaded corpus —
a vendor that actually exists, a title with an actual redelivery chain — so
each query touches the data volume it would touch in production.

Usage:
    python data/benchmark_local.py
    python data/benchmark_local.py --repeat 5
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
import time
from pathlib import Path

import clickhouse_connect

QUERY_FILE = Path(__file__).parent / "sample_queries.sql"

# Latency budget. Gate 3 asks whether the analytical layer is real, so the
# threshold is on the interactive queries an agent or dashboard issues.
LATENCY_BUDGET_S = 2.0

# A query reading fewer rows than this is not exercising the corpus, and its
# fast latency is not evidence of anything.
MIN_MEANINGFUL_ROWS_READ = 100_000

QUERY_HEADER = re.compile(r"^--\s*[─\-]*\s*(Q\d+):\s*(.+?)\s*[─\-]*\s*$", re.M)
PARAM_RE = re.compile(r"\{(\w+):(\w+)\}")


def parse_queries(path: Path) -> list[dict]:
    """Split sample_queries.sql into named statements on its `-- ── Qn:` headers."""
    text = path.read_text()
    marks = list(QUERY_HEADER.finditer(text))
    queries = []

    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[start:end]

        # Keep only the statement itself: drop comment lines, stop at the ';'.
        body = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("--"))
        stmt = body.split(";")[0].strip()
        if stmt:
            queries.append({"id": m.group(1), "title": m.group(2).strip(), "sql": stmt})

    return queries


def sample_parameters(client) -> dict:
    """Draw real parameter values from the corpus so queries hit real data."""
    params: dict = {}

    row = client.query(
        """
        SELECT vendor_id, platform_spec, codec, hdr_format
        FROM preflight.qc_inspections
        WHERE result = 'fail'
        GROUP BY vendor_id, platform_spec, codec, hdr_format
        ORDER BY count() DESC
        LIMIT 1
        """
    ).result_rows
    if row:
        params.update(
            vendor_id=row[0][0],
            platform_spec=row[0][1],
            codec=row[0][2],
            hdr_format=row[0][3],
        )

    # A title with the longest redelivery chain — the heaviest Q7 case.
    row = client.query(
        """
        SELECT title_id
        FROM preflight.qc_inspections
        GROUP BY title_id
        ORDER BY max(redelivery_attempt) DESC, count() DESC
        LIMIT 1
        """
    ).result_rows
    if row:
        params["title_id"] = row[0][0]

    row = client.query(
        "SELECT error_code FROM preflight.qc_inspections "
        "WHERE error_code != '' GROUP BY error_code ORDER BY count() DESC LIMIT 1"
    ).result_rows
    if row:
        params["error_code"] = row[0][0]

    row = client.query(
        "SELECT error_category FROM preflight.qc_inspections "
        "WHERE error_category != '' GROUP BY error_category ORDER BY count() DESC LIMIT 1"
    ).result_rows
    if row:
        params["error_category"] = row[0][0]

    return params


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate 3 latency benchmark (local ClickHouse)")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--user", default="default")
    ap.add_argument("--password", default="preflight-local")
    ap.add_argument("--repeat", type=int, default=3, help="runs per query; median reported")
    args = ap.parse_args()

    if args.host not in ("localhost", "127.0.0.1", "::1"):
        print(f"REFUSING: --host {args.host} is not loopback. This benchmark is local-only.")
        return 2

    client = clickhouse_connect.get_client(
        host=args.host,
        port=args.port,
        username=args.user,
        password=args.password,
        database="preflight",
    )

    total_rows = client.query("SELECT count() FROM preflight.qc_inspections").result_rows[0][0]
    if total_rows == 0:
        print("Corpus is empty. Run ./data/load_local.sh first.")
        return 2

    print("=" * 78)
    print("  GATE 3 — corpus scale and query latency (local ClickHouse)")
    print("=" * 78)
    print(f"  Rows in qc_inspections : {total_rows:,}")
    print(f"  Runs per query         : {args.repeat} (median reported)")
    print(f"  Latency budget         : {LATENCY_BUDGET_S}s")
    print()

    params = sample_parameters(client)
    print("  Parameters sampled from the corpus:")
    for k, v in params.items():
        print(f"    {k:16s} = {v!r}")
    print()

    queries = parse_queries(QUERY_FILE)
    print(f"  Parsed {len(queries)} queries from {QUERY_FILE.name}")
    print()
    print(f"  {'':4s} {'query':<46s} {'median':>9s} {'rows read':>13s}")
    print("  " + "-" * 76)

    results = []
    for q in queries:
        needed = {name for name, _ in PARAM_RE.findall(q["sql"])}
        missing = needed - params.keys()
        if missing:
            print(f"  {q['id']:<4s} {q['title'][:46]:<46s} {'SKIP':>9s}   no value for {sorted(missing)}")
            results.append({**q, "status": "skipped"})
            continue

        timings, summary, error = [], {}, None
        for _ in range(args.repeat):
            try:
                start = time.perf_counter()
                res = client.query(q["sql"], parameters={k: params[k] for k in needed})
                timings.append(time.perf_counter() - start)
                summary = res.summary or {}
            except Exception as exc:  # noqa: BLE001 — a broken query is a finding
                error = str(exc).split("\n")[0][:110]
                break

        if error:
            print(f"  {q['id']:<4s} {q['title'][:46]:<46s} {'ERROR':>9s}")
            print(f"       └─ {error}")
            results.append({**q, "status": "error", "error": error})
            continue

        median = statistics.median(timings)
        rows_read = int(summary.get("read_rows", 0))
        results.append({**q, "status": "ok", "median": median, "rows_read": rows_read})
        print(f"  {q['id']:<4s} {q['title'][:46]:<46s} {median * 1000:>7.0f}ms {rows_read:>13,}")

    # ── Gate 3 verdict ──────────────────────────────────────────────────────
    ok = [r for r in results if r["status"] == "ok"]
    errors = [r for r in results if r["status"] == "error"]
    over_budget = [r for r in ok if r["median"] > LATENCY_BUDGET_S]
    scanned = [r for r in ok if r["rows_read"] >= MIN_MEANINGFUL_ROWS_READ]

    print()
    print("=" * 78)
    checks = [
        ("corpus is multi-million row", total_rows >= 10_000_000, f"{total_rows:,} rows"),
        ("every query runs without error", not errors, f"{len(errors)} errored"),
        ("all queries within latency budget", not over_budget, f"{len(over_budget)} over {LATENCY_BUDGET_S}s"),
        (
            "queries actually scan the corpus",
            len(scanned) >= max(1, len(ok) // 2),
            f"{len(scanned)}/{len(ok)} read >= {MIN_MEANINGFUL_ROWS_READ:,} rows",
        ),
    ]
    width = max(len(label) for label, _, _ in checks)
    for label, passed, detail in checks:
        print(f"  {'PASS' if passed else 'FAIL'}  {label:<{width}}  {detail}")

    print()
    if all(passed for _, passed, _ in checks):
        print("  GATE 3 GREEN — the corpus is large and the queries are fast.")
        print("=" * 78)
        return 0

    print("  GATE 3 NOT GREEN — see failing checks above.")
    print("=" * 78)
    return 1


if __name__ == "__main__":
    sys.exit(main())
