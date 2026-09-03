"""Drive a real FastMCP tool call across a live-jar invalidation.

Uses FastMCP's in-memory protocol transport, so the call goes through the actual
MCP tool dispatch rather than a hand-rolled shim. The bound client's live cookie
jar is emptied between two identical ``notebook_list`` calls; the second call
must return the same notebooks and leave a repopulated jar behind.

Result contract: ``{"before", "after", "live_cookies", "transport"}``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastmcp import Client

from notebooklm import NotebookLMClient
from notebooklm.mcp.server import create_server

from ._contract import ScenarioResult, require, run_scenario

#: Disposable profile the orchestrator copies the source credentials into.
PROFILE = "mcp-live"


async def scenario() -> ScenarioResult:
    """Call one MCP tool, invalidate the live jar, call it again."""
    holder: dict[str, NotebookLMClient] = {}

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[NotebookLMClient]:
        async with NotebookLMClient.from_storage(profile=PROFILE, keepalive=600.0) as client:
            holder["client"] = client
            yield client

    server = create_server(profile=PROFILE, client_factory=factory)
    async with Client(server) as mcp:
        before_result = await mcp.call_tool("notebook_list", {"limit": 50})
        require(
            before_result.structured_content is not None,
            "MCP baseline returned no structured content",
        )
        before = before_result.structured_content
        require(
            "total" in before and "notebooks" in before,
            "MCP baseline response is incomplete",
        )
        holder["client"]._require_web_runtime().kernel.get_http_client().cookies.clear()
        after_result = await mcp.call_tool("notebook_list", {"limit": 50})
        require(
            after_result.structured_content is not None,
            "MCP recovery returned no structured content",
        )
        after = after_result.structured_content
        require(
            "total" in after and "notebooks" in after,
            "MCP recovery response is incomplete",
        )
        require(
            before.get("total") == after.get("total"),
            "MCP recovery changed the notebook count",
        )
        require(
            before.get("notebooks") == after.get("notebooks"),
            "MCP recovery changed notebook identity",
        )
        live = holder["client"]._require_web_runtime().kernel.get_http_client().cookies
        require(len(live) > 0, "MCP recovery did not restore the live jar")
        return {
            "before": before.get("total"),
            "after": after.get("total"),
            "live_cookies": len(live),
            "transport": "fastmcp-in-memory",
        }


def main() -> None:
    """Entry point for ``python -m`` execution by the live-auth matrix."""
    run_scenario(scenario)


if __name__ == "__main__":
    main()
