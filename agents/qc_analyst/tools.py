"""
QC-Analyst Agent — ClickHouse tool wrappers.

Each function here is an ADK tool the QC-Analyst calls. They map onto the
analytical queries in data/sample_queries.sql and every one of them runs
through the official mcp-clickhouse MCP server.

These are the queries that make ClickHouse load-bearing:
  - Fast OLAP over 50M rows
  - Vendor x error code x codec analytics
  - Risk scoring against historical distributions
  - Cost trend analysis

SQL SAFETY
The MCP tool signature is `run_query(query: str)`; there is no parameter
binding channel, so values cannot be bound server-side. Every value below goes
through quote_literal() and every identifier through quote_identifier(). The
previous version interpolated agent-supplied strings straight into SQL with
f-strings — injectable, and broken for any value containing an apostrophe.
"""

from __future__ import annotations

import os
from typing import Any

import structlog

from agents.shared.clickhouse_mcp import (
    quote_identifier,
    quote_literal,
    readonly_client,
)

log = structlog.get_logger(__name__)

DATABASE = quote_identifier(os.environ.get("CLICKHOUSE_DATABASE", "preflight"))
TABLE = f"{DATABASE}.qc_inspections"

# Bound the lookback so a hallucinated value cannot turn a bounded scan into a
# full-history one, and so INTERVAL never receives a non-integer.
MAX_LOOKBACK_DAYS = 3650


def _lookback(days: Any, default: int = 90) -> int:
    try:
        value = int(days)
    except (TypeError, ValueError):
        log.warning("tool.lookback.invalid", value=days, using=default)
        return default
    return max(1, min(value, MAX_LOOKBACK_DAYS))


async def get_vendor_failure_rate(
    vendor_id: str,
    platform_spec: str,
    lookback_days: int = 90,
) -> dict[str, Any]:
    """
    Get this vendor's historical failure rate by error code for the last N days.

    Returns the top failure codes, rates, and average remediation costs.
    This is Q1 from sample_queries.sql — the foundational risk signal.

    Args:
        vendor_id: Vendor identifier (e.g. VND_ROUNDABOUT)
        platform_spec: Platform spec (e.g. netflix-imf-2.1)
        lookback_days: How many days of history to query (default 90)
    """
    days = _lookback(lookback_days)
    client = readonly_client()

    sql = f"""
        SELECT
            error_code,
            error_category,
            count()                                       AS total_inspections,
            countIf(result = 'fail')                      AS failures,
            round(ifNotFinite(countIf(result = 'fail') / count(), 0), 3) AS fail_rate,
            round(ifNotFinite(avg(remediation_cost_usd), 0), 2)     AS avg_cost_usd,
            round(sum(remediation_cost_usd), 0)           AS total_cost_usd
        FROM {TABLE}
        WHERE
            vendor_id = {quote_literal(vendor_id)}
            AND platform_spec = {quote_literal(platform_spec)}
            AND inspected_at >= now() - INTERVAL {days} DAY
            AND error_code != ''
        GROUP BY error_code, error_category
        ORDER BY failures DESC
        LIMIT 10
    """

    summary_sql = f"""
        SELECT
            count()                                      AS total_submissions,
            countIf(result = 'fail')                     AS total_failures,
            round(ifNotFinite(countIf(result = 'fail') / count(), 0), 3) AS overall_fail_rate
        FROM {TABLE}
        WHERE vendor_id = {quote_literal(vendor_id)}
          AND inspected_at >= now() - INTERVAL {days} DAY
    """

    log.info("tool.vendor_failure_rate", vendor_id=vendor_id, platform_spec=platform_spec)
    rows = await client.query(sql)
    summary = await client.query(summary_sql)

    return {
        "vendor_id": vendor_id,
        "platform_spec": platform_spec,
        "lookback_days": days,
        "summary": summary[0] if summary else {},
        "top_failure_codes": rows,
    }


