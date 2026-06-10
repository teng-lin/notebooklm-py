"""VCR integration test for the MCP ``notebook_list`` tool.

Drives the MCP tool end-to-end through a REAL :class:`~notebooklm.client.NotebookLMClient`
(not the unit-test mock) with VCR intercepting HTTP, proving the full
``MCP tool -> _app/client -> recorded RPC`` path against an EXISTING cassette.

Cassette reuse (NEVER re-record): this replays the SAME ``notebooks_list.yaml``
cassette the CLI notebook-list VCR test uses
(``tests/integration/cli_vcr/test_notebooks.py::TestListCommand``). The
``notebooklm_vcr`` matcher keys on ``rpcids`` + body-shape (see
``tests/vcr_config.py``), so the MCP code path — which calls
``client.notebooks.list()`` and issues the same ``LIST_NOTEBOOKS`` (rpcid
``wXbhsf``) batchexecute POST as the CLI — matches the recording without any
re-record. ``NOTEBOOKLM_VCR_RECORD`` is deliberately NOT set.

The MCP server is exercised through FastMCP's in-memory :class:`fastmcp.Client`
against a server whose lifespan binds the real client via the ``client_factory``
seam (the same seam the unit tests use, but pointed at a real
``NotebookLMClient`` instead of a mock).
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

# tests/integration is NOT covered by the mcp conftest's collect-ignore skip, so
# this module guards its own fastmcp import. Without the `mcp` extra the whole
# module is skipped cleanly.
pytest.importorskip("fastmcp")

# Add tests directory to path for the ``vcr_config`` import (mirrors
# ``test_settings_vcr.py`` and the other top-level VCR integration tests).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from fastmcp import Client  # noqa: E402 - after importorskip guard

from notebooklm import NotebookLMClient  # noqa: E402
from notebooklm.mcp.server import create_server  # noqa: E402
from tests.integration.conftest import get_vcr_auth, skip_no_cassettes  # noqa: E402
from tests.vcr_config import notebooklm_vcr  # noqa: E402

# Both module-level marks: ``vcr`` so the integration-tier collection hook and
# the keepalive/network-guard autouse fixtures recognize this as a VCR test, and
# ``skip_no_cassettes`` so it skips when no real cassettes are present.
pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# The cassette this test reuses — identical to the CLI notebook-list VCR test.
CASSETTE = "notebooks_list.yaml"


@pytest.mark.vcr
@pytest.mark.asyncio
@notebooklm_vcr.use_cassette(CASSETTE)
async def test_mcp_notebook_list_over_vcr() -> None:
    """``notebook_list`` returns recorded notebooks through the real client.

    End-to-end: in-memory FastMCP ``Client`` -> ``notebook_list`` tool ->
    ``_app``/``client.notebooks.list()`` -> recorded ``LIST_NOTEBOOKS`` RPC.
    """
    auth = await get_vcr_auth()

    @contextlib.asynccontextmanager
    async def real_client_factory() -> AsyncIterator[NotebookLMClient]:
        async with NotebookLMClient(auth) as real_client:
            yield real_client

    server = create_server(client_factory=real_client_factory)

    async with Client(server) as mcp_client:
        result = await mcp_client.call_tool("notebook_list", {})

    # The tool projects the typed result to ``{"notebooks": [...]}`` via
    # ``to_jsonable``. Assert the structured-content shape AND that real
    # notebooks came back from the recorded RPC (proves the full path ran).
    structured = result.structured_content
    assert isinstance(structured, dict)
    assert "notebooks" in structured
    notebooks = structured["notebooks"]
    assert isinstance(notebooks, list)
    assert notebooks, "expected at least one recorded notebook from the cassette"
    # Each notebook carries at minimum an id + title from the decoded RPC row.
    first = notebooks[0]
    assert isinstance(first, dict)
    assert first.get("id"), "recorded notebook is missing an id"
    assert "title" in first
