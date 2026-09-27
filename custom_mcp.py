"""Call a tool on a custom MCP server (streamable HTTP) with a bearer token.

Uses the MCP Python SDK's streamable-HTTP client with an ``Authorization:
Bearer`` header, so it works with the on-behalf-of-user token (OBO). This is the
same path ``partc-mcp-server/test_client.py`` proved over the network, and it
sidesteps ``DatabricksMCPClient``'s OAuth-only restriction for Apps-hosted MCP.

**Timeouts matter here.** These tools front Genie agents (NL→SQL→warehouse),
and a cross-workspace UC MCP Service call can take 20-40s+ (longer on a cold
warehouse). The SDK's default httpx ``timeout`` is only 30s — blowing past it
raises *inside* an anyio task group, which Python reports as the opaque
"unhandled errors in a TaskGroup (1 sub-exception)". We therefore (a) raise the
timeouts generously and (b) unwrap ExceptionGroups so the real cause surfaces.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Generous ceilings for slow, cross-workspace Genie-backed tools. The HTTP read
# timeout must clear the worst-case Genie round-trip (cold warehouse included).
_HTTP_TIMEOUT = timedelta(seconds=180)      # httpx read timeout for the POST
_SSE_READ_TIMEOUT = timedelta(seconds=300)  # streamed-response idle timeout
_TOOL_READ_TIMEOUT = timedelta(seconds=180)  # MCP-level per-request timeout


def _unwrap(exc: BaseException) -> BaseException:
    """Return the most informative leaf of a (possibly nested) ExceptionGroup.

    anyio wraps failures from the streamable-HTTP client in a ``TaskGroup``
    ExceptionGroup whose ``str()`` is useless. Dig out the underlying error so
    callers (and the UI) see e.g. a real timeout or HTTP status.
    """
    seen: set[int] = set()
    while True:
        subs = getattr(exc, "exceptions", None)
        if subs and id(exc) not in seen:
            seen.add(id(exc))
            exc = subs[0]
            continue
        return exc


def call_tool_sync(server_url: str, token: str, tool_name: str, arguments: dict) -> str:
    """Synchronously call one MCP tool and return its concatenated text content."""

    async def _run() -> str:
        headers = {"Authorization": f"Bearer {token}"}
        async with streamablehttp_client(
            server_url,
            headers=headers,
            timeout=_HTTP_TIMEOUT,
            sse_read_timeout=_SSE_READ_TIMEOUT,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(
                    tool_name, arguments, read_timeout_seconds=_TOOL_READ_TIMEOUT
                )
                text = "".join(getattr(c, "text", "") for c in res.content)
                if res.isError:
                    raise RuntimeError(text or "tool reported an error")
                return text or "_(No answer.)_"

    try:
        return asyncio.run(_run())
    except BaseException as e:  # noqa: BLE001 - re-raise the meaningful leaf cause
        leaf = _unwrap(e)
        if leaf is e:
            raise
        raise RuntimeError(f"{type(leaf).__name__}: {leaf}") from e


