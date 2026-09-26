"""Local MCP client smoke test for the Part C Reporting custom MCP server.

Connects to a running server over streamable HTTP, lists its tools, and calls
``ask_partc_reporting`` with a real question — proving the agent answers through
the MCP boundary. Point it elsewhere with MCP_URL (e.g. a deployed app's /mcp).

    python test_client.py
    MCP_URL=https://<app-url>/mcp python test_client.py
"""

from __future__ import annotations

import asyncio
import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000/mcp")
QUESTION = os.environ.get("MCP_QUESTION", "What is our data quality score by contract?")
# A deployed Databricks App is OAuth-gated: pass a bearer token via MCP_TOKEN
# (e.g. `databricks auth token --profile fevm | jq -r .access_token`). Local
# runs need no token.
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

            print(f"\n→ calling ask_partc_reporting({QUESTION!r}) …\n")
            result = await session.call_tool("ask_partc_reporting", {"question": QUESTION})
            for block in result.content:
                if getattr(block, "type", None) == "text":
                    print(block.text)
            if result.isError:
                print("\n⚠️ tool reported an error")


if __name__ == "__main__":
    asyncio.run(main())
