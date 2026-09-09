"""
Integration test: full agent loop — Gate 4.

A non-compliant QC result goes in.
A tracked redelivery record must appear in ClickHouse on the other side.
No human in the middle.

Run:
    uv run pytest tests/integration/test_full_loop.py -v -m integration

Gate 4 is GREEN when this test passes against ClickHouse Cloud.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

from agents.action.agent import file_redelivery
from agents.orchestrator.agent import run_orchestrator
from agents.qc_analyst.agent import run_qc_analyst
from agents.shared.clickhouse_mcp import run_query
from agents.shared.models import QCError, QCResult
from agents.spec_reader.agent import run_spec_reader

DATABASE = os.environ.get("CLICKHOUSE_DATABASE", "preflight")

# ── Test fixtures ─────────────────────────────────────────────────────────────

SAMPLE_QC_RESULT = QCResult(
    title_id="NFLX_TEST_001",
    title_name="The Test Film",
    vendor_id="VND_ROUNDABOUT",
    platform_spec="netflix-imf-2.1",
    codec="ProRes",
    hdr_format="SDR",
    audio_config="5.1",
    redelivery_attempt=0,
    errors=[
        QCError(
            error_code="IMF_AUDIO_LOUDNESS_ERROR",
            error_category="audio",
            error_message="Integrated loudness -31.2 LKFS exceeds maximum -24.0 LKFS",
            stage="auto-qc",
        ),
        QCError(
            error_code="IMF_CPL_ERROR",
            error_category="structural",
            error_message="Missing ApplicationIdentification element in CPL",
            stage="photon",
        ),
    ],
)


# ── Gate 4 integration test ───────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.gate
@pytest.mark.asyncio
async def test_full_agent_loop_closes() -> None:
    """
    GATE 4: The complete agent loop runs end-to-end without human intervention.

    Input:  A non-compliant QC result (2 blocking errors)
    Output: A RedeliveryRecord written to ClickHouse redelivery_tracking table

    Steps:
    1. Spec-Reader classifies the failures with spec citations
    2. QC-Analyst queries ClickHouse history and returns a risk assessment
    3. Orchestrator synthesizes both and makes a REDELIVER decision
    4. Action agent files the redelivery and writes to ClickHouse
    5. We verify the record exists in ClickHouse (Gate 4 green condition)
    """
    qc_result = SAMPLE_QC_RESULT

    # Step 1: Spec-Reader
    classification = await run_spec_reader(qc_result)

    assert classification is not None, "Spec-Reader returned None"
    assert len(classification.failures) > 0, "No failures classified"
    assert classification.has_blocking_failures, (
        f"Expected blocking failures for loudness + CPL errors, got: "
        f"{[f.severity for f in classification.failures]}"
    )

    # Verify spec citations exist
    for failure in classification.failures:
        assert failure.spec_section, (
            f"Missing spec_section for {failure.error_code}"
        )
        assert failure.spec_requirement, (
            f"Missing spec_requirement for {failure.error_code}"
        )

    # Step 2: QC-Analyst
    assessment = await run_qc_analyst(qc_result)

    assert assessment is not None, "QC-Analyst returned None"
    assert 0.0 <= assessment.risk_score <= 1.0, (
        f"Risk score out of range: {assessment.risk_score}"
    )
    assert assessment.risk_label in ("low", "medium", "high", "critical"), (
        f"Invalid risk label: {assessment.risk_label}"
    )

    # Step 3: Orchestrator
    decision = await run_orchestrator(qc_result, classification, assessment)

    assert decision is not None, "Orchestrator returned None"
    assert decision.decision.value == "redeliver", (
        f"Expected REDELIVER decision for blocking failures, got: {decision.decision}"
    )
    assert decision.decision_rationale, "Missing decision rationale"

    # Step 4: Action agent — file the redelivery
    record = await file_redelivery(qc_result, decision, assessment)

    assert record is not None, "Action agent returned None"
    assert record.tracking_id is not None
    tracking_id = str(record.tracking_id)

    # Step 5: GATE 4 — verify the record exists in ClickHouse
    rows = await run_query(
        f"""
        SELECT tracking_id, title_id, decision, status, risk_score
        FROM {DATABASE}.redelivery_tracking
        WHERE tracking_id = '{tracking_id}'
        LIMIT 1
        """
    )

    assert len(rows) == 1, (
        f"GATE 4 FAIL: RedeliveryRecord {tracking_id} not found in ClickHouse. "
        f"The loop did not close."
    )
    assert rows[0]["decision"] == "redeliver"
    assert rows[0]["title_id"] == qc_result.title_id

    print(f"\n✅ GATE 4 GREEN — Full loop closed:")
    print(f"   Title:       {qc_result.title_id}")
    print(f"   Decision:    {decision.decision.value.upper()}")
    print(f"   Risk Score:  {assessment.risk_score:.2f} ({assessment.risk_label})")
    print(f"   Tracking ID: {tracking_id}")
    print(f"   ClickHouse:  redelivery_tracking row confirmed ✓")


# ── Checkpoint 2 test (no ClickHouse write required) ─────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_qc_analyst_returns_risk_score() -> None:
    """
    Checkpoint 2: QC-Analyst hits ClickHouse Cloud and returns a risk score
    with a historical citation. No write required.
    """
    from agents.shared.models import ClassifiedFailure, FailureSeverity, SpecClassification

    qc_result = SAMPLE_QC_RESULT
    classification = SpecClassification(
        inspection_id=qc_result.inspection_id,
        title_id=qc_result.title_id,
        vendor_id=qc_result.vendor_id,
        platform_spec=qc_result.platform_spec,
        failures=[
            ClassifiedFailure(
                error_code="IMF_AUDIO_LOUDNESS_ERROR",
                error_category="audio",
                error_message="Integrated loudness -31.2 LKFS",
                severity=FailureSeverity.BLOCKING,
                spec_section="Section 3.4 Loudness",
                spec_requirement="-24 LKFS ±1 LU mandatory",
            )
        ],
    )

    assessment = await run_qc_analyst(qc_result)

    assert assessment.risk_score >= 0.0
    assert assessment.risk_label in ("low", "medium", "high", "critical")

    print(f"\n✅ Checkpoint 2: risk_score={assessment.risk_score:.2f} ({assessment.risk_label})")
    if assessment.vendor_total_submissions > 0:
        print(f"   Historical submissions found: {assessment.vendor_total_submissions}")
        print(f"   Historical fail rate: {assessment.vendor_historical_fail_rate:.1%}")
