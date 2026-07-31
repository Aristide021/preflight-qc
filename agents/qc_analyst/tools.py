"""
QC-Analyst Agent — ClickHouse tool wrappers.

Each function here is an ADK tool that the QC-Analyst agent calls via MCP.
They map 1:1 to the top-10 analytical queries in data/sample_queries.sql.

These are the queries that make ClickHouse load-bearing:
  - Fast OLAP over 50M+ rows
  - Vendor × error code × codec analytics
  - Risk scoring against historical distributions
  - Cost trend analysis

Every tool returns typed results that flow into the RiskAssessment model.
"""

from __future__ import annotations

import json
import os
from typing import Any

import structlog

from agents.shared.clickhouse_mcp import clickhouse_session, mcp_query

log = structlog.get_logger(__name__)

DATABASE = os.environ.get("CLICKHOUSE_DATABASE", "preflight")


# ─────────────────────────────────────────────────────────────────────────────
# ADK tool functions — called by the QC-Analyst agent
# Each is decorated with @tool in agent.py
# ─────────────────────────────────────────────────────────────────────────────

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
    sql = f"""
        SELECT
            error_code,
            error_category,
            count()                                       AS total_inspections,
            countIf(result = 'fail')                      AS failures,
            round(countIf(result = 'fail') / count(), 3)  AS fail_rate,
            round(avg(remediation_cost_usd), 2)           AS avg_cost_usd,
            round(sum(remediation_cost_usd), 0)           AS total_cost_usd
        FROM {DATABASE}.qc_inspections
        WHERE
            vendor_id = '{vendor_id}'
            AND platform_spec = '{platform_spec}'
            AND inspected_at >= now() - INTERVAL {lookback_days} DAY
            AND error_code != ''
        GROUP BY error_code, error_category
        ORDER BY fail_rate DESC
        LIMIT 10
    """
    log.info("tool.vendor_failure_rate", vendor_id=vendor_id, platform_spec=platform_spec)
    async with clickhouse_session() as session:
        rows = await mcp_query(session, sql)

    # Also get overall submission count for this vendor
    count_sql = f"""
        SELECT
            count() AS total_submissions,
            countIf(result = 'fail') AS total_failures,
            round(countIf(result = 'fail') / count(), 3) AS overall_fail_rate
        FROM {DATABASE}.qc_inspections
        WHERE vendor_id = '{vendor_id}'
          AND inspected_at >= now() - INTERVAL {lookback_days} DAY
    """
    async with clickhouse_session() as session:
        summary = await mcp_query(session, count_sql)

    return {
        "vendor_id": vendor_id,
        "platform_spec": platform_spec,
        "lookback_days": lookback_days,
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
            count()                                                 AS occurrences,
            round(count() / sum(count()) OVER (), 3)                AS share_of_all_failures
        FROM {DATABASE}.qc_inspections
        WHERE
            codec = '{codec}'
            AND hdr_format = '{hdr_format}'
            AND platform_spec = '{platform_spec}'
            AND result = 'fail'
            AND error_code != ''
        GROUP BY error_code, error_category
        HAVING occurrences > 10
        ORDER BY occurrences DESC
        LIMIT 10
    """
    log.info("tool.codec_clusters", codec=codec, hdr_format=hdr_format)
    async with clickhouse_session() as session:
        rows = await mcp_query(session, sql)

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
    sql = f"""
        SELECT
            count()                                                   AS historical_submissions,
            countIf(result = 'fail')                                  AS historical_failures,
            round(countIf(result = 'fail') / count(), 3)              AS historical_fail_rate,
            round(avg(redelivery_attempt), 1)                         AS avg_redelivery_attempts,
            round(sum(remediation_cost_usd), 0)                       AS total_cost_to_date_usd,
            round(avg(remediation_cost_usd), 0)                       AS avg_cost_per_failure_usd
        FROM {DATABASE}.qc_inspections
        WHERE
            vendor_id = '{vendor_id}'
            AND platform_spec = '{platform_spec}'
            AND codec = '{codec}'
            AND inspected_at >= now() - INTERVAL 365 DAY
    """
    log.info("tool.risk_score_inputs", vendor_id=vendor_id, codec=codec)
    async with clickhouse_session() as session:
        rows = await mcp_query(session, sql)

    # Get top predicted failure codes
    top_codes_sql = f"""
        SELECT error_code, count() AS cnt
        FROM {DATABASE}.qc_inspections
        WHERE vendor_id = '{vendor_id}'
          AND codec = '{codec}'
          AND result = 'fail'
          AND error_code != ''
          AND inspected_at >= now() - INTERVAL 365 DAY
        GROUP BY error_code
        ORDER BY cnt DESC
        LIMIT 5
    """
    async with clickhouse_session() as session:
        top_codes = await mcp_query(session, top_codes_sql)

    return {
        "vendor_id": vendor_id,
        "platform_spec": platform_spec,
        "codec": codec,
        "hdr_format": hdr_format,
        "risk_inputs": rows[0] if rows else {},
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
        return {"estimated_total_cost_usd": 0.0, "by_code": []}

    codes_list = ", ".join(f"'{c}'" for c in error_codes)
    sql = f"""
        SELECT
            error_code,
            round(avg(remediation_cost_usd), 2)               AS avg_cost_usd,
            round(quantile(0.95)(remediation_cost_usd), 2)     AS p95_cost_usd,
            count()                                            AS sample_size
        FROM {DATABASE}.qc_inspections
        WHERE
            vendor_id = '{vendor_id}'
            AND error_code IN ({codes_list})
            AND result = 'fail'
            AND remediation_cost_usd > 0
        GROUP BY error_code
    """
    log.info("tool.cost_estimate", vendor_id=vendor_id, codes=error_codes)
    async with clickhouse_session() as session:
        rows = await mcp_query(session, sql)

    total = sum(float(r.get("avg_cost_usd", 0)) for r in rows)
    return {
        "vendor_id": vendor_id,
        "error_codes": error_codes,
        "by_code": rows,
        "estimated_total_cost_usd": round(total, 2),
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
        FROM {DATABASE}.qc_inspections
        WHERE title_id = '{title_id}'
        ORDER BY redelivery_attempt ASC, inspected_at ASC
        LIMIT 50
    """
    log.info("tool.title_history", title_id=title_id)
    async with clickhouse_session() as session:
        rows = await mcp_query(session, sql)

    return {
        "title_id": title_id,
        "total_attempts": len(rows),
        "history": rows,
    }
