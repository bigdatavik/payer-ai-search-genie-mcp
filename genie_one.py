"""Genie One MCP client — plain HTTP, no heavy dependencies.

Genie One is Databricks' broad discovery front door: one managed MCP endpoint
(``/api/2.0/mcp/genie``) that searches across every certified Genie agent,
dashboard, and metric view in the workspace and routes a natural-language
question to the right one — no asset IDs required.

This module speaks MCP over Streamable HTTP (JSON-RPC) using only ``requests``
(which ships with ``databricks-sdk``), so the app's dependency footprint stays
tiny. It follows the standard MCP flow: initialize -> initialized -> genie_ask
-> poll.

``iter_ask`` yields events as the response progresses, which the FastAPI layer
turns into Server-Sent Events so the React UI can stream live status and the
long poll never trips the Databricks Apps proxy timeout.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

import requests


@dataclass
class GenieAnswer:
    status: str
    final_answer: str
    deep_link: str
    conversation_id: str
    response_id: str


class GenieOneError(RuntimeError):
    pass


class GenieOneClient:
    """Minimal MCP client for the workspace-wide Genie One server."""

    def __init__(self, host: str, token: str, timeout: float = 30.0):
        self.base = f"{host.rstrip('/')}/api/2.0/mcp/genie"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self._timeout = timeout

    # -- low-level JSON-RPC helpers -------------------------------------------
    @staticmethod
    def _strip_sse(text: str) -> str:
        # The server may reply as an SSE event ("data: {...}"); take the payload.
        out = []
        for line in text.splitlines():
            out.append(line[6:] if line.startswith("data: ") else line)
        return "\n".join(out).strip()

    def _post(self, session_id: Optional[str], payload: dict) -> requests.Response:
        headers = dict(self._headers)
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return requests.post(self.base, headers=headers, data=json.dumps(payload), timeout=self._timeout)

    def _rpc(self, session_id: Optional[str], payload: dict) -> dict:
        resp = self._post(session_id, payload)
        resp.raise_for_status()
        body = self._strip_sse(resp.text)
        return json.loads(body) if body else {}

    # -- MCP handshake ---------------------------------------------------------
    def _open_session(self) -> str:
        resp = self._post(
            None,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "payer-finance-genie-react", "version": "1.0"},
                },
            },
        )
        resp.raise_for_status()
        session_id = resp.headers.get("Mcp-Session-Id")
        if not session_id:
            raise GenieOneError("Genie One did not return an Mcp-Session-Id on initialize.")
        # Required MCP handshake step.
        self._post(session_id, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        return session_id

    # -- public API ------------------------------------------------------------
    def iter_ask(
        self,
        question: str,
        conversation_id: Optional[str] = None,
        max_polls: int = 90,
        poll_secs: float = 4.0,
    ) -> Iterator[dict]:
        """Ask Genie One a question, yielding progress events until it finishes.

        Yields ``{"type": "status", "poll": i, "status": ...}`` on each poll and
        finally ``{"type": "answer", ...}`` when the response completes. Continue
        a thread by passing ``conversation_id``. Raises ``GenieOneError`` on a
        failed/cancelled response or timeout.
        """
        session_id = self._open_session()

        ask_args = {"question": question}
        if conversation_id:
            ask_args["conversation_id"] = conversation_id
        started = self._rpc(
            session_id,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "genie_ask", "arguments": ask_args},
            },
        )
        sc = _structured(started)
        conv = sc.get("conversation_id")
        resp_id = sc.get("response_id")
        if not conv or not resp_id:
            raise GenieOneError(f"genie_ask did not start a response: {started}")

        for i in range(1, max_polls + 1):
            polled = self._rpc(
                session_id,
                {
                    "jsonrpc": "2.0",
                    "id": 10 + i,
                    "method": "tools/call",
                    "params": {
                        "name": "genie_poll_response",
                        "arguments": {"conversation_id": conv, "response_id": resp_id},
                    },
                },
            )
            psc = _structured(polled)
            status = (psc.get("status") or "").lower()
            yield {"type": "status", "poll": i, "status": status or "in_progress"}
            if status == "completed":
                yield {
                    "type": "answer",
                    "final_answer": psc.get("final_answer") or "",
                    "deep_link": psc.get("deep_link") or "",
                    "conversation_id": conv,
                    "response_id": resp_id,
                }
                return
            if status in ("failed", "cancelled"):
                raise GenieOneError(f"Genie response ended with status={status}")
            time.sleep(poll_secs)

        raise GenieOneError(f"Timed out after {max_polls} polls ({poll_secs}s each).")

    def ask(
        self,
        question: str,
        conversation_id: Optional[str] = None,
        max_polls: int = 90,
        poll_secs: float = 4.0,
        on_progress: Optional[Callable[[int, str], None]] = None,
    ) -> GenieAnswer:
        """Blocking convenience wrapper over :meth:`iter_ask`."""
        answer: Optional[GenieAnswer] = None
        for ev in self.iter_ask(question, conversation_id, max_polls, poll_secs):
            if ev["type"] == "status" and on_progress:
                on_progress(ev["poll"], ev["status"])
            elif ev["type"] == "answer":
                answer = GenieAnswer(
                    status="completed",
                    final_answer=ev["final_answer"],
                    deep_link=ev["deep_link"],
                    conversation_id=ev["conversation_id"],
                    response_id=ev["response_id"],
                )
        if answer is None:  # pragma: no cover - iter_ask raises before this
            raise GenieOneError("Genie One finished without returning an answer.")
        return answer


def _structured(rpc_result: dict) -> dict:
    """Pull the ``structuredContent`` object out of a tools/call result."""
    return (rpc_result.get("result") or {}).get("structuredContent") or {}