async def get_codec_error_clusters(
    codec: str,
    hdr_format: str,
    platform_spec: str,
) -> dict[str, Any]:
    """
    Get which error codes cluster with this codec + HDR format combination.

    Returns error codes ordered by frequency within this codec/HDR pairing.
    This is Q4 from sample_queries.sql — reveals codec-specific failure signatures.

    Args:
        codec: Video codec (e.g. JPEG2000, H.265, ProRes)
        hdr_format: HDR format (e.g. DolbyVision, HDR10, SDR)
        platform_spec: Platform spec identifier
    """
    sql = f"""
        SELECT
            error_code,
            error_category,
            count()                                 AS occurrences,
            round(ifNotFinite(count() / sum(count()) OVER (), 0), 3) AS share_of_all_failures
        FROM {TABLE}
        WHERE
            codec = {quote_literal(codec)}
            AND hdr_format = {quote_literal(hdr_format)}
            AND platform_spec = {quote_literal(platform_spec)}
            AND result = 'fail'
            AND error_code != ''
        GROUP BY error_code, error_category
        HAVING occurrences > 10
        ORDER BY occurrences DESC
        LIMIT 10
    """
    log.info("tool.codec_clusters", codec=codec, hdr_format=hdr_format)
    rows = await readonly_client().query(sql)

    return {
        "codec": codec,
        "hdr_format": hdr_format,
        "platform_spec": platform_spec,
        "error_clusters": rows,
    }


async def get_risk_score_inputs(
    vendor_id: str,
    platform_spec: str,
    codec: str,
    hdr_format: str,
) -> dict[str, Any]:
    """
    Pull the combined risk signals used to compute a risk score.

    This is Q3 from sample_queries.sql — the pre-flight risk scoring query.
    Returns historical fail rate, avg attempts, cost-to-date, and top failure codes.

    Args:
        vendor_id: Vendor identifier
        platform_spec: Platform spec
        codec: Video codec
        hdr_format: HDR format
    """
    client = readonly_client()

    sql = f"""
        SELECT
            count()                                      AS historical_submissions,
            countIf(result = 'fail')                     AS historical_failures,
            round(ifNotFinite(countIf(result = 'fail') / count(), 0), 3) AS historical_fail_rate,
            round(ifNotFinite(avg(redelivery_attempt), 0), 2)      AS avg_redelivery_attempts,
            round(sum(remediation_cost_usd), 0)          AS total_cost_to_date_usd,
            -- avgIf, not avg: passing and warning rows carry a cost of 0 and are
            -- ~96% of the table, so a plain avg reported $611 per failure when
            -- the real figure is ~$7,830. The diluted number was heading for the
            -- cost-per-failure dashboard tile.
            round(ifNotFinite(avgIf(remediation_cost_usd, result = 'fail'), 0), 0)
                                                         AS avg_cost_per_failure_usd
        FROM {TABLE}
        WHERE
            vendor_id = {quote_literal(vendor_id)}
            AND platform_spec = {quote_literal(platform_spec)}
            AND codec = {quote_literal(codec)}
            AND inspected_at >= now() - INTERVAL 365 DAY
    """

    top_codes_sql = f"""
        SELECT error_code, count() AS cnt
        FROM {TABLE}
        WHERE vendor_id = {quote_literal(vendor_id)}
          AND codec = {quote_literal(codec)}
          AND result = 'fail'
          AND error_code != ''
          AND inspected_at >= now() - INTERVAL 365 DAY
        GROUP BY error_code
        ORDER BY cnt DESC
        LIMIT 5
    """

    # Corpus-wide rate over the same window. A vendor's absolute failure rate is
    # not interpretable on its own — 8% means nothing until you know the
    # baseline is 3% — and the risk score needs the ratio, not the raw rate.
    baseline_sql = f"""
        SELECT round(ifNotFinite(countIf(result = 'fail') / count(), 0), 4) AS baseline_fail_rate
        FROM {TABLE}
        WHERE inspected_at >= now() - INTERVAL 365 DAY
    """

    log.info("tool.risk_score_inputs", vendor_id=vendor_id, codec=codec)
    rows = await client.query(sql)
    top_codes = await client.query(top_codes_sql)
    baseline = await client.query(baseline_sql)

    return {
        "vendor_id": vendor_id,
        "platform_spec": platform_spec,
        "codec": codec,
        "hdr_format": hdr_format,
        "risk_inputs": rows[0] if rows else {},
        "baseline_fail_rate": (baseline[0]["baseline_fail_rate"] if baseline else 0.0),
        "predicted_failure_codes": [r["error_code"] for r in top_codes],
    }


