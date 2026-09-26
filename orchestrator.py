"""LLM tool-calling orchestrator — the industry way to route (no keyword rules).

Instead of deterministic keyword matching, a Foundation Model is given each
backend as a **tool** (with a strong description) and *picks* which one should
answer. This is agentic / semantic routing.

**Router-only mode:** the model makes ONE call purely to choose a tool; we then
run that tool and stream its answer **verbatim** (no re-synthesis), so tables
and SQL stay exactly as the underlying agent produced them. The tool the model
picks is the authoritative signal for which UI tile to light.

Tools exposed to the model:
  • ``ask_governed_data(question)`` → Genie One (workspace-wide certified agents)
  • one tool per configured custom MCP agent (e.g. ``ask_partc_reporting``),
    each called over the network against its deployed ``/mcp`` endpoint.

Events yielded (``app.py`` turns these into SSE frames):
  {"type":"route",  "agent_id","agent_name","via"}      # emitted as soon as the model chooses
  {"type":"status", "poll","status"}                     # progress while the tool runs
  {"type":"answer", "final_answer","assets","deep_link","conversation_id","via","routed_space_ids","agent_id"}
  {"type":"error",  "message"}
"""

from __future__ import annotations

import json
import os
from typing import Iterator, Optional

from openai import OpenAI

from genie_one import GenieOneClient, GenieOneError
from genie_parse import extract_certified_assets, routed_space_ids

DEFAULT_MODEL = os.environ.get("ORCHESTRATOR_MODEL", "databricks-claude-sonnet-4-6")

SYSTEM_PROMPT = (
    "You are the router for a health-plan (payer) analytics assistant. Decide which "
    "specialized data agent should answer the user's question and call EXACTLY ONE tool, "
    "passing the user's question verbatim. Choose the single best-matching tool. Only answer "
    "directly, without calling a tool, for greetings or questions about your own capabilities. "
    "Never make up data or numbers yourself — always route data questions to a tool."
)

# The always-present Genie One tool. Its description is the routing logic — it
# lists the certified domains Genie One covers so the model routes correctly.
_GENIE_ONE_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_governed_data",
        "description": (
            "Query the organization's certified, governed data via Genie One, which routes to "
            "the right certified Genie agent. Use for: finance & reporting (KPIs, MLR, PMPM, "
            "premium, cost, revenue, margin, variance); members (eligibility, risk tiers, HCC, "
            "care gaps, enrollment); pharmacy (Rx spend, adherence/PDC, formulary); home health "
            "(post-acute, rehospitalization, visit completion); primary care/PCO (utilization, "
            "admits per 1000, ER visits); HEDIS/Stars quality (measures, gaps in care); and MA "
            "plan exits (recapture, contestable members, market/network adequacy)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The user's data question, verbatim."}
            },
            "required": ["question"],
        },
    },
}


def _external_tool_spec(agent: dict) -> dict:
    """Build a function-tool spec for one configured custom MCP agent."""
    desc = agent.get("description") or f"{agent['name']}: {agent.get('blurb', '')}"
    return {
        "type": "function",
        "function": {
            "name": agent["tool_name"],
            "description": desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The user's question, verbatim."}
                },
                "required": ["question"],
            },
        },
    }


