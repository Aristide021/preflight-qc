# Gate 2 Smoke Test — ClickHouse MCP Connectivity
#
# PURPOSE: Prove Gate 2 — a Gemini-driven agent issues an MCP query to
# ClickHouse and gets a real result back.
#
# This is the PROOF ARTIFACT for the hackathon hard requirement:
#   "ClickHouse used at runtime via the official mcp-clickhouse MCP server"
#
# Run:
#   uv run python mcp_config/gate2_smoke_test.py
#
# Environment variables required (see .env.example):
#   CLICKHOUSE_HOST, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD
#   GOOGLE_API_KEY
#
# NOTE ON THE MCP SERVER
# The official mcp-clickhouse server is a PYTHON package, declared in
# pyproject.toml and installed into .venv/bin/mcp-clickhouse. An earlier
# version of this file launched `npx -y @clickhouse/mcp-clickhouse`; that npm
# package does not exist (registry returns 404), so the server never started
# and the MCP session closed immediately.

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Google ADK imports (the hard runtime requirement) ────────────────────────
# E402: these deliberately follow load_dotenv() — the ADK/genai clients read
# GOOGLE_API_KEY at import time, so .env must be loaded first.
# ruff: noqa: E402
import google.adk as adk  # noqa: F401  — asserts the SDK is importable at runtime
from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types
from mcp import StdioServerParameters

log = structlog.get_logger()

CLICKHOUSE_HOST = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_USER = os.environ["CLICKHOUSE_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_PASSWORD"]

APP_NAME = "gate2_smoke_test"
USER_ID = "gate2"

# A value the agent can only produce by actually reaching ClickHouse.
GATE2_SENTINEL = "clickhouse-mcp-wiring-works"
GATE2_QUERY = f"SELECT 1 AS gate2_probe, '{GATE2_SENTINEL}' AS status"

GATE2_EXTENDED_QUERY = """
SELECT
    database,
    count() AS table_count
FROM system.tables
WHERE database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema')
GROUP BY database
ORDER BY table_count DESC
LIMIT 5
""".strip()


# ADK's MCP request timeout defaults to 5s. The mcp-clickhouse server builds a
# fresh ClickHouse client per tool call — TLS handshake to ClickHouse Cloud
# alone takes ~2s cold, and an idled service takes longer to wake — so the
# default reliably times out on the first call even against a healthy cluster.
MCP_TIMEOUT_SECONDS = 60.0


def _resolve_mcp_server() -> StdioConnectionParams:
    """Launch the mcp-clickhouse console script from this project's venv."""
    server_bin = Path(__file__).parent.parent / ".venv" / "bin" / "mcp-clickhouse"
    command = str(server_bin) if server_bin.exists() else shutil.which("mcp-clickhouse")

    if not command:
        raise SystemExit(
            "ERROR: mcp-clickhouse executable not found.\n"
            "       It ships with the Python package declared in pyproject.toml.\n"
            "       Run: uv sync"
        )

    return StdioConnectionParams(
        server_params=StdioServerParameters(
            command=command,
            args=[],
            env={
                **os.environ,
                "CLICKHOUSE_HOST": CLICKHOUSE_HOST,
                "CLICKHOUSE_PORT": os.environ.get("CLICKHOUSE_PORT", "8443"),
                "CLICKHOUSE_USER": CLICKHOUSE_USER,
                "CLICKHOUSE_PASSWORD": CLICKHOUSE_PASSWORD,
                "CLICKHOUSE_SECURE": os.environ.get("CLICKHOUSE_SECURE", "true"),
            },
        ),
        timeout=MCP_TIMEOUT_SECONDS,
    )


