"""Call a tool on a custom MCP server (streamable HTTP) with a bearer token.

Uses the MCP Python SDK's streamable-HTTP client with an ``Authorization:
Bearer`` header, so it works with the on-behalf-of-user token (OBO). This is the
same path ``partc-mcp-server/test_client.py`` proved over the network, and it
sidesteps ``DatabricksMCPClient``'s OAuth-only restriction for Apps-hosted MCP.
"""

from __future__ import annotations

import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def call_tool_sync(server_url: str, token: str, tool_name: str, arguments: dict) -> str:
    """Synchronously call one MCP tool and return its concatenated text content."""

    async def _run() -> str:
        headers = {"Authorization": f"Bearer {token}"}
        async with streamablehttp_client(server_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(tool_name, arguments)
                text = "".join(getattr(c, "text", "") for c in res.content)
                if res.isError:
                    raise RuntimeError(text or "tool reported an error")
                return text or "_(No answer.)_"

    return asyncio.run(_run())