class Orchestrator:
    """Routes one question to Genie One or a custom MCP agent via LLM tool choice."""

    def __init__(
        self,
        host: str,
        token: str,
        external_agents: Optional[list[dict]] = None,
        model: str = DEFAULT_MODEL,
    ):
        self._host = host.rstrip("/")
        self._token = token
        self._model = model
        self._external = external_agents or []
        # Index external agents by the tool name the model will call.
        self._by_tool = {a["tool_name"]: a for a in self._external if a.get("tool_name")}
        # OpenAI-compatible client for the Foundation Model routing call. Points at
        # the workspace serving endpoints with the caller's bearer token (OBO), so
        # it needs no WorkspaceClient and avoids the app SP's env OAuth creds.
        self._llm = OpenAI(base_url=f"{self._host}/serving-endpoints", api_key=self._token)
        self._tools = [_GENIE_ONE_TOOL] + [_external_tool_spec(a) for a in self._external]

    # -- public API ------------------------------------------------------------
    def iter_ask(self, question: str, conversation_id: Optional[str] = None) -> Iterator[dict]:
        """Route the question and stream the chosen agent's answer verbatim."""
        try:
            tool_call, direct_text = self._route(question)
        except Exception as e:  # noqa: BLE001 - surface routing failures cleanly
            yield {"type": "error", "message": f"Router (model) error: {e}"}
            return

        # No tool → the model answered directly (greeting / capability question).
        if tool_call is None:
            yield {"type": "route", "agent_id": None, "agent_name": "Assistant", "via": "Assistant"}
            yield {
                "type": "answer",
                "final_answer": direct_text or "_(No answer.)_",
                "assets": [],
                "deep_link": "",
                "conversation_id": conversation_id or "",
                "via": "Assistant",
                "routed_space_ids": [],
                "agent_id": None,
            }
            return

        name, args = tool_call
        q = args.get("question") or question

        if name == "ask_governed_data":
            yield {"type": "route", "agent_id": "genie-one", "agent_name": "Genie One", "via": "Genie One"}
            yield from self._run_genie_one(q, conversation_id)
        elif name in self._by_tool:
            agent = self._by_tool[name]
            yield {
                "type": "route",
                "agent_id": agent["id"],
                "agent_name": agent["name"],
                "via": f"{agent['name']} (custom MCP)",
            }
            yield from self._run_external(agent, q)
        else:
            yield {"type": "error", "message": f"Model chose an unknown tool: {name}"}

    # -- routing ---------------------------------------------------------------
    def _route(self, question: str) -> tuple[Optional[tuple[str, dict]], Optional[str]]:
        """Ask the Foundation Model to pick a tool. Returns ((name,args), None) or (None, text)."""
        resp = self._llm.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
            tools=self._tools,
            tool_choice="auto",
            temperature=0,
        )
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        if not tool_calls:
            return None, (msg.content or "")
        tc = tool_calls[0]
        try:
            args = json.loads(tc.function.arguments or "{}")
        except Exception:
            args = {}
        return (tc.function.name, args), None

    # -- tool execution --------------------------------------------------------
    def _run_genie_one(self, question: str, conversation_id: Optional[str]) -> Iterator[dict]:
        gc = GenieOneClient(host=self._host, token=self._token)
        try:
            for ev in gc.iter_ask(question, conversation_id=conversation_id):
                if ev["type"] == "status":
                    yield {"type": "status", "poll": ev["poll"], "status": ev["status"]}
                elif ev["type"] == "answer":
                    body = ev["final_answer"] or "_(Genie returned no answer text.)_"
                    yield {
                        "type": "answer",
                        "final_answer": body,
                        "assets": extract_certified_assets(body),
                        "deep_link": ev["deep_link"],
                        "conversation_id": ev["conversation_id"],
                        "via": "Genie One",
                        "routed_space_ids": routed_space_ids(body),
                        "agent_id": "genie-one",
                    }
        except GenieOneError as e:
            yield {"type": "error", "message": f"Genie One error: {e}"}

    def _run_external(self, agent: dict, question: str) -> Iterator[dict]:
        """Call a custom MCP server's tool over the network (bearer / OBO)."""
        yield {"type": "status", "poll": 1, "status": "asking_agent"}
        try:
            from custom_mcp import call_tool_sync

            text = call_tool_sync(agent["mcp_url"], self._token, agent["tool_name"], {"question": question})
        except Exception as e:  # noqa: BLE001
            yield {"type": "error", "message": f"{agent['name']} (custom MCP) error: {e}"}
            return

        # A single "answered via" chip; link to the underlying agent if configured.
        assets = []
        if agent.get("deep_link"):
            assets = [
                {
                    "kind": "External agent",
                    "icon": agent.get("icon", "🛰️"),
                    "label": agent["name"],
                    "url": agent["deep_link"],
                }
            ]
        yield {
            "type": "answer",
            "final_answer": text,
            "assets": assets,
            "deep_link": agent.get("deep_link", ""),
            "conversation_id": "",  # v1: custom MCP calls are single-turn (no threading)
            "via": f"{agent['name']} (custom MCP)",
            "routed_space_ids": [],
            "agent_id": agent["id"],
        }
