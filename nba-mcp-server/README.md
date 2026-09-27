# NBA Payer Genie — cross-workspace custom MCP server + MCP Service

Demonstrates the full **cross-workspace MCP** pattern:

- **Workspace B** (`fevm-humana-nba-genie`, CLI profile `genie`) hosts a Genie agent behind a
  **custom MCP server** (this Databricks App) — self-contained: the app's service principal holds all
  the Genie/data grants.
- **Workspace A** (`fevm`) registers that app's `/mcp` URL as an **MCP Service** — a Unity Catalog
  securable governed by Unity Gateway — so A reaches B's agent with `EXECUTE` grants, tool selection,
  policies, and audit, without any A-side code or direct data access.

```
Workspace A (fevm)                                   Workspace B (fevm-humana-nba-genie)
  MCP Service  payer_catalog_vm.mcp.nba_payer_genie
      │  (Unity Gateway: EXECUTE, tool selection, policy, audit)
      ▼
  HTTP connection  payer_catalog_vm.mcp.nba_payer_genie_conn  ──HTTPS /mcp──►  App mcp-nba-payer-genie
                                                                                  └─► NBA Payer Genie
                                                                                        (space 01f1a227…)
                                                                                        humana_nba_genie_catalog
```

There are two ways to build each piece: the **UI** (see the walkthrough summary at the bottom) and the
**CLI runbook** below (what was actually run). Requires Databricks CLI ≥ 1.16 and profiles `genie`
(workspace B) and `fevm` (workspace A). **Never auto-select a profile — pass `--profile` explicitly.**

---

## Part 1 — Workspace B: deploy the custom MCP server (profile `genie`)

The server code is in this folder (`server/main.py`, FastMCP, tool `ask_nba_payer_genie` wrapping the
Genie Conversation API for space `01f1a2271a4618a8be35471ddcfb87f6`).

```bash
# from nba-mcp-server/
# (one-time, if the genie profile's token is stale:)
databricks auth login --host https://fevm-humana-nba-genie.cloud.databricks.com --profile genie

# 1. create the app (its own service principal is auto-created)
databricks apps create mcp-nba-payer-genie --profile genie

# 2. upload source + deploy
USER=$(databricks current-user me --profile genie | jq -r .userName)
databricks sync . "/Users/$USER/mcp-nba-payer-genie" --profile genie
databricks apps deploy mcp-nba-payer-genie \
  --source-code-path "/Workspace/Users/$USER/mcp-nba-payer-genie" --profile genie
# → App URL: https://mcp-nba-payer-genie-<workspace-id>.databricksapps.com  (MCP endpoint: + /mcp)
```

