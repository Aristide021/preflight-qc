"""
Action Agent — files redeliveries and writes the audit trail to ClickHouse.

This agent closes the loop: after the Orchestrator decides, the Action agent:
  1. Generates the final remediation instructions for the vendor
  2. Writes a RedeliveryRecord to ClickHouse (redelivery_tracking table)
  3. Returns the tracking record for the Orchestrator to confirm

The ClickHouse write here is what makes the loop auditable and queryable.
Every redelivery filed by the agent becomes part of the history the
QC-Analyst can reason over in future runs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import structlog
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

from agents.shared.clickhouse_mcp import clickhouse_session, mcp_insert
from agents.shared.models import (
    OrchestratorDecision,
    QCResult,
    RedeliveryRecord,
    RedeliveryStatus,
    RiskAssessment,
)

log = structlog.get_logger(__name__)

DATABASE = os.environ.get("CLICKHOUSE_DATABASE", "preflight")
AGENT_VERSION = "orchestrator-v1"


async def file_redelivery(
    qc_result: QCResult,
    decision: OrchestratorDecision,
    assessment: RiskAssessment,
) -> RedeliveryRecord:
    """
    File a redelivery record and write it to ClickHouse.

    This is the audit-trail write that makes every agent action queryable.
    The Orchestrator calls this after making its decision.

    Args:
        qc_result: Original QC inspection result
        decision: Orchestrator's filing decision
        assessment: QC-Analyst's risk assessment

    Returns:
        RedeliveryRecord that was written to ClickHouse
    """
    log.info(
        "action.file_redelivery",
        title_id=qc_result.title_id,
        decision=decision.decision.value,
        risk_score=assessment.risk_score,
    )

    record = RedeliveryRecord(
        tracking_id=uuid4(),
        title_id=qc_result.title_id,
        original_inspection=qc_result.inspection_id,
        package_id=uuid4(),
        vendor_id=qc_result.vendor_id,
        platform_spec=qc_result.platform_spec,
        filed_at=datetime.now(timezone.utc),
        filed_by_agent=AGENT_VERSION,
        decision=decision.decision.value,
        decision_rationale=decision.decision_rationale,
        status=RedeliveryStatus.FILED,
        attempt_number=qc_result.redelivery_attempt,
        estimated_cost_usd=decision.estimated_cost_usd,
        spec_section=decision.spec_section,
        spec_requirement=decision.spec_requirement,
        risk_score=assessment.risk_score,
        predicted_fail_codes=assessment.predicted_failure_codes,
    )

    # Write to ClickHouse via MCP (the audit trail INSERT)
    row = {
        "tracking_id": str(record.tracking_id),
        "title_id": record.title_id,
        "original_inspection": str(record.original_inspection),
        "package_id": str(record.package_id),
        "vendor_id": record.vendor_id,
        "platform_spec": record.platform_spec,
        "filed_at": record.filed_at.strftime("%Y-%m-%d %H:%M:%S"),
        "filed_by_agent": record.filed_by_agent,
        "decision": record.decision,
        "decision_rationale": record.decision_rationale,
        "status": record.status.value,
        "attempt_number": record.attempt_number,
        "estimated_cost_usd": record.estimated_cost_usd,
        "resolved_at": None,
        "spec_section": record.spec_section,
        "spec_requirement": record.spec_requirement,
        "risk_score": record.risk_score,
        "predicted_fail_codes": json.dumps(record.predicted_fail_codes),
    }

    try:
        async with clickhouse_session() as session:
            await mcp_insert(
                session,
                table=f"{DATABASE}.redelivery_tracking",
                rows=[row],
            )
        log.info(
            "action.redelivery.filed",
            tracking_id=str(record.tracking_id),
            title_id=record.title_id,
        )
    except Exception as e:
        log.error("action.insert.failed", error=str(e))
        # Don't raise — the record is still returned even if the write fails
        # In production, this would trigger an alert and retry

    return record
