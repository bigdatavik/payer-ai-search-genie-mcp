"""Payer Finance AI Search — FastAPI backend + React SPA over Genie One.

A single-process Databricks App: FastAPI serves both the JSON/SSE API under
``/api`` and the pre-built React single-page app (from ``static/``). Keeping the
frontend and backend in one process/port means there is no cross-origin call, so
we sidestep the Databricks Apps reverse-proxy CORS limitation entirely.

The "brain" is **Genie One** — Databricks' workspace-wide discovery MCP endpoint
(``/api/2.0/mcp/genie``). No asset IDs are configured here: Genie One discovers
and routes to whatever certified Genie agents / dashboards / metric views exist
in the workspace this app runs in, so it deploys to *any* workspace unchanged.

Answers stream back over Server-Sent Events so the UI shows live progress and a
long Genie poll never trips the proxy's idle timeout.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from databricks.sdk import WorkspaceClient

from genie_one import GenieOneClient, GenieOneError
from genie_space import GenieSpaceClient, GenieSpaceError

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = FastAPI(title="Payer Finance AI Search")


# --- certified-asset extraction -----------------------------------------------
# Markdown link matcher: [label](url)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def extract_certified_assets(markdown: str) -> list[dict]:
    """Pull the certified assets Genie One cited out of its markdown answer.

    Genie One embeds links to the assets it used — Genie agents
    (``/genie/rooms/``), dashboards (``/dashboardsv3/``), and per-query results.
    We classify and de-duplicate them so the UI can show *which* certified asset
    answered — the "one front door, know the source" point.
    """
    seen: dict[str, dict] = {}
    for label, url in _LINK_RE.findall(markdown):
        if "/genie/rooms/" in url:
            kind, icon = "Genie agent", "🧞"
        elif "/dashboardsv3/" in url or "/dashboards/" in url:
            kind, icon = "Dashboard", "📊"
        elif "#embed-query" in url or "/sql/" in url:
            kind, icon = "Query result", "🔎"
        elif "/one/chat/threads/" in url:
            continue  # the "Explore in Databricks" deep link — shown separately
        else:
            kind, icon = "Source", "📄"
        key = f"{kind}:{label}"
        if key not in seen:
            seen[key] = {"kind": kind, "icon": icon, "label": label, "url": url}
    order = {"Genie agent": 0, "Dashboard": 1, "Query result": 2, "Source": 3}
    return sorted(seen.values(), key=lambda a: order.get(a["kind"], 9))


_ROOM_RE = re.compile(r"/genie/rooms/([0-9a-fA-F]+)")


def routed_space_ids(markdown: str) -> list[str]:
    """The Genie Space IDs Genie One routed to, parsed from its answer's links.

    Lets the UI light up the certified agent Genie One actually chose.
    """
    seen: list[str] = []
    for sid in _ROOM_RE.findall(markdown):
        if sid not in seen:
            seen.append(sid)
    return seen


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
    in ``X-Forwarded-Access-Token``, so Genie runs with that user's Unity Catalog
    permissions. Fall back to the app service principal / local profile creds.
    """
    token = request.headers.get("x-forwarded-access-token")
    if token:
        return token, "on-behalf-of-user"
    # App service principal / local profile creds. Degrade gracefully when no
    # credentials are configured (e.g. local dev without a profile) so the UI
    # still loads with a "not connected" badge instead of a 500.
    try:
        auth = WorkspaceClient().config.authenticate() or {}
        return auth.get("Authorization", "").removeprefix("Bearer ").strip(), "service-principal"
    except Exception:
        return "", "service-principal"


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# --- API ----------------------------------------------------------------------
def _genie_space_agents() -> list[dict]:
    """Optional extra "live" agents backed by specific Genie Spaces.

    Read from the ``GENIE_SPACE_AGENTS`` env var (a JSON list) so the app stays
    portable — no workspace-specific space IDs are baked into the source. Each
    entry needs at least ``name`` and ``space_id``; ``hints`` (lowercase
    keywords) drive the Supervisor's auto-routing. Example value:

        [{"id":"member360","name":"Member 360","icon":"👤",
          "blurb":"Eligibility & risk","space_id":"<genie-space-id>",
          "hints":["member","risk tier","care gap","eligibility","hcc"]}]
    """
    raw = os.environ.get("GENIE_SPACE_AGENTS")
    if not raw:
        # Fall back to a JSON file (keeps big rosters out of env/YAML).
        path = os.environ.get("GENIE_SPACE_AGENTS_FILE")
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
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for a in data:
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
            }
        )
    return out


@app.get("/api/config")
def config() -> dict:
    """Roster config the frontend merges in at startup (portable, no rebuild)."""
    return {"genie_space_agents": _genie_space_agents()}


class AskRequest(BaseModel):
    question: str
    conversation_id: Optional[str] = None
    # When set, route to this specific Genie Space (a second "live" agent)
    # instead of Genie One discovery. agent_name is just a display label.
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
    """Stream Genie One's answer for a question over Server-Sent Events."""
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
        # Route: a specific Genie Space (second live agent) vs Genie One discovery.
        if req.space_id:
            via = req.agent_name or "Genie agent"
            client: object = GenieSpaceClient(host=host, token=token, space_id=req.space_id)
            errors = (GenieSpaceError,)
        else:
            via = "Genie One"
            client = GenieOneClient(host=host, token=token)
            errors = (GenieOneError,)

        try:
            for ev in client.iter_ask(req.question, conversation_id=req.conversation_id):  # type: ignore[attr-defined]
                if ev["type"] == "status":
                    yield _sse("status", {"poll": ev["poll"], "status": ev["status"]})
                elif ev["type"] == "answer":
                    body = ev["final_answer"] or "_(Genie returned no answer text.)_"
                    if req.space_id:
                        # The space itself is the certified asset that answered.
                        assets = [
                            {
                                "kind": "Genie agent",
                                "icon": "🧞",
                                "label": via,
                                "url": ev["deep_link"],
                            }
                        ]
                        routed = [req.space_id]
                    else:
                        assets = extract_certified_assets(body)
                        routed = routed_space_ids(body)
                    yield _sse(
                        "done",
                        {
                            "answer": body,
                            "assets": assets,
                            "deep_link": ev["deep_link"],
                            "conversation_id": ev["conversation_id"],
                            "via": via,
                            "routed_space_ids": routed,
                        },
                    )
        except errors as e:
            yield _sse("error", {"message": f"{via} error: {e}"})
        except Exception as e:  # noqa: BLE001 - surface anything else cleanly
            yield _sse("error", {"message": f"Unexpected error contacting {via}: {e}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
