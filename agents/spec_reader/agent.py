"""
Spec-Reader Agent — entry point.

Reads the live delivery spec and classifies each QC failure as
blocking / cosmetic / advisory with a spec citation.

Usage (CLI):
    uv run python agents/spec_reader/agent.py --input data/sample_qc_result.json

Usage (programmatic):
    from agents.spec_reader.agent import run_spec_reader
    classification = await run_spec_reader(qc_result)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

import google.adk as adk
from google import genai
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from agents.shared.models import (
    ClassifiedFailure,
    FailureSeverity,
    QCResult,
    SpecClassification,
)
from agents.spec_reader.prompts import FEW_SHOT_EXAMPLES, SPEC_FETCH_PROMPT, SYSTEM_PROMPT
from agents.spec_reader.spec_loader import fetch_spec

log = structlog.get_logger(__name__)

MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")


async def run_spec_reader(qc_result: QCResult) -> SpecClassification:
    """
    Run the Spec-Reader agent against a QC result.

    Fetches the live delivery spec, classifies each failure, returns
    a SpecClassification with spec citations for every error.

    Args:
        qc_result: The QC inspection result to classify

    Returns:
        SpecClassification with blocking/cosmetic/advisory labels + spec citations
    """
    log.info(
        "spec_reader.start",
        title_id=qc_result.title_id,
        vendor_id=qc_result.vendor_id,
        error_count=len(qc_result.errors),
    )

    # Fetch the live spec
    spec_doc = await fetch_spec(qc_result.platform_spec)

    # Build the classification prompt
    errors_json = json.dumps([e.model_dump() for e in qc_result.errors], indent=2)
    prompt = SPEC_FETCH_PROMPT.format(
        spec_url=spec_doc.url,
        errors_json=errors_json,
    )

    # Inject the spec content directly (saves a tool call for the agent)
    full_prompt = f"""{prompt}

SPEC CONTENT (fetched from {spec_doc.url}):
---
{spec_doc.content[:8000]}
---

Now classify each error according to the spec content above.
Return valid JSON matching the SpecClassification schema.
"""

    # Create and run the Gemini agent
    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))

    agent = LlmAgent(
        name="spec_reader",
        model=MODEL,
        description="Classifies QC failures against the live delivery spec",
        instruction=SYSTEM_PROMPT,
    )

    session_service = adk.InMemorySessionService()
    session = await session_service.create_session(
        app_name="preflight_qc",
        user_id="spec_reader_runner",
    )

    runner = adk.Runner(
        agent=agent,
        app_name="preflight_qc",
        session_service=session_service,
    )

    response_text = ""
    async for event in runner.run_async(
        user_id="spec_reader_runner",
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

    # Parse the agent's JSON response into a SpecClassification
    classification = _parse_classification(
        response_text=response_text,
        qc_result=qc_result,
        spec_url=spec_doc.url,
    )

    log.info(
        "spec_reader.done",
        title_id=qc_result.title_id,
        blocking=classification.blocking_count,
        cosmetic=classification.cosmetic_count,
        advisory=classification.advisory_count,
    )
    return classification


def _parse_classification(
    response_text: str,
    qc_result: QCResult,
    spec_url: str,
) -> SpecClassification:
    """Parse the agent's JSON response into a SpecClassification."""
    import re

    # Extract JSON from the response (agent may wrap it in markdown code fences)
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response_text, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # Try to find raw JSON
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        json_str = json_match.group(0) if json_match else "{}"

    try:
        data = json.loads(json_str)
        failures = [ClassifiedFailure(**f) for f in data.get("failures", [])]
    except Exception as e:
        log.error("spec_reader.parse.error", error=str(e), response=response_text[:200])
        # Fall back: classify all errors as blocking (conservative)
        failures = [
            ClassifiedFailure(
                error_code=err.error_code,
                error_category=err.error_category,
                error_message=err.error_message,
                severity=FailureSeverity.BLOCKING,
                spec_section="PARSE ERROR — classified conservatively as blocking",
                spec_requirement="Could not parse spec citation from agent response",
            )
            for err in qc_result.errors
        ]

    return SpecClassification(
        inspection_id=qc_result.inspection_id,
        title_id=qc_result.title_id,
        vendor_id=qc_result.vendor_id,
        platform_spec=qc_result.platform_spec,
        failures=failures,
        spec_url=spec_url,
        agent_reasoning=data.get("agent_reasoning", response_text[:500]) if "data" in dir() else "",
    )


# ── CLI entry point ───────────────────────────────────────────────────────────

async def _cli_main() -> None:
    parser = argparse.ArgumentParser(description="Run Spec-Reader agent on a QC result")
    parser.add_argument("--input", required=True, help="Path to QC result JSON file")
    parser.add_argument("--output", help="Path to write SpecClassification JSON output")
    args = parser.parse_args()

    qc_result = QCResult.model_validate_json(Path(args.input).read_text())
    classification = await run_spec_reader(qc_result)

    output = classification.model_dump_json(indent=2)
    if args.output:
        Path(args.output).write_text(output)
        print(f"✓ Classification written to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    asyncio.run(_cli_main())
