"""Local/remote MCP client smoke test for the NBA Payer Genie custom MCP server.

Connects over streamable HTTP, lists tools, and calls ``ask_nba_payer_genie``.
Point it at a deployed app with MCP_URL, and pass a bearer token with MCP_TOKEN
(a deployed Databricks App is OAuth-gated).

    python test_client.py
    MCP_URL=https://<app-url>/mcp MCP_TOKEN=$(databricks auth token --profile genie | jq -r .access_token) python test_client.py
"""

from __future__ import annotations

import asyncio
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000/mcp")
QUESTION = os.environ.get("MCP_QUESTION", "What is the next best action for our highest-risk members?")
MCP_TOKEN = os.environ.get("MCP_TOKEN")


async def main() -> None:
    headers = {"Authorization": f"Bearer {MCP_TOKEN}"} if MCP_TOKEN else None
    print(f"→ connecting to {MCP_URL} ({'with' if headers else 'no'} auth)")
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("→ tools exposed:", [t.name for t in tools.tools])
            for t in tools.tools:
                print(f"   • {t.name}: {(t.description or '').strip().splitlines()[0]}")

            print(f"\n→ calling ask_nba_payer_genie({QUESTION!r}) …\n")
            result = await session.call_tool("ask_nba_payer_genie", {"question": QUESTION})
            for block in result.content:
                if getattr(block, "type", None) == "text":
                    print(block.text)
            if result.isError:
                print("\n⚠️ tool reported an error")


if __name__ == "__main__":
    asyncio.run(main())
