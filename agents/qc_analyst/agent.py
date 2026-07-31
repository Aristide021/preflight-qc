"""
QC-Analyst Agent — entry point.

The differentiating agent: reasons over ClickHouse history to risk-score
an incoming delivery before it's ever submitted.

Usage (CLI — with --sample flag for development):
    uv run python agents/qc_analyst/agent.py --sample

Usage (programmatic):
    from agents.qc_analyst.agent import run_qc_analyst
    assessment = await run_qc_analyst(qc_result, classification)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from uuid import uuid4

import structlog
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

import google.adk as adk
from google import genai
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from agents.qc_analyst.prompts import ANALYST_QUERY_PROMPT, SYSTEM_PROMPT
from agents.qc_analyst.tools import (
    get_codec_error_clusters,
    get_redelivery_cost_estimate,
    get_risk_score_inputs,
    get_title_history,
    get_vendor_failure_rate,
)
from agents.shared.models import (
    HistoricalInsight,
    QCResult,
    RiskAssessment,
    SpecClassification,
)

log = structlog.get_logger(__name__)
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")


async def run_qc_analyst(
    qc_result: QCResult,
    classification: SpecClassification,
) -> RiskAssessment:
    """
    Run the QC-Analyst agent.

    Queries ClickHouse for historical context and returns a risk assessment
    with a 0.0–1.0 risk score backed by real query data.

    Args:
        qc_result: The incoming QC inspection result
        classification: The Spec-Reader's classification of the errors

    Returns:
        RiskAssessment with risk score, insights, and predicted failure codes
    """
    log.info(
        "qc_analyst.start",
        title_id=qc_result.title_id,
        vendor_id=qc_result.vendor_id,
        risk_will_be_computed=True,
    )

    # Run ClickHouse queries directly (tools)
    # In Phase 3 this becomes agent-driven with tool calls via ADK
    # For Phase 2 Checkpoint: run queries directly and pass results to Gemini
    vendor_data = await get_vendor_failure_rate(
        vendor_id=qc_result.vendor_id,
        platform_spec=qc_result.platform_spec,
    )
    codec_data = await get_codec_error_clusters(
        codec=qc_result.codec,
        hdr_format=qc_result.hdr_format,
        platform_spec=qc_result.platform_spec,
    )
    risk_inputs = await get_risk_score_inputs(
        vendor_id=qc_result.vendor_id,
        platform_spec=qc_result.platform_spec,
        codec=qc_result.codec,
        hdr_format=qc_result.hdr_format,
    )
    error_codes = [err.error_code for err in qc_result.errors]
    cost_data = await get_redelivery_cost_estimate(
        vendor_id=qc_result.vendor_id,
        error_codes=error_codes,
    )

    # Build the Gemini prompt with query results embedded
    delivery_json = json.dumps(qc_result.model_dump(mode="json"), indent=2, default=str)
    classifications_json = json.dumps(classification.model_dump(mode="json"), indent=2, default=str)

    full_prompt = f"""{ANALYST_QUERY_PROMPT.format(
        delivery_json=delivery_json,
        classifications_json=classifications_json,
    )}

ClickHouse query results (already fetched for you):

VENDOR FAILURE RATE (last 90 days):
{json.dumps(vendor_data, indent=2)}

CODEC/HDR ERROR CLUSTERS:
{json.dumps(codec_data, indent=2)}

RISK SCORE INPUTS (1-year history):
{json.dumps(risk_inputs, indent=2)}

COST ESTIMATE:
{json.dumps(cost_data, indent=2)}

