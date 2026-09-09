"""
QC-Analyst Agent — the history reasoner.

Queries 50M inspection records in ClickHouse via MCP to risk-score an incoming
delivery against what has actually happened before. This is the differentiator:
a single-package checker structurally cannot produce this output.

Usage (CLI):
    uv run python -m agents.qc_analyst.agent --input data/sample_qc_result.json

WHAT CHANGED AND WHY

  - The previous version called the tool functions itself in Python, pasted the
    JSON into a prompt, and asked the model to summarise it. The model had no
    tools and made no decisions, so "the agent queries ClickHouse" described a
    script with a language model bolted on the end. The tools are now registered
    with the agent and it chooses which history to pull.

  - The risk score was produced by the model and recovered with a regex, with a
    fallback that fabricated a score when parsing failed. A risk number that
    drives a redelivery decision should be reproducible and auditable, so it is
    now computed in Python from the values the tools actually returned. The
    model supplies the narrative, not the arithmetic.

  - adk.InMemorySessionService / adk.Runner / adk.types were wrong import paths
    (see google.adk.sessions, google.adk.runners, google.genai.types).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import structlog
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

# Prefer Vertex AI when no Gemini API key is configured. The project ID may be
# overridden through the environment for another deployment.
if not os.environ.get("GOOGLE_API_KEY"):
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "gen-lang-client-0768345181")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

# ruff: noqa: E402
import google.adk as adk  # noqa: F401  — runtime proof the SDK is loaded
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types

from agents.qc_analyst.prompts import SYSTEM_PROMPT
from agents.qc_analyst.tools import (
    get_codec_error_clusters,
    get_redelivery_cost_estimate,
    get_risk_score_inputs,
    get_title_history,
    get_vendor_failure_rate,
)
from agents.shared.clickhouse_mcp import close_all
from agents.shared.models import HistoricalInsight, QCResult, RiskAssessment

log = structlog.get_logger(__name__)

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
APP_NAME = "preflight_qc"
USER_ID = "qc_analyst_runner"

ANALYST_TOOLS = [
    FunctionTool(get_vendor_failure_rate),
    FunctionTool(get_risk_score_inputs),
    FunctionTool(get_codec_error_clusters),
    FunctionTool(get_redelivery_cost_estimate),
    FunctionTool(get_title_history),
]


# Below this many historical submissions, the combination is not a sample worth
# scoring against — it is an unproven workflow.
MIN_SAMPLE_FOR_HISTORY = 100

# What "this vendor is as bad as it gets" looks like, as a multiple of baseline.
# 4x the corpus-wide failure rate saturates the historical component.
SATURATION_RATIO = 4.0


def _score_from_history(
    risk_inputs: dict[str, Any],
    attempt: int,
    baseline_fail_rate: float,
    vendor_fallback_rate: float = 0.0,
) -> tuple[float, str]:
    """
    Deterministic risk score from observed history. Returns (score, basis).

    Kept out of the model on purpose: this number decides whether a redelivery
    is filed, so it has to be reproducible and explainable from the data. The
    weighting is a stated assumption, not a measured one.

    Two things this must not get wrong:

    - Absolute failure rates are not interpretable. A 8% rate is unremarkable
      against a 8% baseline and alarming against a 1% one, so the historical
      component is the ratio to the corpus baseline, not the raw rate. Scoring
      the raw rate put every delivery in this corpus under 0.10 — permanently
      "low", and useless as a signal.

    - No history is not low risk. A vendor/spec/codec combination with no track
      record is an unproven workflow, and the first version scored it 0.0 and
      labelled it "low" — the most confident possible answer from the least
      evidence. It now falls back to the vendor's overall rate and carries an
      explicit unproven-combination penalty.
    """
    submissions = int(risk_inputs.get("historical_submissions") or 0)
    fail_rate = float(risk_inputs.get("historical_fail_rate") or 0.0)
    avg_attempts = float(risk_inputs.get("avg_redelivery_attempts") or 0.0)
    baseline = baseline_fail_rate or 0.0

    if submissions < MIN_SAMPLE_FOR_HISTORY:
        # Unproven combination: lean on the vendor's broader record and add a
        # penalty for the missing track record rather than reporting safety.
        effective_rate = vendor_fallback_rate or baseline
        basis = (
            f"no meaningful history for this exact combination "
            f"({submissions} submissions); scored from the vendor's overall rate "
            f"with an unproven-workflow penalty"
        )
        unproven_penalty = 0.30
    else:
        effective_rate = fail_rate
        basis = f"{submissions:,} historical submissions for this combination"
        unproven_penalty = 0.0

    if baseline > 0:
        ratio = effective_rate / baseline
        historical = min(1.0, ratio / SATURATION_RATIO)
    else:
        historical = min(1.0, effective_rate)

    score = historical * 0.6
    score += unproven_penalty
    # Chains that historically needed rework carry more risk than the raw rate.
    score += min(0.15, avg_attempts * 0.10)
    # A package already on its second or third attempt is empirically worse.
    score += min(0.20, attempt * 0.10)

    return round(max(0.0, min(1.0, score)), 3), basis


async def run_qc_analyst(qc_result: QCResult) -> RiskAssessment:
    """Run the QC-Analyst against a QC result and return a risk assessment."""
    log.info(
        "qc_analyst.start",
        title_id=qc_result.title_id,
        vendor_id=qc_result.vendor_id,
        attempt=qc_result.redelivery_attempt,
    )

    error_codes = [e.error_code for e in qc_result.errors]

    agent = LlmAgent(
        name="qc_analyst",
        model=MODEL,
        description="Reasons over ClickHouse delivery history to risk-score a delivery",
        instruction=SYSTEM_PROMPT,
        tools=ANALYST_TOOLS,
    )

    session_service = InMemorySessionService()
    session = await session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)

    prompt = f"""Assess the historical risk of this incoming delivery.