async def get_redelivery_cost_estimate(
    vendor_id: str,
    error_codes: list[str],
) -> dict[str, Any]:
    """
    Estimate remediation cost based on historical cost data for these error codes.

    Returns average and p95 cost per failure code, and a total estimate.
    Used by the Orchestrator to populate the cost field in the redelivery record.

    Args:
        vendor_id: Vendor identifier
        error_codes: List of error codes found in this QC run
    """
    if not error_codes:
        return {
            "vendor_id": vendor_id,
            "error_codes": [],
            "by_code": [],
            "estimated_total_cost_usd": 0.0,
        }

    codes_list = ", ".join(quote_literal(c) for c in error_codes)
    sql = f"""
        SELECT
            error_code,
            round(ifNotFinite(avg(remediation_cost_usd), 0), 2)            AS avg_cost_usd,
            round(ifNotFinite(quantile(0.95)(remediation_cost_usd), 0), 2) AS p95_cost_usd,
            count()                                        AS sample_size
        FROM {TABLE}
        WHERE
            vendor_id = {quote_literal(vendor_id)}
            AND error_code IN ({codes_list})
            AND result = 'fail'
            AND remediation_cost_usd > 0
        GROUP BY error_code
    """
    log.info("tool.cost_estimate", vendor_id=vendor_id, codes=error_codes)
    client = readonly_client()
    rows = await client.query(sql)
    estimate_basis = "vendor_error_history"

    # A new/demo vendor may have no exact-code history even though ClickHouse
    # contains useful observations for the same failure codes. In that case,
    # use the corpus-wide code history rather than presenting $0 as a cost.
    if not rows:
        fallback_sql = f"""
            SELECT
                error_code,
                round(ifNotFinite(avg(remediation_cost_usd), 0), 2) AS avg_cost_usd,
                round(ifNotFinite(quantile(0.95)(remediation_cost_usd), 0), 2) AS p95_cost_usd,
                count() AS sample_size
            FROM {TABLE}
            WHERE
                error_code IN ({codes_list})
                AND result = 'fail'
                AND remediation_cost_usd > 0
            GROUP BY error_code
        """
        rows = await client.query(fallback_sql)
        estimate_basis = "cross_vendor_error_history" if rows else "unavailable"

    total = sum(float(r.get("avg_cost_usd") or 0) for r in rows)
    sample_size = sum(int(r.get("sample_size") or 0) for r in rows)
    return {
        "vendor_id": vendor_id,
        "error_codes": error_codes,
        "by_code": rows,
        "estimated_total_cost_usd": round(total, 2),
        "estimate_basis": estimate_basis,
        "sample_size": sample_size,
    }


async def get_title_history(title_id: str) -> dict[str, Any]:
    """
    Get the complete redelivery history for a title.

    Shows every inspection attempt, result, and error in chronological order.
    Used by the Orchestrator to understand the full delivery chain context.

    Args:
        title_id: Platform title identifier (e.g. NFLX_100042)
    """
    sql = f"""
        SELECT
            redelivery_attempt,
            inspected_at,
            result,
            error_code,
            error_severity,
            error_message,
            resolved,
            vendor_id,
            codec
        FROM {TABLE}
        WHERE title_id = {quote_literal(title_id)}
        ORDER BY redelivery_attempt ASC, inspected_at ASC
        LIMIT 50
    """
    log.info("tool.title_history", title_id=title_id)
    rows = await readonly_client().query(sql)

    # A title emits one row per error, so row count is not attempt count.
    attempts = {r.get("redelivery_attempt") for r in rows}

    return {
        "title_id": title_id,
        "total_attempts": len(attempts),
        "rows_returned": len(rows),
        "history": rows,
    }