async def run_gate2_smoke_test() -> bool:
    print("\n" + "=" * 60)
    print("  GATE 2 SMOKE TEST — ClickHouse MCP Connectivity")
    print("=" * 60)

    mcp_server_params = _resolve_mcp_server()

    print("\n[GATE 2] Launching mcp-clickhouse MCP server...")
    print(f"         Server: {mcp_server_params.server_params.command}")
    print(f"         Host  : {CLICKHOUSE_HOST}")
    print(f"         User  : {CLICKHOUSE_USER}")

    toolset = McpToolset(connection_params=mcp_server_params)

    try:
        # ── Step 1: Discover MCP tools ──────────────────────────────────────
        # ADK 2.x get_tools() returns List[BaseTool]. (The 1.x
        # MCPToolset.from_server() returned a (tools, exit_stack) tuple; the
        # toolset now owns its own lifecycle and is closed via .close().)
        print("\n[GATE 2] Discovering MCP tools...")
        tools = await toolset.get_tools()
        tool_names = [t.name for t in tools]
        print(f"[GATE 2] MCP tools discovered ({len(tool_names)}): {tool_names}")

        if not tool_names:
            print("[GATE 2] FAIL — MCP server exposed no tools.")
            return False

        # ── Step 2: Agent with the MCP toolset ──────────────────────────────
        print("\n[GATE 2] Creating LlmAgent with ClickHouse MCP tools...")
        agent = LlmAgent(
            name="gate2_probe_agent",
            model=os.environ.get("GEMINI_MODEL", "gemini-flash-latest"),
            description=(
                "Gate 2 probe agent. Queries ClickHouse via MCP to verify "
                "the end-to-end integration works."
            ),
            instruction=(
                "You are a database probe agent. When asked to run SQL, use the "
                "available ClickHouse tools and report the raw result verbatim. "
                "Never invent results: if a tool call fails, say so explicitly."
            ),
            tools=[toolset],
        )

        # ── Step 3: Run it ──────────────────────────────────────────────────
        print("\n[GATE 2] Running agent query...")
        print(f"         SQL: {GATE2_QUERY}")

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
        )
        runner = Runner(
            agent=agent,
            app_name=APP_NAME,
            session_service=session_service,
        )

        prompt = (
            f"Run this ClickHouse SQL and show the result:\n\n"
            f"```sql\n{GATE2_QUERY}\n```\n\n"
            f"Then run:\n\n```sql\n{GATE2_EXTENDED_QUERY}\n```\n\n"
            f"Show both results."
        )

        result_text = ""
        tool_calls: list[str] = []
        model_error: str | None = None

        try:
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
                        result_text += part.text
                    if getattr(part, "function_call", None):
                        tool_calls.append(part.function_call.name)
        except Exception as exc:  # noqa: BLE001 — report, don't mask
            # A model-side failure (quota, auth, model name) must not look like
            # a ClickHouse failure. Capture it and let the gate report below
            # attribute the failure to the right half of the integration.
            model_error = f"{type(exc).__name__}: {exc}"
            print(f"\n[GATE 2] Model call FAILED — {model_error.splitlines()[0][:200]}")

        print(f"\n[GATE 2] MCP tool calls made: {tool_calls or 'NONE'}")
        if result_text:
            print(f"\n[GATE 2] Agent response:\n{result_text}")

        # Independent of the model: prove the MCP path itself reaches ClickHouse.
        # This isolates "ClickHouse wiring works" from "the LLM could run".
        print("\n[GATE 2] Direct MCP probe (no model in the loop)...")
        direct_ok = False
        query_tool = next((t for t in tools if t.name == "run_query"), None)
        if query_tool is not None:
            probe = await query_tool.run_async(args={"query": GATE2_QUERY}, tool_context=None)
            direct_ok = GATE2_SENTINEL in str(probe)
            print(f"[GATE 2] Direct MCP result: {str(probe)[:220]}")

    finally:
        await toolset.close()

    # ── Step 4: Gate check ──────────────────────────────────────────────────
    # Checked against evidence the agent could only obtain from ClickHouse.
    # The previous version passed on `len(result_text) > 0`, which would go
    # green on the agent replying "I could not connect."
    clickhouse_checks = [
        ("MCP server started and exposed tools", bool(tool_names)),
        ("MCP round-trip returned the sentinel from ClickHouse", direct_ok),
    ]
    model_checks = [
        ("model call completed", model_error is None),
        ("agent invoked an MCP tool", bool(tool_calls)),
        ("agent reported the sentinel", GATE2_SENTINEL in result_text),
    ]

    print("\n" + "=" * 60)
    print("  ClickHouse / MCP half")
    for label, ok in clickhouse_checks:
        print(f"    {'PASS' if ok else 'FAIL'}  {label}")
    print("\n  Gemini / ADK half")
    for label, ok in model_checks:
        print(f"    {'PASS' if ok else 'FAIL'}  {label}")

    clickhouse_ok = all(ok for _, ok in clickhouse_checks)
    model_ok = all(ok for _, ok in model_checks)

    print()
    if clickhouse_ok and model_ok:
        print("  GATE 2 GREEN — Gemini agent queried ClickHouse via MCP.")
        print("    - google.adk imported and called at runtime")
        print("    - google.genai types used at runtime")
        print("    - ClickHouse queried via the mcp-clickhouse MCP server")
    elif clickhouse_ok:
        print("  GATE 2 PARTIAL — ClickHouse via MCP works; the model call did not.")
        print("    The hard requirement's ClickHouse half is proven.")
        if model_error:
            print(f"    Model error: {model_error.splitlines()[0][:160]}")
        print("    If this is a 429 with 'limit: 0', the API key's project has no")
        print("    Gemini quota allocated — enable billing or switch to Vertex AI.")
    else:
        print("  GATE 2 RED — the ClickHouse/MCP path itself failed.")
        print("    Check CLICKHOUSE_* in .env and that the service is not idled.")
    print("=" * 60 + "\n")

    return clickhouse_ok and model_ok


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run_gate2_smoke_test()) else 1)
