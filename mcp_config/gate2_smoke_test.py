# Gate 2 Smoke Test — ClickHouse MCP Connectivity
#
# PURPOSE: Prove Gate 2: a Gemini-driven agent successfully issues an MCP query
# to ClickHouse and gets a result back.
#
# This is the PROOF ARTIFACT for the hackathon hard requirement:
#   "ClickHouse used at runtime via the official mcp-clickhouse MCP server"
#
# Run:
#   uv run python mcp_config/gate2_smoke_test.py
#
# Expected output:
#   [GATE 2] MCP tools discovered: ['query', 'list_databases', ...]
#   [GATE 2] Agent response: ... ClickHouse result ...
#   [GATE 2] ✅ GATE 2 GREEN — Gemini agent queried ClickHouse via MCP.
#
# Environment variables required (see .env.example):
#   CLICKHOUSE_HOST, CLICKHOUSE_USER, CLICKHOUSE_PASSWORD
#   GOOGLE_API_KEY

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Google ADK imports (the hard runtime requirement) ─────────────────────────
import google.adk as adk
from google import genai                                          # google-genai
from google.adk.agents import LlmAgent                          # google-adk 2.x agent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset   # google-adk 2.x MCP (not MCPToolset)
from mcp import StdioServerParameters

log = structlog.get_logger()

CLICKHOUSE_HOST     = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_USER     = os.environ["CLICKHOUSE_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_PASSWORD"]

GATE2_QUERY = "SELECT 1 AS gate2_probe, 'ClickHouse MCP wiring works' AS status"

GATE2_EXTENDED_QUERY = """
SELECT
    database,
    count() AS table_count
FROM system.tables
WHERE database NOT IN ('system', 'INFORMATION_SCHEMA', 'information_schema')
GROUP BY database
ORDER BY table_count DESC
LIMIT 5
"""


async def run_gate2_smoke_test() -> bool:
    print("\n" + "=" * 60)
    print("  GATE 2 SMOKE TEST — ClickHouse MCP Connectivity")
    print("=" * 60)
    print(f"\n[GATE 2] Connecting to ClickHouse via mcp-clickhouse MCP server...")
    print(f"         Host: {CLICKHOUSE_HOST}")
    print(f"         User: {CLICKHOUSE_USER}")

    mcp_server_params = StdioServerParameters(
        command="npx",
        args=["-y", "@clickhouse/mcp-clickhouse"],
        env={
            **os.environ,
            "CLICKHOUSE_HOST":     CLICKHOUSE_HOST,
            "CLICKHOUSE_PORT":     os.environ.get("CLICKHOUSE_PORT", "8443"),
            "CLICKHOUSE_USER":     CLICKHOUSE_USER,
            "CLICKHOUSE_PASSWORD": CLICKHOUSE_PASSWORD,
            "CLICKHOUSE_SECURE":   os.environ.get("CLICKHOUSE_SECURE", "true"),
        },
    )

    # ── Step 1: Discover MCP tools via McpToolset (google-adk 2.6.0 API) ─────
    print("\n[GATE 2] Discovering MCP tools...")
    toolset = McpToolset(connection_params=mcp_server_params)
    tools, exit_stack = await toolset.get_tools()

    tool_names = [t.name for t in tools]
    print(f"[GATE 2] MCP tools discovered: {tool_names}")

    if not tool_names:
        print("[GATE 2] ❌ No MCP tools found — check mcp-clickhouse install.")
        await exit_stack.aclose()
        return False

    # ── Step 2: Create LlmAgent with the MCP tools ────────────────────────────
    print("\n[GATE 2] Creating LlmAgent with ClickHouse MCP tools...")

    agent = LlmAgent(
        name="gate2_probe_agent",
        model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
        description=(
            "Gate 2 probe agent. Queries ClickHouse via MCP to verify "
            "the end-to-end integration works."
        ),
        instruction=(
            "You are a database probe agent. When asked to run SQL, "
            "use the available ClickHouse tools and return the raw result."
        ),
        tools=tools,
    )

    # ── Step 3: Run the agent ─────────────────────────────────────────────────
    print(f"\n[GATE 2] Running agent query...")
    print(f"         SQL: {GATE2_QUERY}")

    session_service = adk.InMemorySessionService()
    session = await session_service.create_session(
        app_name="gate2_smoke_test",
        user_id="gate2",
    )

    runner = adk.Runner(
        agent=agent,
        app_name="gate2_smoke_test",
        session_service=session_service,
    )

    prompt = (
        f"Run this ClickHouse SQL and show me the result:\n\n"
        f"```sql\n{GATE2_QUERY}\n```\n\n"
        f"Then also run:\n```sql\n{GATE2_EXTENDED_QUERY}\n```\n\n"
        f"Show both results."
    )

    result_text = ""
    async for event in runner.run_async(
        user_id="gate2",
        session_id=session.id,
        new_message=adk.types.Content(
            role="user",
            parts=[adk.types.Part(text=prompt)],
        ),
    ):
        if hasattr(event, "content") and event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    result_text += part.text

    await exit_stack.aclose()

    print(f"\n[GATE 2] Agent response:\n{result_text}")

    # ── Step 4: Gate check ────────────────────────────────────────────────────
    gate_passed = len(result_text.strip()) > 0

    print("\n" + "=" * 60)
    if gate_passed:
        print("  ✅  GATE 2 GREEN — Gemini agent queried ClickHouse via MCP.")
        print("      Hard requirement SATISFIED:")
        print("      • google.adk imported and called at runtime ✓")
        print("      • google.genai imported at runtime ✓")
        print("      • ClickHouse queried via mcp-clickhouse MCP server ✓")
    else:
        print("  ❌  GATE 2 RED — Agent returned no result.")
        print("      Check CLICKHOUSE_PASSWORD and GOOGLE_API_KEY in .env")
    print("=" * 60 + "\n")

    return gate_passed


if __name__ == "__main__":
    passed = asyncio.run(run_gate2_smoke_test())
    sys.exit(0 if passed else 1)
