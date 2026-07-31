"""
Shared ClickHouse MCP client for all agents.

Provides a connection singleton and typed query wrappers.
All agents import from here — one place to configure, one place to change.

In local dev: connects to localhost ClickHouse (no cloud credits burned).
In production: connects to ClickHouse Cloud via the MCP server, with
credentials sourced from Secret Manager via environment variables.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import structlog
from mcp import ClientSession, StdioServerParameters
from tenacity import retry, stop_after_attempt, wait_exponential

log = structlog.get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# MCP server parameters
# ─────────────────────────────────────────────────────────────────────────────

def _mcp_server_params() -> StdioServerParameters:
    """
    Build StdioServerParameters for the mcp-clickhouse server.
    Credentials are always sourced from environment (Secret Manager in prod).
    """
    required_vars = ["CLICKHOUSE_HOST", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"]
    missing = [v for v in required_vars if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing ClickHouse environment variables: {missing}\n"
            f"Copy .env.example → .env and fill in the values."
        )

    return StdioServerParameters(
        command="npx",
        args=["-y", "@clickhouse/mcp-clickhouse"],
        env={
            **os.environ,
            "CLICKHOUSE_HOST":     os.environ["CLICKHOUSE_HOST"],
            "CLICKHOUSE_PORT":     os.environ.get("CLICKHOUSE_PORT", "8443"),
            "CLICKHOUSE_USER":     os.environ["CLICKHOUSE_USER"],
            "CLICKHOUSE_PASSWORD": os.environ["CLICKHOUSE_PASSWORD"],
            "CLICKHOUSE_DATABASE": os.environ.get("CLICKHOUSE_DATABASE", "preflight"),
            "CLICKHOUSE_SECURE":   os.environ.get("CLICKHOUSE_SECURE", "true"),
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# MCP session context manager
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def clickhouse_session() -> AsyncIterator[ClientSession]:
    """
    Async context manager that yields an active MCP ClientSession connected
    to ClickHouse via mcp-clickhouse.

    Usage:
        async with clickhouse_session() as session:
            result = await mcp_query(session, "SELECT 1")
    """
    from mcp import stdio_client

    server_params = _mcp_server_params()
    log.debug("opening.clickhouse.mcp.session", host=os.environ.get("CLICKHOUSE_HOST"))

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            log.debug("clickhouse.mcp.session.ready")
            yield session


# ─────────────────────────────────────────────────────────────────────────────
# Query helpers
# ─────────────────────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)
async def mcp_query(
    session: ClientSession,
    sql: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """
    Execute a SELECT query via MCP and return results as a list of dicts.
    Retries up to 3 times with exponential backoff on transient errors.

    Args:
        session: Active MCP ClientSession (from clickhouse_session())
        sql: ClickHouse SQL query string
        params: Optional query parameters (substituted as {name:Type} placeholders)

    Returns:
        List of row dicts
    """
    log.debug("mcp.query", sql=sql[:100])

    # Substitute params if provided
    if params:
        for key, value in params.items():
            # Simple substitution — for production use parameterized queries
            sql = sql.replace(f"{{{key}}}", str(value))

    result = await session.call_tool("query", {"query": sql})

    if result.isError:
        raise RuntimeError(f"ClickHouse MCP query failed: {result.content}")

    # mcp-clickhouse returns content as text (JSON or TSV depending on format)
    import json
    content = result.content[0].text if result.content else "[]"

    try:
        rows = json.loads(content)
        if isinstance(rows, dict) and "data" in rows:
            return rows["data"]
        if isinstance(rows, list):
            return rows
        return [rows]
    except (json.JSONDecodeError, IndexError):
        # Fall back: return raw text wrapped in a dict
        return [{"raw": content}]


async def mcp_insert(
    session: ClientSession,
    table: str,
    rows: list[dict[str, Any]],
) -> bool:
    """
    Insert rows into a ClickHouse table via MCP.
    Used by the Action agent to write redelivery records.

    Args:
        session: Active MCP ClientSession
        table: Fully qualified table name (e.g. preflight.redelivery_tracking)
        rows: List of row dicts matching the table schema

    Returns:
        True on success
    """
    import json
    log.debug("mcp.insert", table=table, rows=len(rows))

    result = await session.call_tool(
        "insert",
        {"table": table, "data": json.dumps(rows)},
    )

    if result.isError:
        raise RuntimeError(f"ClickHouse MCP insert failed: {result.content}")

    log.info("mcp.insert.success", table=table, rows=len(rows))
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: run a one-shot query without managing the session
# ─────────────────────────────────────────────────────────────────────────────

async def run_query(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """
    Open a session, run one query, close. Convenience for simple queries.
    For multiple queries, use clickhouse_session() directly.
    """
    async with clickhouse_session() as session:
        return await mcp_query(session, sql, params)
