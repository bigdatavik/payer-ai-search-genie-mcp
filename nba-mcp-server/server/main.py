"""Custom MCP server — the NBA Payer Genie agent, exposed as a tool.

Hosts the **``NBA Payer Genie - Member 360 & Next Best Action``** Genie Agent
(in workspace **fevm-humana-nba-genie**, "workspace B") behind a custom MCP
server, following the Databricks pattern:
https://learn.microsoft.com/en-us/azure/databricks/agents/mcp-tools/custom-mcp

The tool wraps the Genie Conversation API for this space. Once deployed as a
Databricks App (``mcp-*``) in workspace B, its ``/mcp`` URL can be registered as
an **MCP Service** in another workspace ("workspace A") for cross-workspace,
Unity-Gateway-governed access.

Transport: streamable HTTP (FastMCP serves it at ``/mcp``). Auth is handled by
the Databricks SDK — the app service principal when deployed, your CLI profile
when run locally.
"""

from __future__ import annotations

import os
import time

from databricks.sdk import WorkspaceClient
from mcp.server.fastmcp import FastMCP

# The NBA Payer Genie space in workspace B. Overridable via env so the same
# server image can front a different agent without a code change.
SPACE_ID = os.environ.get("GENIE_SPACE_ID", "01f1a2271a4618a8be35471ddcfb87f6")

MAX_POLLS = int(os.environ.get("GENIE_MAX_POLLS", "90"))
POLL_SECS = float(os.environ.get("GENIE_POLL_SECS", "3"))

_TERMINAL_OK = "COMPLETED"
_TERMINAL_BAD = {"FAILED", "CANCELLED"}

mcp = FastMCP("nba-payer-genie", stateless_http=True)


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
def ask_nba_payer_genie(question: str) -> str:
    """Answer a payer Member 360 / Next Best Action (NBA) question.

    Use this tool for member-population analytics and next-best-action guidance:
    member 360 profiles, risk and care gaps, engagement/outreach, and recommended
    next best actions for members. Returns a markdown answer including the
    generated SQL and a result table.
    """
    w = WorkspaceClient()
    started = w.api_client.do(
        "POST", f"{_base(SPACE_ID)}/start-conversation", body={"content": question}
    )
    conv = started.get("conversation_id") or (started.get("conversation") or {}).get("id")
    msg_id = started.get("message_id") or (started.get("message") or {}).get("id")
    if not conv or not msg_id:
        return f"⚠️ Could not start an NBA Payer Genie conversation: {started}"

    for _ in range(MAX_POLLS):
        msg = w.api_client.do(
            "GET", f"{_base(SPACE_ID)}/conversations/{conv}/messages/{msg_id}"
        )
        status = (msg.get("status") or "").upper()
        if status == _TERMINAL_OK:
            return _render_answer(w, SPACE_ID, conv, msg_id, msg)
        if status in _TERMINAL_BAD:
            err = (msg.get("error") or {}).get("error") or status
            return f"⚠️ NBA Payer Genie response {status.lower()}: {err}"
        time.sleep(POLL_SECS)
    return "⚠️ Timed out waiting for the NBA Payer Genie agent."


def main() -> None:
    """Entry point run by app.yaml (`uv run python -m server.main`)."""
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = int(os.environ.get("DATABRICKS_APP_PORT", "8000"))
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