Grant the **app service principal** (`<APP_SP>` = the app's `service_principal_client_id`) what the
agent needs — the app runs as this SP, so it (not the caller) needs access:

```bash
APP_SP=<app service_principal_client_id>          # from `databricks apps get mcp-nba-payer-genie`
SPACE=01f1a2271a4618a8be35471ddcfb87f6

# Genie agent: CAN RUN
databricks api patch /api/2.0/permissions/genie/$SPACE \
  --json "{\"access_control_list\":[{\"service_principal_name\":\"$APP_SP\",\"permission_level\":\"CAN_RUN\"}]}" \
  --profile genie

# Underlying data: USE CATALOG + USE/SELECT on each schema the agent queries
for stmt in \
  "GRANT USE CATALOG ON CATALOG humana_nba_genie_catalog TO \`$APP_SP\`" \
  "GRANT USE SCHEMA ON SCHEMA humana_nba_genie_catalog.nba_genie TO \`$APP_SP\`" \
  "GRANT SELECT ON SCHEMA humana_nba_genie_catalog.nba_genie TO \`$APP_SP\`" \
  "GRANT USE SCHEMA ON SCHEMA humana_nba_genie_catalog.nba_new TO \`$APP_SP\`" \
  "GRANT SELECT ON SCHEMA humana_nba_genie_catalog.nba_new TO \`$APP_SP\`" ; do
  databricks experimental aitools tools query "$stmt" --profile genie
done
```

Test the B app directly (bearer = a workspace-B token):

```bash
MCP_URL="https://mcp-nba-payer-genie-<workspace-id>.databricksapps.com/mcp" \
MCP_TOKEN="$(databricks auth token --profile genie | jq -r .access_token)" \
python test_client.py
```

---

## Part 2 — Workspace A: register the MCP Service (profile `fevm`)

Reference: <https://learn.microsoft.com/en-us/azure/databricks/ai-gateway/register-mcp-service>

```bash
# 1. a schema to hold the connection + service
databricks experimental aitools tools query \
  "CREATE SCHEMA IF NOT EXISTS payer_catalog_vm.mcp" --profile fevm

# 2. HTTP connection to the B app's /mcp URL.
#    Bearer token = a token valid for the B app (short-lived — see OAuth M2M below for durable).
TOKEN=$(databricks auth token --profile genie | jq -r .access_token)
databricks connections create --parent schemas/payer_catalog_vm.mcp --profile fevm --json "{
  \"name\": \"nba_payer_genie_conn\",
  \"connection_type\": \"HTTP\",
  \"options\": {
    \"host\": \"https://mcp-nba-payer-genie-<workspace-id>.databricksapps.com\",
    \"port\": \"443\",
    \"base_path\": \"/mcp\",
    \"bearer_token\": \"$TOKEN\"
  }
}"

# 3. MCP Service from that connection (omit include_tool_selectors to expose all tools)
databricks ai-gateway create-mcp-service schemas/payer_catalog_vm.mcp nba_payer_genie --profile fevm --json '{
  "comment": "NBA Payer Genie (workspace B) via a custom MCP server, registered cross-workspace.",
  "config": { "source_connection": { "name": "connections/payer_catalog_vm.mcp.nba_payer_genie_conn" } }
}'

# 4. grant EXECUTE so others can invoke (owner can already). They also need USE CATALOG/USE SCHEMA.
databricks grants update mcp_service payer_catalog_vm.mcp.nba_payer_genie --profile fevm \
  --json '{"changes":[{"principal":"<user-or-group>","add":["EXECUTE"]}]}'
```

Test through Unity Gateway from workspace A (JSON-RPC; the `method` selects the op):

```bash
TOKEN=$(databricks auth token --profile fevm | jq -r .access_token)
URL="https://fevm-serverless-mwelio-humana.cloud.databricks.com/ai-gateway/mcp-services/payer_catalog_vm.mcp.nba_payer_genie"

# list tools
curl -s -X POST "$URL" -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# call the tool
curl -s -X POST "$URL" -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ask_nba_payer_genie","arguments":{"question":"How many members are in each risk tier?"}}}'
```

Or test in **AI Playground → Tools → MCP Servers → External MCP servers → `nba_payer_genie`**.

---

## Durable auth: OAuth M2M (recommended — what the live setup uses)

The bearer token above is a user OAuth token and **expires (~1h)** — the connection then breaks. The
durable fix is **OAuth M2M**: a service principal's `client_id`/`client_secret` stored in the connection,
which Databricks uses to mint and **auto-refresh** tokens. Agents/callers never see the credentials.

> **Gotcha:** you **cannot** `connections update` an existing connection from bearer → M2M
> ("the only valid transition is username/password → OAuth token"). Create a **new** M2M connection and
> **repoint the MCP Service** at it.

```bash
# --- Workspace B (profile genie): a service principal for the connection ---
SP=$(databricks service-principals create --display-name "mcp-nba-conn-sp" --active --profile genie -o json)
SP_ID=$(echo "$SP" | jq -r .id)                 # numeric id
CLIENT_ID=$(echo "$SP" | jq -r .applicationId)  # client_id
# OAuth secret (value shown ONCE — capture inline, don't print)
SECRET=$(databricks service-principal-secrets-proxy create "$SP_ID" --profile genie -o json | jq -r .secret)
# the SP must be allowed to invoke the B app
databricks apps update-permissions mcp-nba-payer-genie --profile genie \
  --json "{\"access_control_list\":[{\"service_principal_name\":\"$CLIENT_ID\",\"permission_level\":\"CAN_USE\"}]}"

# --- Workspace A (profile fevm): new M2M connection + repoint the service ---
databricks connections create --parent schemas/payer_catalog_vm.mcp --profile fevm --json "{
  \"name\": \"nba_payer_genie_m2m_conn\",
  \"connection_type\": \"HTTP\",
  \"options\": {
    \"host\": \"https://mcp-nba-payer-genie-<workspace-id>.databricksapps.com\",
    \"port\": \"443\", \"base_path\": \"/mcp\",
    \"client_id\": \"$CLIENT_ID\", \"client_secret\": \"$SECRET\",
    \"oauth_scope\": \"all-apis\",
    \"token_endpoint\": \"https://fevm-humana-nba-genie.cloud.databricks.com/oidc/v1/token\"
  }
}"

databricks ai-gateway update-mcp-service mcp-services/payer_catalog_vm.mcp.nba_payer_genie config \
  --profile fevm \
  --json '{"config":{"source_connection":{"name":"connections/payer_catalog_vm.mcp.nba_payer_genie_m2m_conn"}}}'

# optional: delete the old bearer connection once the service points at the M2M one
databricks connections delete payer_catalog_vm.mcp.nba_payer_genie_conn --profile fevm
```

Rotating the SP secret later means recreating the connection with the new secret (secrets are long-lived,
unlike the ~1h bearer). Keep the `client_secret` out of committed files/logs — use a secret scope if you
want it fully off the command line.

---

## UI walkthrough (click-through, for learning)

The same result without the CLI. **The map:** Workspace B *builds & hosts* the agent behind a custom
MCP server and grants its service principal; Workspace A *registers & governs* it as an MCP Service,
then uses it.

### Workspace B (`fevm-humana-nba-genie`) — host the agent behind a custom MCP server

> The MCP server is custom code (it wraps the Genie Conversation API), so it's deployed from this repo,
> not clicked together. The UI's role is the template starting point and the grants.

1. **Create the app (starting point):** **Compute → Apps → Create app → Agents category →
   "MCP Server – Hello World"** template; name it `mcp-…`. (For the real one, the template code is
   replaced with the Genie-wrapping tool in `server/main.py` and deployed via the CLI in Part 1 above.)
2. **Grant the app's service principal** what the agent needs (the app runs as this SP):
   - **Genie Agents → open "NBA Payer Genie" → Share →** add the app SP (`app-… mcp-nba-payer-genie`)
     as **Can Run**.
   - **Catalog Explorer →** `humana_nba_genie_catalog` **→ Permissions → Grant →** the app SP **→
     USE CATALOG**; repeat on schemas `nba_genie` and `nba_new` granting **USE SCHEMA + SELECT**.
3. **Copy the app URL** (App details page) — the MCP endpoint is `https://<app-url>.databricksapps.com/mcp`.

### Workspace A (`fevm`) — register it as an MCP Service

**Step 1 — Create the HTTP connection** (the pipe + stored credentials):
- **Catalog → ➕ Add → Create a connection**
- **Connection name:** `nba_payer_genie_conn`
- **Connection type:** **HTTP**
- **Auth type:** **Bearer token** *(pick **OAuth Machine to Machine** for durable auth — see above)*
- On the Authentication page: **Host** = `https://<B-app-url>.databricksapps.com`, **Port** = `443`,
  **Base Path** = `/mcp`, **Bearer Token** = a token valid for the B app
- **Create connection**

**Step 2 — Create the MCP Service** (the governed securable):
- **AI Gateway → MCPs → Register MCP Server** *(or **Catalog →** pick a schema **→ Create → MCP Service**)*
- Enter **catalog / schema / name** → `payer_catalog_vm` / `mcp` / `nba_payer_genie` (name is permanent)
- **Select an existing HTTP connection** → `nba_payer_genie_conn` *(or **Create new connection** inline)*
- **Tools:** select which tools to expose (tool selection); optionally add a comment
- **Create**

**Step 3 — Authenticate** — only if the connection uses **per-user OAuth**: open the MCP Service in
Catalog Explorer → **Login** → complete consent. (Bearer / OAuth M2M: skip.)

**Step 4 — Grant EXECUTE** (so others can invoke it):
- Open the MCP Service (Catalog Explorer, or **AI Gateway → MCPs**) → **Permissions tab → Grant**
- Select users / groups / service principals → privilege **EXECUTE** → **Grant**
- They also need **USE CATALOG** and **USE SCHEMA** on the parent catalog/schema.

**Step 5 — Test in the AI Playground:**
- **AI Playground →** pick a model with the **Tools enabled** label
- **Tools → ➕ Add tool → MCP Servers → External MCP servers →** select
  **`payer_catalog_vm.mcp.nba_payer_genie`**
- Chat, e.g. *"How many members are in each risk tier?"* — the model calls the tool and shows the result.

> **Governance note:** give end users **EXECUTE on the MCP Service**, **not** `USE CONNECTION` on the
> underlying connection — the connection is admin-only plumbing; the MCP Service is the governed door
> (tool selection, service policies, and audit via Unity Gateway).

> **UC Connection vs UC MCP Service** (both appear in the Playground's MCP picker): the **UC Connection**
> is the raw endpoint+credentials (needs `USE CONNECTION`, no governance); the **UC MCP Service** wraps
> it and routes through Unity Gateway (needs only `EXECUTE`, adds tool selection + policies + audit).
> Use the MCP Service.
