"""
Spec-Reader Agent — entry point.

Reads the live delivery spec and classifies each QC failure as
blocking / cosmetic / advisory with a spec citation.

Usage (CLI):
    uv run python agents/spec_reader/agent.py --input data/sample_qc_result.json

Usage (programmatic):
    from agents.spec_reader.agent import run_spec_reader
    classification = await run_spec_reader(qc_result)

WHAT CHANGED AND WHY
  - adk.InMemorySessionService / adk.types do not exist. They live in
    google.adk.sessions and google.genai.types. The module could not run.
  - A genai.Client was constructed and never used — it existed only so the
    "google-genai called at runtime" claim would appear true in a grep. The
    real runtime call path is LlmAgent -> Runner, which is exercised below.
  - The agent asked for JSON in prose and recovered it with a regex, falling
    back to marking every error blocking when parsing failed. That fallback is
    indistinguishable in the output from a genuine all-blocking verdict. ADK
    supports output_schema, so the model is now constrained to the schema and a
    parse failure surfaces as an error rather than a plausible-looking verdict.
  - Ungrounded runs are no longer silent: if the spec could not be fetched, the
    classification records it (see SpecClassification.spec_version_used).
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

# Prefer Vertex AI when no Gemini API key is configured. The project ID may be
# overridden through the environment for another deployment.
if not os.environ.get("GOOGLE_API_KEY"):
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "TRUE")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "gen-lang-client-0768345181")
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

# E402: imports follow load_dotenv because the ADK/genai clients read
# GOOGLE_API_KEY at import time.
# ruff: noqa: E402
import google.adk as adk  # noqa: F401  — runtime proof the SDK is loaded
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, Field

from agents.shared.models import (
    ClassifiedFailure,
    QCResult,
    SpecClassification,
)
from agents.spec_reader.prompts import SYSTEM_PROMPT
from agents.spec_reader.spec_loader import fetch_spec

log = structlog.get_logger(__name__)

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
APP_NAME = "preflight_qc"
USER_ID = "spec_reader_runner"

# How much spec text to put in front of the model. The fetched bundle is ~57k
# characters across four documents; gemini-flash handles that comfortably and
# truncating it is what previously fed the model page furniture instead of
# requirements.
MAX_SPEC_CHARS = int(os.environ.get("SPEC_MAX_CHARS", "60000"))


class SpecReaderOutput(BaseModel):
    """
    What the model is asked to produce.

    Deliberately narrower than SpecClassification: identifiers, counts and
    timestamps are assembled in code afterwards. The model classifies; it does
    not get to invent an inspection_id.
    """

    failures: list[ClassifiedFailure] = Field(
        default_factory=list,
        description="One entry per input error, classified against the spec",
    )
    agent_reasoning: str = Field(
        default="",
        description="Brief rationale for the classifications, citing spec sections",
    )


def _build_prompt(qc_result: QCResult, spec_text: str, grounded: bool) -> str:
    errors_json = json.dumps([e.model_dump() for e in qc_result.errors], indent=2)

    grounding_note = (
        "The spec text below was fetched live from the platform's published "
        "documentation. Cite it directly."
        if grounded
        else "WARNING: the live spec could not be fetched and the text below is a "
        "built-in fallback. Say so in your reasoning and treat every "
        "classification as unverified."
    )

    return f"""Classify each QC error below against the delivery specification.

{grounding_note}

DELIVERY CONTEXT
  title_id      : {qc_result.title_id}
  vendor_id     : {qc_result.vendor_id}
  platform_spec : {qc_result.platform_spec}
  package_type  : {qc_result.package_type}
  codec         : {qc_result.codec}
  hdr_format    : {qc_result.hdr_format}
  audio_config  : {qc_result.audio_config}
  attempt       : {qc_result.redelivery_attempt}

QC ERRORS TO CLASSIFY
{errors_json}

DELIVERY SPECIFICATION
---
{spec_text[:MAX_SPEC_CHARS]}
---

Classify every error. For each one quote the specific spec language you relied
on in spec_requirement, and name the document/section in spec_section. If the
spec does not address an error, say so in spec_requirement rather than
inventing a requirement, and classify it advisory.
"""


async def run_spec_reader(qc_result: QCResult) -> SpecClassification:
    """
    Run the Spec-Reader agent against a QC result.

    Fetches the live delivery spec, classifies each failure, and returns a
    SpecClassification with spec citations for every error.
    """
    log.info(
        "spec_reader.start",
        title_id=qc_result.title_id,
        vendor_id=qc_result.vendor_id,
        error_count=len(qc_result.errors),
    )

    spec_doc = await fetch_spec(qc_result.platform_spec)
    if not spec_doc.is_grounded:
        log.warning("spec_reader.ungrounded", platform_spec=qc_result.platform_spec)

    agent = LlmAgent(
        name="spec_reader",
        model=MODEL,
        description="Classifies QC failures against the live delivery spec",
        instruction=SYSTEM_PROMPT,
        output_schema=SpecReaderOutput,
        output_key="classification",
    )

    session_service = InMemorySessionService()
    session = await session_service.create_session(app_name=APP_NAME, user_id=USER_ID)
    runner = Runner(agent=agent, app_name=APP_NAME, session_service=session_service)

    prompt = _build_prompt(qc_result, spec_doc.content, spec_doc.is_grounded)

    async for _ in runner.run_async(
        user_id=USER_ID,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
    ):
        pass

    # output_schema routes the validated object into session state under
    # output_key, so there is nothing to parse out of the response text.
    final = await session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session.id
    )
    raw = (final.state or {}).get("classification")

    if raw is None:
        raise RuntimeError(
            "Spec-Reader produced no structured output. The model returned nothing "
            "matching SpecReaderOutput; check model availability and quota."
        )

    parsed = SpecReaderOutput.model_validate(raw) if isinstance(raw, dict) else raw

    spec_label = (
        f"{qc_result.platform_spec} via {', '.join(spec_doc.sources)}"
        if spec_doc.is_grounded
        else f"{qc_result.platform_spec} (UNGROUNDED — live spec fetch failed)"
    )

    classification = SpecClassification(
        inspection_id=qc_result.inspection_id,
        title_id=qc_result.title_id,
        vendor_id=qc_result.vendor_id,
        platform_spec=qc_result.platform_spec,
        failures=parsed.failures,
        spec_version_used=spec_label,
        spec_url=spec_doc.url,
        agent_reasoning=parsed.agent_reasoning,
    )

    log.info(
        "spec_reader.done",
        title_id=qc_result.title_id,
        grounded=spec_doc.is_grounded,
        blocking=classification.blocking_count,
        cosmetic=classification.cosmetic_count,
        advisory=classification.advisory_count,
    )
    return classification


# ── CLI entry point ──────────────────────────────────────────────────────────


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
        print(f"Classification written to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    asyncio.run(_cli_main())
