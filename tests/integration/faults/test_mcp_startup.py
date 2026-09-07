"""#2370/#2330: exercise initialization over actual subprocess stdio and HTTP sockets."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests._fault_server.http import HttpFaultServer, Reply, Route, Stall
from tests._fault_server.web import homepage_response, list_response

pytestmark = pytest.mark.allow_no_vcr


@pytest.fixture
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    # Unlike the in-memory MCP tests, this test owns a real child process.
    if sys.platform == "win32":
        return asyncio.WindowsProactorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


@pytest.mark.parametrize("recover", [True, False], ids=["recover", "shutdown-during-open"])
async def test_stdio_discovery_while_client_open_is_stalled(tmp_path: Path, recover: bool) -> None:
    upstream = HttpFaultServer()
    upstream.enqueue(Route.homepage(), Stall("headers", "opening", Reply(body=homepage_response())))
    read = Route.rpc("wXbhsf")
    if recover:
        upstream.enqueue(read, Reply(body=list_response("wXbhsf", [("nb-recovered", "Recovered")])))
    report = tmp_path / "cleanup.json"
    stderr = tmp_path / "stderr.log"
    async with upstream:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "tests._fault_server.mcp_startup_worker",
                "--port",
                str(upstream.address[1]),
                "--report",
                str(report),
            ],
            env={
                **os.environ,
                "NOTEBOOKLM_HOME": str(tmp_path),
                "NOTEBOOKLM_PROFILE": "agent-2370",
            },
        )
        try:
            with stderr.open("w", encoding="utf-8") as errors:
                async with stdio_client(params, errlog=errors) as (reader, writer):
                    async with ClientSession(reader, writer) as session:
                        # Observe the upstream request BEFORE testing initialize: a
                        # timeout cannot pass just because warm-up never started.
                        await upstream.wait_for_gate("opening", timeout=10)
                        await asyncio.wait_for(session.initialize(), timeout=5)
                        tools = await asyncio.wait_for(session.list_tools(), timeout=5)
                        assert {"server_info", "notebook_list"} <= {
                            tool.name for tool in tools.tools
                        }
                        info = await asyncio.wait_for(
                            session.call_tool("server_info", {}), timeout=5
                        )
                        assert not info.isError
                        assert not upstream.gate("opening").is_set()
                        assert [row.route for row in upstream.journal] == [Route.homepage()]
                        assert not report.exists(), "client opening must still be in flight"

                        if recover:
                            upstream.release("opening")
                            notebooks = await asyncio.wait_for(
                                session.call_tool("notebook_list", {}), timeout=5
                            )
                            assert not notebooks.isError
                            assert notebooks.structuredContent is not None
                            assert (
                                notebooks.structuredContent["notebooks"][0]["id"] == "nb-recovered"
                            )
                        # Otherwise close stdin while the network open is pending.
            assert report.is_file(), "child must finalize the open during stdio shutdown"
            cleanup = json.loads(report.read_text(encoding="utf-8"))
            assert cleanup == {
                "opens": 1,
                "cancelled": not recover,
                "client_closed": True,
                "http_closed": True,
            }
            assert sum(row.route == read for row in upstream.journal) == int(recover)
            assert upstream.remaining() == 0
        finally:
            # Release only AFTER the child has shut down; doing it earlier would
            # conceal a regression in cancellation of the background opening task.
            upstream.release("opening")
    assert upstream.active_handlers == 0
    assert not upstream.errors
