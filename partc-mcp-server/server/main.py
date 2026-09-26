"""Custom MCP server — the Part C Reporting Genie Agent, exposed as a tool.

This hosts the **``PartC-Reporting-Genie``** Genie Agent behind a custom MCP
server, following the Databricks pattern:
https://learn.microsoft.com/en-us/azure/databricks/agents/mcp-tools/custom-mcp

Why this exists: ``PartC-Reporting-Genie`` is **not certified/favorited**, so
**Genie One will not discover or route to it**. But the Genie *Conversation API*
can reach any Genie space you have permission on, by its space ID. So we wrap
that agent as a single MCP tool — ``ask_partc_reporting`` — and any MCP client
(the orchestrator app, the AI Playground, Claude Desktop, a Supervisor agent)
can call it as an external tool, entirely outside Genie One.

Transport: streamable HTTP (FastMCP serves it at ``/mcp``). Deployed as a
Databricks App named ``mcp-*``; auth is handled by the Databricks SDK — the app
service principal when deployed, your CLI profile when run locally.
"""

from __future__ import annotations

import os
import time

from databricks.sdk import WorkspaceClient
from mcp.server.fastmcp import FastMCP

# The Genie Agent this server fronts. Genie One can't see it (not certified),
# but the Conversation API reaches it directly by this space ID. Overridable so
# the same server can front a different agent without a code change.
SPACE_ID = os.environ.get("GENIE_SPACE_ID", "01f14c0eea5a133c88b42273a0a9b4e1")

# Poll cadence for the Conversation API (matches what we validated via the CLI).
MAX_POLLS = int(os.environ.get("GENIE_MAX_POLLS", "90"))
POLL_SECS = float(os.environ.get("GENIE_POLL_SECS", "3"))

_TERMINAL_OK = "COMPLETED"
_TERMINAL_BAD = {"FAILED", "CANCELLED"}

mcp = FastMCP("partc-reporting-genie", stateless_http=True)


def _base(space_id: str) -> str:
    return f"/api/2.0/genie/spaces/{space_id}"


def _fetch_table(w: WorkspaceClient, space_id: str, conv: str, msg_id: str, attachment_id: str | None) -> str:
    """Render an attachment's query result as a markdown table (best-effort)."""
    if not attachment_id:
        return ""
    try:
        res = w.api_client.do(
            "GET",
            f"{_base(space_id)}/conversations/{conv}/messages/{msg_id}/attachments/{attachment_id}/query-result",
        )
    except Exception:
        return ""
    sr = (res or {}).get("statement_response") or {}
    cols = (((sr.get("manifest") or {}).get("schema") or {}).get("columns")) or []
    rows = ((sr.get("result") or {}).get("data_array")) or []
    if not cols or not rows:
        return ""
    names = [c.get("name", "") for c in cols]
    header = "| " + " | ".join(names) + " |"
    sep = "| " + " | ".join("---" for _ in names) + " |"
    body = ["| " + " | ".join("" if v is None else str(v) for v in row) + " |" for row in rows[:50]]
    table = "\n".join([header, sep, *body])
    if len(rows) > 50:
        table += f"\n\n_Showing first 50 of {len(rows)} rows._"
    return table


def _render_answer(w: WorkspaceClient, space_id: str, conv: str, msg_id: str, msg: dict) -> str:
    """Assemble a completed message's attachments into a markdown answer."""
    parts: list[str] = []
    sql_blocks: list[str] = []
    for att in msg.get("attachments") or []:
        text = (att.get("text") or {}).get("content")
        if text:
            parts.append(text.strip())
        query = att.get("query")
        if query:
            desc = (query.get("description") or "").strip()
            if desc:
                parts.append(desc)
            sql = (query.get("query") or "").strip()
            if sql:
                sql_blocks.append(sql)
            table = _fetch_table(w, space_id, conv, msg_id, att.get("attachment_id"))
            if table:
                parts.append(table)
    if sql_blocks:
        joined = "\n\n".join(f"```sql\n{s}\n```" for s in sql_blocks)
        parts.append(f"**Generated SQL**\n\n{joined}")
    return "\n\n".join(p for p in parts if p) or "_(The agent returned no answer text.)_"


@mcp.tool()
def ask_partc_reporting(question: str) -> str:
    """Answer a question about Medicare Part C CMS compliance reporting.

    Use this tool for Part C compliance reporting: timeliness metrics, TI
    accuracy, data quality scores, publish-readiness status, validation
    failures, per-contract compliance rates, and CY2027 regulatory changes.
    Returns a markdown answer including the generated SQL and a result table.
    """
    w = WorkspaceClient()
    started = w.api_client.do(
        "POST", f"{_base(SPACE_ID)}/start-conversation", body={"content": question}
    )
    conv = started.get("conversation_id") or (started.get("conversation") or {}).get("id")
    msg_id = started.get("message_id") or (started.get("message") or {}).get("id")
    if not conv or not msg_id:
        return f"⚠️ Could not start a Part C Reporting conversation: {started}"

    for _ in range(MAX_POLLS):
        msg = w.api_client.do(
            "GET", f"{_base(SPACE_ID)}/conversations/{conv}/messages/{msg_id}"
        )
        status = (msg.get("status") or "").upper()
        if status == _TERMINAL_OK:
            return _render_answer(w, SPACE_ID, conv, msg_id, msg)
        if status in _TERMINAL_BAD:
            err = (msg.get("error") or {}).get("error") or status
            return f"⚠️ Part C Reporting agent response {status.lower()}: {err}"
        time.sleep(POLL_SECS)
    return "⚠️ Timed out waiting for the Part C Reporting agent."


def main() -> None:
    """Entry point run by app.yaml (`uv run python -m server.main`).

    Databricks Apps assigns the port via ``$DATABRICKS_APP_PORT`` and requires
    binding ``0.0.0.0``; locally we fall back to 8000. FastMCP then serves the
    streamable HTTP transport at ``/mcp``.
    """
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
