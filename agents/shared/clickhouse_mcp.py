"""
Shared ClickHouse MCP client for all agents.

Every agent reaches ClickHouse through the official `mcp-clickhouse` MCP server
launched here, which is what satisfies the hard requirement that ClickHouse is
used at runtime via MCP.

WHAT WAS WRONG BEFORE

  - The server was launched as `npx -y @clickhouse/mcp-clickhouse`. That npm
    package does not exist (registry 404). The official server is the Python
    package in pyproject.toml. Nothing in this module had ever run.
  - It called `session.call_tool("query", ...)`. The tool is named `run_query`.
  - `mcp_insert` called a tool named `insert`, which the server does not expose.
    The Action agent's write path was fiction.
  - `mcp_query`'s `params` did `sql.replace("{k}", str(v))` while the docstring
    claimed ClickHouse `{name:Type}` binding. It was string splicing, so callers
    that believed they were binding were interpolating.
  - A fresh MCP server subprocess was spawned per query — two per tool call.

TWO PRIVILEGE LEVELS

The server is read-only unless CLICKHOUSE_ALLOW_WRITE_ACCESS=true, so this
module exposes two cached clients: `readonly_client()` for the analytics agents
and `write_client()` for the Action agent alone. Read agents therefore cannot
mutate the tracking table even if their prompt is manipulated into trying.

ON QUERY PARAMETERS

The MCP tool signature is `run_query(query: str)` — there is no parameter
channel, so values cannot be bound server-side. Callers must pass user- or
model-supplied values through `quote_literal()` / `quote_identifier()`. Do not
f-string a raw value into SQL.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import structlog
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters

log = structlog.get_logger(__name__)

# mcp-clickhouse builds a fresh ClickHouse client per call; the TLS handshake to
# ClickHouse Cloud alone is ~2s, so ADK's 5s default times out on a healthy
# cluster.
MCP_TIMEOUT_SECONDS = float(os.environ.get("MCP_TIMEOUT_SECONDS", "60"))

_QUERY_TOOL = "run_query"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


# ── Safe SQL construction ────────────────────────────────────────────────────


def quote_literal(value: Any) -> str:
    """
    Render a Python value as a ClickHouse SQL literal.

    Required because the MCP tool accepts only a finished SQL string. Values
    reaching these tools come from an LLM, so raw interpolation is both an
    injection path and a correctness bug the moment a value contains a quote.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def quote_identifier(name: str) -> str:
    """Validate a table/column identifier. Rejects anything not a plain dotted name."""
    if not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


# ── Client ───────────────────────────────────────────────────────────────────


def _resolve_server_binary() -> str:
    venv_bin = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "mcp-clickhouse"
    command = str(venv_bin) if venv_bin.exists() else shutil.which("mcp-clickhouse")
    if not command:
        raise RuntimeError(
            "mcp-clickhouse executable not found. It ships with the Python package "
            "declared in pyproject.toml — run `uv sync`."
        )
    return command


