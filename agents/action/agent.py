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

from agents.action.adapters import DownstreamDispatcher
from agents.shared.clickhouse_mcp import quote_literal, write_client
from agents.shared.models import (
    AdapterResult,
    OrchestratorDecision,
    QCResult,
    RedeliveryFiledEvent,
    RedeliveryRecord,
    RedeliveryStatus,
    RiskAssessment,
)

log = structlog.get_logger(__name__)

DATABASE = os.environ.get("CLICKHOUSE_DATABASE", "preflight")
AGENT_VERSION = "orchestrator-v1"



class ClickHouseAuditWriter:
    """
    Primary system of record: writes the audit trail directly into ClickHouse
    redelivery_tracking table. Execution failure here is fatal.
    """

    async def record_redelivery(self, record: RedeliveryRecord) -> None:
        columns = (
            "tracking_id, title_id, original_inspection, package_id, vendor_id, "
            "platform_spec, filed_at, filed_by_agent, decision, decision_rationale, "
            "status, attempt_number, estimated_cost_usd, resolved_at, spec_section, "
            "spec_requirement, risk_score, predicted_fail_codes"
        )
        values = ", ".join(
            quote_literal(value)
            for value in (
                str(record.tracking_id),
                record.title_id,
                str(record.original_inspection),
                str(record.package_id),
                record.vendor_id,
                record.platform_spec,
                record.filed_at.strftime("%Y-%m-%d %H:%M:%S"),
                record.filed_by_agent,
                record.decision,
                record.decision_rationale,
                record.status.value,
                record.attempt_number,
                record.estimated_cost_usd,
                None,
                record.spec_section,
                record.spec_requirement,
                record.risk_score,
                json.dumps(record.predicted_fail_codes),
            )
        )
        sql = f"INSERT INTO {DATABASE}.redelivery_tracking ({columns}) VALUES ({values})"
        await write_client().execute(sql)


async def _file_and_dispatch(
    qc_result: QCResult,
    decision: OrchestratorDecision,
    assessment: RiskAssessment,
    dispatcher: DownstreamDispatcher | None = None,
    audit_writer: ClickHouseAuditWriter | None = None,
) -> tuple[RedeliveryRecord, list[AdapterResult]]:
    """
    Shared core implementation:
      1. Constructs the RedeliveryRecord.
      2. Commits the audit record to ClickHouse via ClickHouseAuditWriter (fatal if it fails).
      3. Constructs the RedeliveryFiledEvent once.
      4. Dispatches to downstream adapters exactly once.
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

    # 1. Primary Audit Record write to ClickHouse (fatal on failure)
    writer = audit_writer or ClickHouseAuditWriter()
    await writer.record_redelivery(record)

    log.info(
        "action.redelivery.filed",
        tracking_id=str(record.tracking_id),
        title_id=record.title_id,
    )

    # 2. Emit Domain Event & Dispatch Downstream Adapters (exactly once)
    event = RedeliveryFiledEvent(
        tracking_id=record.tracking_id,
        title_id=record.title_id,
        title_name=qc_result.title_name,
        vendor_id=record.vendor_id,
        platform_spec=record.platform_spec,
        decision=record.decision,
        decision_rationale=record.decision_rationale,
        blocking_findings=[err.model_dump(mode="json") for err in qc_result.errors],
        remediation_instructions=decision.remediation_instructions,
        estimated_cost_usd=record.estimated_cost_usd,
        risk_score=record.risk_score,
        release_window_impact=(
            f"Remediation cost est. ${record.estimated_cost_usd:,.2f}"
            if record.estimated_cost_usd > 0
            else None
        ),
        metadata={
            "package_id": str(record.package_id),
            "attempt_number": record.attempt_number,
            "spec_section": record.spec_section,
        },
    )

    active_dispatcher = dispatcher or DownstreamDispatcher()
    adapter_results = await active_dispatcher.dispatch(event)
    for res in adapter_results:
        log.info(
            "action.adapter_result",
            adapter=res.adapter_name,
            status=res.status.value,
            message=res.message,
            duration_ms=res.duration_ms,
        )

    return record, adapter_results


async def file_redelivery(
    qc_result: QCResult,
    decision: OrchestratorDecision,
    assessment: RiskAssessment,
) -> RedeliveryRecord:
    """
    File a redelivery record, commit audit trail to ClickHouse, and dispatch downstream adapters.
    Returns RedeliveryRecord for full backward compatibility with existing callers.
    """
    record, _ = await _file_and_dispatch(qc_result, decision, assessment)
    return record


async def file_redelivery_with_events(
    qc_result: QCResult,
    decision: OrchestratorDecision,
    assessment: RiskAssessment,
    dispatcher: DownstreamDispatcher | None = None,
) -> tuple[RedeliveryRecord, list[AdapterResult]]:
    """
    File a redelivery record, commit audit trail, and return both the record
    and the downstream adapter results. Guaranteed to dispatch downstream adapters exactly once.
    """
    return await _file_and_dispatch(
        qc_result=qc_result,
        decision=decision,
        assessment=assessment,
        dispatcher=dispatcher,
    )
