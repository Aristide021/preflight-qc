"""
Orchestrator Agent — coordinates the full agent loop.

Flow:
  QCResult → Spec-Reader → QC-Analyst → Orchestrator → Action Agent

The Orchestrator synthesizes the Spec-Reader's classification and the
QC-Analyst's risk assessment to make a filing decision, then hands off
to the Action agent to execute and close the loop.

This is where "checker" becomes "compliance system."
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

# Prefer Vertex AI when no Gemini API key is configured. The project ID may be
# overridden through the environment for another deployment.
if not os.environ.get("GOOGLE_API_KEY"):
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "gen-lang-client-0768345181")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

import asyncio

import google.adk as adk  # noqa: F401 — runtime proof the SDK is loaded
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agents.shared.models import (
    OrchestratorDecision,
    QCResult,
    RedeliveryDecision,
    RiskAssessment,
    SpecClassification,
)

log = structlog.get_logger(__name__)
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SYSTEM_PROMPT = """You are the Orchestrator agent for a media delivery QC compliance system.

Your role: synthesize the Spec-Reader's failure classification and the QC-Analyst's
historical risk assessment, then make a filing decision.

Decision options:
  REDELIVER   — blocking failures exist; redelivery is mandatory
  WAIVE       — only cosmetic/advisory issues; can be accepted with a waiver
  ESCALATE    — ambiguous or novel failure pattern; escalate for human review
  INVESTIGATE — conflicting signals (e.g. low historical risk but blocking error)

Decision rules (in priority order):
  1. If any BLOCKING failure exists → REDELIVER (mandatory, no override)
  2. If risk_score > 0.75 and only cosmetic failures → ESCALATE
  3. If all failures are COSMETIC or ADVISORY → WAIVE
  4. If risk_score > 0.5 and no prior history → INVESTIGATE

Always include:
  - The spec section cited (from Spec-Reader output)
  - The exact spec requirement quoted
  - Specific remediation instructions for the vendor
  - The risk context from QC-Analyst

Output: JSON matching the OrchestratorDecision schema."""


async def run_orchestrator(
    qc_result: QCResult,
    classification: SpecClassification,
    assessment: RiskAssessment,
) -> OrchestratorDecision:
    """
    Run the Orchestrator to synthesize classifications + risk assessment
    into a concrete filing decision.
    """
    log.info("orchestrator.start", title_id=qc_result.title_id)

    prompt = f"""Make a filing decision for this delivery.

QC Result:
{json.dumps(qc_result.model_dump(mode="json"), indent=2, default=str)}

Spec-Reader Classification:
{json.dumps(classification.model_dump(mode="json"), indent=2, default=str)}

QC-Analyst Risk Assessment:
{json.dumps(assessment.model_dump(mode="json"), indent=2, default=str)}