class ClickHouseMCP:
    """A lazily-connected, reusable MCP toolset bound to one privilege level."""

    def __init__(self, *, allow_writes: bool = False) -> None:
        self.allow_writes = allow_writes
        self._toolset: McpToolset | None = None
        self._tools: dict[str, BaseTool] | None = None

    def _connection_params(self) -> StdioConnectionParams:
        required = ["CLICKHOUSE_HOST", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"]
        missing = [v for v in required if not os.environ.get(v)]
        if missing:
            raise RuntimeError(
                f"Missing ClickHouse environment variables: {missing}. "
                "Copy .env.example to .env and fill in the values."
            )

        # Determine database credentials based on privilege level.
        # In production, true security isolation is enforced at the database level
        # via distinct ClickHouse roles/users (see infra/clickhouse/rbac.sql).
        if self.allow_writes:
            db_user = os.environ.get("CLICKHOUSE_WRITE_USER") or os.environ["CLICKHOUSE_USER"]
            db_pass = os.environ.get("CLICKHOUSE_WRITE_PASSWORD") or os.environ["CLICKHOUSE_PASSWORD"]
        else:
            db_user = os.environ.get("CLICKHOUSE_READONLY_USER") or os.environ["CLICKHOUSE_USER"]
            db_pass = os.environ.get("CLICKHOUSE_READONLY_PASSWORD") or os.environ["CLICKHOUSE_PASSWORD"]

        env = {
            **os.environ,
            "CLICKHOUSE_HOST": os.environ["CLICKHOUSE_HOST"],
            "CLICKHOUSE_PORT": os.environ.get("CLICKHOUSE_PORT", "8443"),
            "CLICKHOUSE_USER": db_user,
            "CLICKHOUSE_PASSWORD": db_pass,
            "CLICKHOUSE_DATABASE": os.environ.get("CLICKHOUSE_DATABASE", "preflight"),
            "CLICKHOUSE_SECURE": os.environ.get("CLICKHOUSE_SECURE", "true"),
            # Explicit either way: never inherit write access from the ambient
            # environment for a client that asked to be read-only.
            "CLICKHOUSE_ALLOW_WRITE_ACCESS": "true" if self.allow_writes else "false",
            "CLICKHOUSE_ALLOW_DROP": "false",
        }

        return StdioConnectionParams(
            server_params=StdioServerParameters(
                command=_resolve_server_binary(), args=[], env=env
            ),
            timeout=MCP_TIMEOUT_SECONDS,
        )

    async def toolset(self) -> McpToolset:
        if self._toolset is None:
            self._toolset = McpToolset(connection_params=self._connection_params())
            log.info(
                "clickhouse_mcp.connect",
                host=os.environ.get("CLICKHOUSE_HOST"),
                allow_writes=self.allow_writes,
            )
        return self._toolset

    async def tools(self) -> dict[str, BaseTool]:
        if self._tools is None:
            toolset = await self.toolset()
            self._tools = {t.name: t for t in await toolset.get_tools()}
            log.debug("clickhouse_mcp.tools", tools=list(self._tools))
        return self._tools

    async def query(self, sql: str) -> list[dict[str, Any]]:
        """Run SQL through MCP and return rows as dicts."""
        tools = await self.tools()
        if _QUERY_TOOL not in tools:
            raise RuntimeError(
                f"MCP server exposes {sorted(tools)}; expected a '{_QUERY_TOOL}' tool."
            )

        log.debug("clickhouse_mcp.query", sql=" ".join(sql.split())[:160])
        result = await tools[_QUERY_TOOL].run_async(args={"query": sql}, tool_context=None)
        return _rows_from_result(result, sql)

    async def execute(self, sql: str) -> None:
        """Run a write statement. Only valid on a write-enabled client."""
        if not self.allow_writes:
            raise PermissionError(
                "This ClickHouse MCP client is read-only. Use write_client() for "
                "statements that mutate data."
            )
        await self.query(sql)

    async def close(self) -> None:
        if self._toolset is not None:
            await self._toolset.close()
            self._toolset, self._tools = None, None


def _rows_from_result(result: Any, sql: str) -> list[dict[str, Any]]:
    """
    Normalise the MCP tool result into row dicts.

    mcp-clickhouse returns {"columns": [...], "rows": [[...]]} as text inside the
    MCP content envelope. Errors arrive as isError=True with the ClickHouse
    exception as text — surfaced here rather than returned as an empty result,
    so a failed query cannot read downstream as "no history found".
    """
    payload = result if isinstance(result, dict) else getattr(result, "model_dump", lambda: {})()

    if payload.get("isError"):
        text = _first_text(payload)
        raise RuntimeError(f"ClickHouse MCP query failed: {text}\nSQL: {' '.join(sql.split())[:300]}")

    text = _first_text(payload)
    if not text:
        return []

    try:
        # ClickHouse emits bare NaN / Infinity for 0/0 and similar. Python's
        # json.loads accepts them, but they are not valid JSON, so they survive
        # all the way into the Gemini request body and the API rejects the call
        # with "Invalid JSON payload received. Unexpected token." Map them to
        # null here so a divide-by-zero in one cell cannot fail the whole run.
        data = json.loads(text, parse_constant=lambda _: None)
    except json.JSONDecodeError:
        return [{"raw": text}]

    if isinstance(data, dict) and "columns" in data and "rows" in data:
        columns = data["columns"]
        return [dict(zip(columns, row, strict=False)) for row in data["rows"]]
    if isinstance(data, list):
        return data
    return [data]


def _first_text(payload: dict[str, Any]) -> str:
    structured = payload.get("structuredContent") or {}
    if isinstance(structured, dict) and isinstance(structured.get("result"), str):
        return structured["result"]
    for item in payload.get("content") or []:
        if isinstance(item, dict) and item.get("text"):
            return item["text"]
    return ""


# ── Cached module-level clients ──────────────────────────────────────────────

_readonly: ClickHouseMCP | None = None
_write: ClickHouseMCP | None = None


def readonly_client() -> ClickHouseMCP:
    """Analytics client. Cannot mutate data — used by Spec-Reader and QC-Analyst."""
    global _readonly
    if _readonly is None:
        _readonly = ClickHouseMCP(allow_writes=False)
    return _readonly


def write_client() -> ClickHouseMCP:
    """Write-enabled client. Reserved for the Action agent's tracking writes."""
    global _write
    if _write is None:
        _write = ClickHouseMCP(allow_writes=True)
    return _write


async def close_all() -> None:
    """Shut down both cached clients. Call at process exit."""
    for client in (_readonly, _write):
        if client is not None:
            await client.close()


async def run_query(sql: str) -> list[dict[str, Any]]:
    """Convenience read-only query against the cached analytics client."""
    return await readonly_client().query(sql)