Based on these ClickHouse results, compute the risk score and return a JSON
object matching the RiskAssessment schema. The risk_score must be derived
from the historical_fail_rate in the risk inputs. Cite which query drove
each insight in the insights array."""

    # Run Gemini to synthesize the risk assessment
    agent = LlmAgent(
        name="qc_analyst",
        model=MODEL,
        description="Reasons over ClickHouse history to risk-score incoming deliveries",
        instruction=SYSTEM_PROMPT,
    )

    session_service = adk.InMemorySessionService()
    session = await session_service.create_session(
        app_name="preflight_qc",
        user_id="qc_analyst_runner",
    )

    runner = adk.Runner(
        agent=agent,
        app_name="preflight_qc",
        session_service=session_service,
    )

    response_text = ""
    async for event in runner.run_async(
        user_id="qc_analyst_runner",
        session_id=session.id,
        new_message=adk.types.Content(
            role="user",
            parts=[adk.types.Part(text=full_prompt)],
        ),
    ):
        if hasattr(event, "content") and event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    response_text += part.text

    assessment = _parse_assessment(
        response_text=response_text,
        qc_result=qc_result,
        risk_inputs=risk_inputs,
        predicted_codes=risk_inputs.get("predicted_failure_codes", []),
        estimated_cost=cost_data.get("estimated_total_cost_usd", 0.0),
    )

    log.info(
        "qc_analyst.done",
        title_id=qc_result.title_id,
        risk_score=assessment.risk_score,
        risk_label=assessment.risk_label,
    )
    return assessment


def _parse_assessment(
    response_text: str,
    qc_result: QCResult,
    risk_inputs: dict,
    predicted_codes: list[str],
    estimated_cost: float,
) -> RiskAssessment:
    """Parse agent response into RiskAssessment, with data-driven fallback."""
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response_text, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        json_str = json_match.group(0) if json_match else "{}"

    try:
        data = json.loads(json_str)
        risk_score = float(data.get("risk_score", 0.5))
        insights = [HistoricalInsight(**i) for i in data.get("insights", [])]
        reasoning = data.get("agent_reasoning", response_text[:500])
    except Exception as e:
        log.error("qc_analyst.parse.error", error=str(e))
        # Fallback: derive risk score directly from the data
        ri = risk_inputs.get("risk_inputs", {})
        risk_score = float(ri.get("historical_fail_rate", 0.5))
        insights = []
        reasoning = f"Parse error — risk score derived from historical_fail_rate={risk_score}"

    ri = risk_inputs.get("risk_inputs", {})
    return RiskAssessment(
        inspection_id=qc_result.inspection_id,
        title_id=qc_result.title_id,
        vendor_id=qc_result.vendor_id,
        risk_score=min(1.0, max(0.0, risk_score)),
        vendor_historical_fail_rate=float(ri.get("historical_fail_rate", 0.0)),
        vendor_total_submissions=int(ri.get("historical_submissions", 0)),
        predicted_failure_codes=predicted_codes,
        avg_redelivery_attempts=float(ri.get("avg_redelivery_attempts", 0.0)),
        estimated_remediation_cost_usd=estimated_cost,
        insights=insights,
        agent_reasoning=reasoning,
    )


# ── CLI entry point ───────────────────────────────────────────────────────────

async def _cli_main() -> None:
    parser = argparse.ArgumentParser(description="Run QC-Analyst agent")
    parser.add_argument("--input", help="Path to QC result JSON file")
    parser.add_argument("--classification", help="Path to SpecClassification JSON file")
    parser.add_argument("--sample", action="store_true", help="Run with built-in sample data")
    args = parser.parse_args()

    if args.sample:
        # Checkpoint 2 gate: run with sample data to prove ClickHouse MCP works
        qc_result = QCResult(
            title_id="NFLX_100042",
            vendor_id="VND_ROUNDABOUT",
            platform_spec="netflix-imf-2.1",
            codec="ProRes",
            hdr_format="SDR",
            redelivery_attempt=0,
        )
        from agents.shared.models import (
            ClassifiedFailure,
            FailureSeverity,
            SpecClassification,
        )
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
    else:
        qc_result = QCResult.model_validate_json(Path(args.input).read_text())
        classification = SpecClassification.model_validate_json(
            Path(args.classification).read_text()
        )

    assessment = await run_qc_analyst(qc_result, classification)
    print("\n" + "=" * 60)
    print(f"  CHECKPOINT 2 — QC-Analyst Result")
    print("=" * 60)
    print(f"  Vendor         : {assessment.vendor_id}")
    print(f"  Risk Score     : {assessment.risk_score:.2f} ({assessment.risk_label.upper()})")
    print(f"  Historical FR  : {assessment.vendor_historical_fail_rate:.1%}")
    print(f"  Predicted Fail : {assessment.predicted_failure_codes}")
    print(f"  Est. Cost      : ${assessment.estimated_remediation_cost_usd:,.0f}")
    print(f"\n  Reasoning: {assessment.agent_reasoning[:300]}...")
    print("=" * 60)

    if assessment.risk_score > 0 or assessment.vendor_total_submissions > 0:
        print("\n✅ CHECKPOINT 2 GREEN — QC-Analyst queried ClickHouse Cloud and returned a risk score.")
    else:
        print("\n⚠️  Check ClickHouse connectivity — risk score is 0 and no submissions found.")


if __name__ == "__main__":
    asyncio.run(_cli_main())
