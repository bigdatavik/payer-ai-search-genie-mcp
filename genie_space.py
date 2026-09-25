"""Genie Space (Conversation API) client — plain HTTP, no heavy dependencies.

Where ``genie_one.py`` talks to Genie One (the workspace-wide *discovery* MCP,
no asset IDs), this client talks to **one specific Genie Agent / Space** via the
Genie Conversation REST API. That is what lets a second agent in the roster route
differently: it is scoped to that space's curated tables and business logic.

Flow: start-conversation (or create-message for a follow-up) -> poll get-message
until COMPLETED -> assemble a markdown answer from the space's text + SQL result
attachments. Yields progress events so the FastAPI layer can stream SSE, exactly
like ``GenieOneClient.iter_ask``.
"""

from __future__ import annotations

import time
from typing import Iterator, Optional

import requests


class GenieSpaceError(RuntimeError):
    pass


class GenieSpaceClient:
    """Minimal Conversation-API client for a single Genie Space."""

    def __init__(self, host: str, token: str, space_id: str, timeout: float = 60.0):
        self.host = host.rstrip("/")
        self.space_id = space_id
        self._base = f"{self.host}/api/2.0/genie/spaces/{space_id}"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._timeout = timeout

    def _get(self, path: str) -> dict:
        r = requests.get(f"{self._base}{path}", headers=self._headers, timeout=self._timeout)
        r.raise_for_status()
        return r.json() if r.text else {}

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(f"{self._base}{path}", headers=self._headers, json=body, timeout=self._timeout)
        r.raise_for_status()
        return r.json() if r.text else {}

    def iter_ask(
        self,
        question: str,
        conversation_id: Optional[str] = None,
        max_polls: int = 90,
        poll_secs: float = 3.0,
    ) -> Iterator[dict]:
        """Ask the Genie Space a question, yielding progress then a final answer.

        Mirrors ``GenieOneClient.iter_ask``: yields ``{"type":"status",...}`` per
        poll and finally ``{"type":"answer",...}``. Pass ``conversation_id`` to
        continue the same thread.
        """
        if conversation_id:
            started = self._post(f"/conversations/{conversation_id}/messages", {"content": question})
            conv = conversation_id
        else:
            started = self._post("/start-conversation", {"content": question})
            conv = started.get("conversation_id") or (started.get("conversation") or {}).get("id")
        msg_id = started.get("message_id") or (started.get("message") or {}).get("id")
        if not conv or not msg_id:
            raise GenieSpaceError(f"Genie Space did not start a message: {started}")

        for i in range(1, max_polls + 1):
            msg = self._get(f"/conversations/{conv}/messages/{msg_id}")
            status = (msg.get("status") or "").upper()
            yield {"type": "status", "poll": i, "status": status.lower() or "submitted"}

            if status == "COMPLETED":
                yield {
                    "type": "answer",
                    "final_answer": self._render_answer(conv, msg_id, msg),
                    "deep_link": f"{self.host}/genie/rooms/{self.space_id}",
                    "conversation_id": conv,
                    "response_id": msg_id,
                }
                return
            if status in ("FAILED", "CANCELLED"):
                err = (msg.get("error") or {}).get("error") or status
                raise GenieSpaceError(f"Genie Space response {status.lower()}: {err}")
            time.sleep(poll_secs)

        raise GenieSpaceError(f"Timed out after {max_polls} polls ({poll_secs}s each).")

    # -- answer assembly -------------------------------------------------------
    def _render_answer(self, conv: str, msg_id: str, msg: dict) -> str:
        """Turn a completed message's attachments into a markdown answer."""
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
                table = self._fetch_table(conv, msg_id, att.get("attachment_id"))
                if table:
                    parts.append(table)
        if sql_blocks:
            joined = "\n\n".join(f"```sql\n{s}\n```" for s in sql_blocks)
            parts.append(f"**Generated SQL**\n\n{joined}")
        return "\n\n".join(p for p in parts if p) or "_(The agent returned no answer text.)_"

    def _fetch_table(self, conv: str, msg_id: str, attachment_id: Optional[str]) -> str:
        """Render an attachment's query result as a markdown table (best-effort)."""
        if not attachment_id:
            return ""
        try:
            res = self._get(
                f"/conversations/{conv}/messages/{msg_id}/attachments/{attachment_id}/query-result"
            )
        except Exception:
            return ""
        sr = res.get("statement_response") or {}
        cols = (((sr.get("manifest") or {}).get("schema") or {}).get("columns")) or []
        rows = ((sr.get("result") or {}).get("data_array")) or []
        if not cols or not rows:
            return ""
        names = [c.get("name", "") for c in cols]
        header = "| " + " | ".join(names) + " |"
        sep = "| " + " | ".join("---" for _ in names) + " |"
        body_rows = []
        for row in rows[:50]:  # keep the payload small
            cells = ["" if v is None else str(v) for v in row]
            body_rows.append("| " + " | ".join(cells) + " |")
        table = "\n".join([header, sep, *body_rows])
        if len(rows) > 50:
            table += f"\n\n_Showing first 50 of {len(rows)} rows._"
        return table
