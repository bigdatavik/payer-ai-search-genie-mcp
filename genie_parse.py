"""Parse Genie One's markdown answer for the certified assets it cited.

Shared by ``app.py`` and ``orchestrator.py`` so both can turn Genie One's answer
into (a) the certified-asset chips shown under an answer and (b) the routed
Genie Space IDs used to light the specific certified tile.
"""

from __future__ import annotations

import re

# Markdown link matcher: [label](url)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_ROOM_RE = re.compile(r"/genie/rooms/([0-9a-fA-F]+)")


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


def routed_space_ids(markdown: str) -> list[str]:
    """The Genie Space IDs Genie One routed to, parsed from its answer's links.

    Lets the UI light up the certified agent Genie One actually chose.
    """
    seen: list[str] = []
    for sid in _ROOM_RE.findall(markdown):
        if sid not in seen:
            seen.append(sid)
    return seen