DELIVERY
  title_id      : {qc_result.title_id}
  vendor_id     : {qc_result.vendor_id}
  platform_spec : {qc_result.platform_spec}
  codec         : {qc_result.codec}
  hdr_format    : {qc_result.hdr_format}
  attempt       : {qc_result.redelivery_attempt}
  error codes   : {json.dumps(error_codes)}

Use the ClickHouse tools to gather history. At minimum call
get_risk_score_inputs and get_vendor_failure_rate; call the others when they
would sharpen the picture.

Then write a short analysis covering:
  - what this vendor's record on this spec and codec actually looks like
  - which error codes history says are most likely to appear or recur
  - what the failures have cost, and what this one is likely to cost
  - anything a delivery manager should act on before resubmitting

Cite concrete numbers from the tool results. Do not state a risk score — that
is computed separately from the same data.
"""

    tool_results: dict[str, Any] = {}
    narrative = ""

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        content = getattr(event, "content", None)
        if not content or not getattr(content, "parts", None):
            continue
        for part in content.parts:
            if getattr(part, "text", None):
                narrative += part.text
            response = getattr(part, "function_response", None)
            if response is not None:
                tool_results[response.name] = response.response

    if not tool_results:
        raise RuntimeError(
            "QC-Analyst called no ClickHouse tools — the assessment would have no "
            "historical basis. Check model availability and tool registration."
        )

    log.info("qc_analyst.tools_called", tools=sorted(tool_results))

    # ADK wraps a tool's return value; unwrap when present.
    def _unwrap(name: str) -> dict[str, Any]:
        raw = tool_results.get(name) or {}
        if isinstance(raw, dict) and set(raw) == {"result"}:
            raw = raw["result"]
        return raw if isinstance(raw, dict) else {}

    risk_payload = _unwrap("get_risk_score_inputs")
    risk_inputs = risk_payload.get("risk_inputs", {}) or {}
    vendor_payload = _unwrap("get_vendor_failure_rate")
    vendor_summary = vendor_payload.get("summary", {}) or {}
    cost_payload = _unwrap("get_redelivery_cost_estimate")

    risk_score, risk_basis = _score_from_history(
        risk_inputs,
        qc_result.redelivery_attempt,
        baseline_fail_rate=float(risk_payload.get("baseline_fail_rate") or 0.0),
        vendor_fallback_rate=float(vendor_summary.get("overall_fail_rate") or 0.0),
    )

    insights: list[HistoricalInsight] = []
    if vendor_summary:
        insights.append(
            HistoricalInsight(
                query_type="vendor_fail_rate",
                finding=(
                    f"{qc_result.vendor_id} submitted "
                    f"{vendor_summary.get('total_submissions', 0):,} packages in the last "
                    f"{vendor_payload.get('lookback_days', 90)} days with an overall failure "
                    f"rate of {vendor_summary.get('overall_fail_rate', 0)}."
                ),
                data=vendor_summary,
                clickhouse_query="get_vendor_failure_rate",
            )
        )
    if risk_inputs:
        insights.append(
            HistoricalInsight(
                query_type="risk_inputs",
                finding=(
                    f"On {qc_result.platform_spec} with {qc_result.codec}, history shows a "
                    f"{risk_inputs.get('historical_fail_rate', 0)} failure rate across "
                    f"{risk_inputs.get('historical_submissions', 0):,} submissions, averaging "
                    f"${float(risk_inputs.get('avg_cost_per_failure_usd') or 0):,.0f} per failure."
                ),
                data=risk_inputs,
                clickhouse_query="get_risk_score_inputs",
            )
        )

    assessment = RiskAssessment(
        inspection_id=qc_result.inspection_id,
        title_id=qc_result.title_id,
        vendor_id=qc_result.vendor_id,
        risk_score=risk_score,
        vendor_historical_fail_rate=float(risk_inputs.get("historical_fail_rate") or 0.0),
        vendor_total_submissions=int(risk_inputs.get("historical_submissions") or 0),
        predicted_failure_codes=risk_payload.get("predicted_failure_codes", []) or [],
        avg_redelivery_attempts=float(risk_inputs.get("avg_redelivery_attempts") or 0.0),
        estimated_remediation_cost_usd=float(
            cost_payload.get("estimated_total_cost_usd") or 0.0
        ),
        insights=insights,
        agent_reasoning=f"Risk basis: {risk_basis}.\n\n{narrative.strip()}",
    )

    log.info(
        "qc_analyst.done",
        title_id=qc_result.title_id,
        risk_score=assessment.risk_score,
        risk_label=assessment.risk_label,
        risk_basis=risk_basis,
        tools_called=len(tool_results),
    )
    return assessment


# ── CLI entry point ──────────────────────────────────────────────────────────


async def _cli_main() -> None:
    parser = argparse.ArgumentParser(description="Run QC-Analyst on a QC result")
    parser.add_argument("--input", required=True, help="Path to QC result JSON file")
    parser.add_argument("--output", help="Path to write RiskAssessment JSON output")
    args = parser.parse_args()

    try:
        qc_result = QCResult.model_validate_json(Path(args.input).read_text())
        assessment = await run_qc_analyst(qc_result)
        output = assessment.model_dump_json(indent=2)
        if args.output:
            Path(args.output).write_text(output)
            print(f"Risk assessment written to {args.output}")
        else:
            print(output)
    finally:
        await close_all()


if __name__ == "__main__":
    asyncio.run(_cli_main())
