# Payer AI Search — a Supervisor front door over Genie One (MCP)

A polished **Databricks App** (React + FastAPI) that gives business users **one
chat box** over all their governed data. A user just asks a question; a
**Supervisor** reads it and **auto-routes** to the right certified agent — the
user never picks an agent or hunts for a dataset.

At the center is **Genie One** — Databricks' managed **MCP** server
(`/api/2.0/mcp/genie`) — a single governed entry point that discovers across
*every* certified Genie agent, metric view, and dashboard in the workspace. **No
asset IDs are hardcoded**, so this app deploys to **any workspace** unchanged and
routes to whatever certified assets exist there.

```
                       ┌────────────────────────────────────────────┐
 Browser (React SPA)   │  Supervisor (auto-routes by question)        │
   one chat box   ────▶│    ├─▶ Genie One (MCP) ─▶ every certified     │
        ▲              │    │      Genie agent / space in the workspace │
        │   SSE stream │    └─▶ optional: specific Genie Space agents   │
        └──────────────┤          (+ your own external agents)         │
                       └────────────────────────────────────────────┘
```

FastAPI serves **both** the API and the built React SPA from a single
process/port — no cross-origin calls, no CORS headaches on the Databricks Apps
proxy. Answers stream over **Server-Sent Events**, so the UI shows live progress
and a long Genie poll never trips the proxy's idle timeout.

## What you get out of the box (zero config)

- **One chat box + a Supervisor** that routes each question to the right agent.
- **A live Finance/"ask anything" agent via Genie One** — works in any workspace
  with Genie enabled, no IDs to configure.
- Runs with each signed-in user's **own Unity Catalog permissions**
  (on-behalf-of-user), so security and lineage stay intact.
- Every answer shows **which certified asset answered** and, for Genie Space
  agents, the **exact SQL** generated.
- Illustrative **preview** tiles (Claims, Pharmacy, Prior Auth, Provider Network,
  FWA) that stand in for agents you'd host elsewhere.

## Add extra Genie Space agents (optional)

Register one or more specific Genie Spaces as additional **live** agents by
setting the `GENIE_SPACE_AGENTS` env var to a JSON list — no rebuild, no code
changes. Each entry needs a `space_id`; `hints` are lowercase keywords the
Supervisor uses to route:

```json
[
  {
    "id": "member360",
    "name": "Member 360",
    "icon": "👤",
    "blurb": "Eligibility & risk",
    "space_id": "<your-genie-space-id>",
    "hints": ["member", "risk tier", "care gap", "eligibility", "hcc"]
  }
]
```

Set it locally in `.env`, or on the deployed app via `env:` in `app.yaml` /
`databricks.yml` (both files show a commented example). Find space IDs with
`databricks genie list-spaces --profile <profile>`.

## What's in this repo

| Path | Purpose |
| --- | --- |
| `app.py` | FastAPI: `/api/ask` (SSE), `/api/whoami`, `/api/config`, serves the SPA. |
| `genie_one.py` | Genie One MCP client — discovery across all certified assets (`requests` only). |
| `genie_space.py` | Genie Space Conversation-API client for a specific space (`requests` only). |
| `frontend/` | Vite + React + TypeScript source for the chat UI. |
| `static/` | **Built** SPA output (gitignored; produced by `npm run build`). |
| `app.yaml` / `databricks.yml` | Databricks Apps runtime config + Asset Bundle. |
| `requirements.txt` | Python deps (intentionally tiny, all pre-mirrored). |

## Prerequisites

- A Databricks workspace with **Genie** enabled and at least one **certified
  Genie agent / metric view** (otherwise Genie One has nothing to route to).
- **Databricks CLI v1.16+** — `databricks --version`
- **Node 20+** and **Python 3.11+**

## Deploy to any workspace

The built frontend (`static/`) is gitignored, so **build it first**, then deploy.
The bundle force-includes `static/**` in its sync (see `databricks.yml`).

```bash
# 1. Build the React SPA into static/
cd frontend && npm install && npm run build && cd ..

# 2. Authenticate to the target workspace (pick your own profile name).
databricks auth login --host https://<your-workspace>.databricks.com --profile <profile>

# 3. Validate, deploy, and start the app.
databricks bundle validate -t dev -p <profile>
databricks bundle deploy   -t dev -p <profile>
databricks bundle run payer_finance_genie_react -t dev -p <profile>
```

`bundle run` prints the app URL (e.g. `https://agent-payer-genie-ui-dev-<id>.<region>.databricksapps.com`).

| Target | App name | Notes |
| --- | --- | --- |
| `dev` (default) | `agent-payer-genie-ui-dev` | development mode |
| `prod` | `agent-payer-genie-ui` | production mode |

Override the app name with `--var app_name=<name>` (must start with `agent-`,
≤ 30 chars). Re-run `npm run build` before `bundle deploy` after any UI change.

## Run locally

Two terminals — FastAPI on `:8000`, Vite dev server on `:5173` (proxies `/api`):

```bash
# Terminal 1 — backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABRICKS_CONFIG_PROFILE=<profile>    # see .env.example
python app.py                                  # serves on :8000

# Terminal 2 — frontend (hot reload)
cd frontend && npm install && npm run dev       # open http://localhost:5173
```

To test the exact production bundle, `npm run build` then run `python app.py` and
open http://localhost:8000.

## How it works

1. React POSTs the question to `/api/ask` (with the routed agent's `space_id` if
   it's a Genie Space agent).
2. The Supervisor routes by keyword hints (sticky to the last agent for
   follow-ups). The matching agent lights up green.
3. `GenieOneClient` (discovery MCP) or `GenieSpaceClient` (Conversation API)
   runs the request, streaming status back as SSE, then a final answer with the
   certified source(s) and any generated SQL.

## License

Sample / learning code. Add a `LICENSE` file with your organization's terms
before publishing this repository publicly.
