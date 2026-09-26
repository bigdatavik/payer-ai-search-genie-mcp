"""Payer Finance AI Search — FastAPI backend + React SPA with an LLM orchestrator.

A single-process Databricks App: FastAPI serves both the JSON/SSE API under
``/api`` and the pre-built React single-page app (from ``static/``). Keeping the
frontend and backend in one process/port means there is no cross-origin call, so
we sidestep the Databricks Apps reverse-proxy CORS limitation entirely.

Routing is **agentic**, not rule-based: an LLM **orchestrator** (see
``orchestrator.py``) is given each backend as a tool — **Genie One** (the
workspace-wide certified-agent router) and each configured **custom MCP agent**
(e.g. the Part C Reporting agent behind its own MCP server) — and the model picks
which to call. Router-only: we stream the chosen agent's answer verbatim, and the
tool the model picked tells the UI which tile to light.

Answers stream back over Server-Sent Events so the UI shows live progress and a
long poll never trips the proxy's idle timeout.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from databricks.sdk import WorkspaceClient

from genie_space import GenieSpaceClient, GenieSpaceError
from genie_parse import extract_certified_assets, routed_space_ids
from orchestrator import Orchestrator

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Payer Finance AI Search")


# --- credential resolution ----------------------------------------------------
@lru_cache(maxsize=1)
def _workspace_host() -> str:
    """Workspace URL — same for every request, so cache it."""
    try:
        return (WorkspaceClient().config.host or os.environ.get("DATABRICKS_HOST") or "").rstrip("/")
    except Exception:
        return (os.environ.get("DATABRICKS_HOST") or "").rstrip("/")


def _resolve_token(request: Request) -> tuple[str, str]:
    """Return (token, auth_mode).

    Prefer on-behalf-of-user: Databricks Apps forwards the signed-in user's token
    in ``X-Forwarded-Access-Token``, so Genie/MCP run with that user's Unity
    Catalog permissions. Fall back to the app service principal / local profile.
    """
    token = request.headers.get("x-forwarded-access-token")
    if token:
        return token, "on-behalf-of-user"
    try:
        auth = WorkspaceClient().config.authenticate() or {}
        return auth.get("Authorization", "").removeprefix("Bearer ").strip(), "service-principal"
    except Exception:
        return "", "service-principal"


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# --- roster config ------------------------------------------------------------
def _load_roster(env_var: str, file_var: str) -> list:
    """Load a JSON roster from an env var, or a file named by another env var."""
    raw = os.environ.get(env_var)
    if not raw:
        path = os.environ.get(file_var)
        if path:
            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(__file__), path)
            try:
                with open(path, encoding="utf-8") as f:
                    raw = f.read()
            except Exception:
                return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _genie_space_agents() -> list[dict]:
    """Optional "live" tiles backed by certified Genie Spaces (display + match).

    Read from ``GENIE_SPACE_AGENTS`` (JSON) or ``GENIE_SPACE_AGENTS_FILE`` (path)
    so the app stays portable — no workspace-specific IDs baked into source.
    Genie One does the real routing; ``space_id`` is used only to light the tile.
    """
    out: list[dict] = []
    for a in _load_roster("GENIE_SPACE_AGENTS", "GENIE_SPACE_AGENTS_FILE"):
        if not isinstance(a, dict) or not a.get("space_id") or not a.get("name"):
            continue
        out.append(
            {
                "id": a.get("id") or str(a["name"]).lower().replace(" ", "-"),
                "name": a["name"],
                "icon": a.get("icon") or "🧞",
                "blurb": a.get("blurb") or "Genie Space",
                "status": "live",
                "poweredBy": "Genie Space",
                "spaceId": a["space_id"],
                "hints": [str(h).lower() for h in (a.get("hints") or [])],
                "example": a.get("example"),
            }
        )
    return out


def _external_mcp_agents() -> list[dict]:
    """Live tiles backed by custom MCP servers reached OUTSIDE Genie One.

    Read from ``EXTERNAL_MCP_AGENTS`` (JSON) or ``EXTERNAL_MCP_AGENTS_FILE``
    (path). Each entry needs ``name``, ``mcp_url``, and ``tool_name``; the
    orchestrator exposes each as a tool the router LLM can pick, and calls the
    tool over the network against the deployed ``/mcp`` endpoint.
    """
    out: list[dict] = []
    for a in _load_roster("EXTERNAL_MCP_AGENTS", "EXTERNAL_MCP_AGENTS_FILE"):
        if not isinstance(a, dict) or not a.get("mcp_url") or not a.get("tool_name") or not a.get("name"):
            continue
        out.append(
            {
                "id": a.get("id") or str(a["name"]).lower().replace(" ", "-"),
                "name": a["name"],
                "icon": a.get("icon") or "🛰️",
                "blurb": a.get("blurb") or "Custom MCP agent",
                "status": "live",
                "external": True,
                "poweredBy": "Custom MCP",
                "mcp_url": a["mcp_url"],
                "tool_name": a["tool_name"],
                "description": a.get("description"),
                "deep_link": a.get("deep_link"),
                "example": a.get("example"),
            }
        )
    return out


@app.get("/api/config")
def config() -> dict:
    """Roster config the frontend merges in at startup (portable, no rebuild)."""
    return {
        "genie_space_agents": _genie_space_agents(),
        "external_mcp_agents": _external_mcp_agents(),
    }


class AskRequest(BaseModel):
    question: str
    conversation_id: Optional[str] = None
    # When set, bypass the orchestrator and route straight to this Genie Space.
    space_id: Optional[str] = None
    agent_name: Optional[str] = None


@app.get("/api/whoami")
def whoami(request: Request) -> dict:
    """Who is signed in, and how the app authenticates to Genie."""
    token, auth_mode = _resolve_token(request)
    email = (
        request.headers.get("x-forwarded-email")
        or request.headers.get("x-forwarded-user")
        or request.headers.get("x-forwarded-preferred-username")
    )
    if not email:
        try:
            email = WorkspaceClient().current_user.me().user_name
        except Exception:
            email = None
    return {
        "email": email,
        "host": _workspace_host(),
        "auth_mode": auth_mode,
        "connected": bool(_workspace_host() and token),
    }


@app.post("/api/ask")
def ask(req: AskRequest, request: Request) -> StreamingResponse:
    """Stream an answer over SSE — via the LLM orchestrator, or a direct space."""
    host = _workspace_host()
    token, _auth_mode = _resolve_token(request)

    def event_stream():
        if not host or not token:
            yield _sse(
                "error",
                {
                    "message": (
                        "Could not resolve Databricks host/credentials. Locally, run "
                        "`databricks auth login --profile <name>` and set "
                        "DATABRICKS_CONFIG_PROFILE."
                    )
                },
            )
            return

        # Optional escape hatch: route straight to a specific Genie Space.
        if req.space_id:
            yield from _stream_direct_space(host, token, req)
            return

        # Default path: the LLM orchestrator picks the tool (Genie One vs a
        # custom MCP agent) and we stream the chosen agent's answer verbatim.
        orch = Orchestrator(host=host, token=token, external_agents=_external_mcp_agents())
        try:
            for ev in orch.iter_ask(req.question, conversation_id=req.conversation_id):
                t = ev["type"]
                if t == "route":
                    yield _sse("route", {"agent_id": ev["agent_id"], "agent_name": ev["agent_name"], "via": ev["via"]})
                elif t == "status":
                    yield _sse("status", {"poll": ev["poll"], "status": ev["status"]})
                elif t == "answer":
                    yield _sse(
                        "done",
                        {
                            "answer": ev["final_answer"],
                            "assets": ev["assets"],
                            "deep_link": ev["deep_link"],
                            "conversation_id": ev["conversation_id"],
                            "via": ev["via"],
                            "routed_space_ids": ev["routed_space_ids"],
                            "agent_id": ev["agent_id"],
                        },
                    )
                elif t == "error":
                    yield _sse("error", {"message": ev["message"]})
        except Exception as e:  # noqa: BLE001 - surface anything else cleanly
            yield _sse("error", {"message": f"Unexpected orchestrator error: {e}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _stream_direct_space(host: str, token: str, req: AskRequest):
    """Route straight to one Genie Space (bypasses the orchestrator)."""
    via = req.agent_name or "Genie agent"
    client = GenieSpaceClient(host=host, token=token, space_id=req.space_id)
    try:
        for ev in client.iter_ask(req.question, conversation_id=req.conversation_id):
            if ev["type"] == "status":
                yield _sse("status", {"poll": ev["poll"], "status": ev["status"]})
            elif ev["type"] == "answer":
                body = ev["final_answer"] or "_(Genie returned no answer text.)_"
                yield _sse(
                    "done",
                    {
                        "answer": body,
                        "assets": [{"kind": "Genie agent", "icon": "🧞", "label": via, "url": ev["deep_link"]}],
                        "deep_link": ev["deep_link"],
                        "conversation_id": ev["conversation_id"],
                        "via": via,
                        "routed_space_ids": [req.space_id],
                        "agent_id": None,
                    },
                )
    except GenieSpaceError as e:
        yield _sse("error", {"message": f"{via} error: {e}"})


# --- static SPA (mounted last so /api routes win) -----------------------------
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
else:

    @app.get("/", response_class=HTMLResponse)
    def _needs_build() -> str:
        return (
            "<h1>Payer Finance AI Search</h1>"
            "<p>The React frontend has not been built yet. Run "
            "<code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code> "
            "to produce <code>static/</code>, then reload.</p>"
        )


if __name__ == "__main__":
    import uvicorn

    # Databricks Apps assigns the port via $DATABRICKS_APP_PORT; bind 0.0.0.0.
    port = int(os.environ.get("DATABRICKS_APP_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