Apply the decision rules from your system prompt and return a JSON object
matching the OrchestratorDecision schema. Include specific remediation
instructions that the vendor can act on immediately."""

    agent = LlmAgent(
        name="orchestrator",
        model=MODEL,
        description="Synthesizes QC findings and risk assessment into a filing decision",
        instruction=SYSTEM_PROMPT,
    )

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="preflight_qc",
        user_id="orchestrator_runner",
    )
    runner = Runner(
        agent=agent,
        app_name="preflight_qc",
        session_service=session_service,
    )

    response_text = ""
    async for event in runner.run_async(
        user_id="orchestrator_runner",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part(text=prompt)],
        ),
    ):
        if hasattr(event, "content") and event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    response_text += part.text

    return _parse_decision(response_text, qc_result, classification, assessment)


def _parse_decision(
    response_text: str,
    qc_result: QCResult,
    classification: SpecClassification,
    assessment: RiskAssessment,
) -> OrchestratorDecision:
    import re

    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response_text, re.DOTALL)
    json_str = json_match.group(1) if json_match else re.search(r"\{.*\}", response_text, re.DOTALL)
    if hasattr(json_str, "group"):
        json_str = json_str.group(0)

    try:
        data = json.loads(json_str) if isinstance(json_str, str) else {}
        decision = RedeliveryDecision(data.get("decision", "redeliver").lower())
        rationale = data.get("decision_rationale", response_text[:500])
        spec_section = data.get("spec_section", "")
        spec_req = data.get("spec_requirement", "")
        remediation = data.get("remediation_instructions", "")
    except Exception:
        # Conservative fallback: blocking failures → redeliver
        decision = (
            RedeliveryDecision.REDELIVER
            if classification.has_blocking_failures
            else RedeliveryDecision.WAIVE
        )
        rationale = f"Parse error — conservative fallback based on blocking_count={classification.blocking_count}"
        spec_section = classification.failures[0].spec_section if classification.failures else ""
        spec_req = classification.failures[0].spec_requirement if classification.failures else ""
        remediation = " | ".join(
            f.remediation_hint for f in classification.failures if f.remediation_hint
        )

    return OrchestratorDecision(
        inspection_id=qc_result.inspection_id,
        title_id=qc_result.title_id,
        vendor_id=qc_result.vendor_id,
        decision=decision,
        decision_rationale=rationale,
        spec_section=spec_section,
        spec_requirement=spec_req,
        risk_score=assessment.risk_score,
        estimated_cost_usd=assessment.estimated_remediation_cost_usd,
        remediation_instructions=remediation,
    )


async def run_full_pipeline(qc_result: QCResult) -> dict[str, Any]:
    """
    Coordinate the end-to-end multi-agent loop:
      1. Concurrently run Spec-Reader and QC-Analyst on incoming QCResult
      2. Synthesize with Orchestrator to make a compliance decision
      3. Conditionally execute Action Agent only if decision is REDELIVER
    """
    from agents.action.agent import file_redelivery
    from agents.qc_analyst.agent import run_qc_analyst
    from agents.spec_reader.agent import run_spec_reader

    log.info("orchestrator.pipeline.start", title_id=qc_result.title_id)

    # Step 1: Concurrently run Spec-Reader and QC-Analyst
    classification, assessment = await asyncio.gather(
        run_spec_reader(qc_result),
        run_qc_analyst(qc_result),
    )

    # Step 2: Orchestrator synthesizes decision
    decision = await run_orchestrator(qc_result, classification, assessment)

    # Step 3: Conditionally file redelivery if REDELIVER
    tracking_record = None
    if decision.decision == RedeliveryDecision.REDELIVER:
        try:
            tracking_record = await file_redelivery(qc_result, decision, assessment)
        except Exception as exc:
            log.error("orchestrator.pipeline.action_failed", error=str(exc))
            raise RuntimeError(
                f"Action Agent failed to write redelivery tracking record: {exc}"
            ) from exc

    return {
        "qc_result": qc_result,
        "classification": classification,
        "assessment": assessment,
        "decision": decision,
        "tracking_record": tracking_record,
    }


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="PreFlight QC Orchestrator CLI")
    parser.add_argument("--input", required=True, help="Path to QC result JSON file")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"Error: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    qc_result = QCResult.model_validate(payload)
    print(f"Loaded QCResult: title={qc_result.title_id}, errors={len(qc_result.errors)}")

    result = asyncio.run(run_full_pipeline(qc_result))

    print("\n" + "=" * 60)
    print(f"ORCHESTRATOR DECISION: {result['decision'].decision.value.upper()}")
    print("=" * 60)
    print(f"Rationale: {result['decision'].decision_rationale}")
    print(f"Risk score: {result['assessment'].risk_score} ({result['assessment'].risk_label})")
    print(f"Blocking failures: {result['classification'].blocking_count}")
    print(f"Spec cited: {result['decision'].spec_section} - {result['decision'].spec_requirement}")
    print(f"Remediation: {result['decision'].remediation_instructions}")
    if result.get("tracking_record"):
        print(f"Tracking ID filed: {result['tracking_record'].tracking_id}")
    else:
        print("No redelivery record filed (decision was not REDELIVER).")


if __name__ == "__main__":
    main()
