"""E2E tests for the MCP server against the real NotebookLM API.

Real ``NotebookLMClient`` + real API, marked ``@pytest.mark.e2e`` (added
automatically by ``tests/e2e/conftest.py::pytest_itemcollected`` and required
explicitly here too). The MCP server is driven through FastMCP's in-memory
:class:`fastmcp.Client` against a server whose lifespan binds the real,
already-open ``client`` fixture via the ``client_factory`` seam.

Coverage:

* ``notebook_list`` (read-only) returns the live notebook set.
* The tool manifest: the core tools are present, deletes carry the
  ``destructiveHint`` annotation + a ``confirm`` parameter, and reads carry
  ``readOnlyHint``.
* A full ``create -> describe -> rename -> delete`` lifecycle driven entirely
  through MCP tools, with cleanup.
* Name resolution against live data: resolve a notebook by its title through
  ``notebook_describe`` and confirm it lands on the right id.

These require auth (``requires_auth``) and the ``mcp`` extra
(``pytest.importorskip("fastmcp")``). They are excluded from the default suite
(``addopts = --ignore=tests/e2e``) and only run via ``pytest tests/e2e -m e2e``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

# Require the `mcp` extra; skip the whole module cleanly when fastmcp is absent.
pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402 - after importorskip guard

from notebooklm import NotebookLMClient  # noqa: E402 - after importorskip guard
from notebooklm.mcp.server import create_server  # noqa: E402 - after importorskip guard

from .conftest import requires_auth  # noqa: E402 - after importorskip guard

pytestmark = pytest.mark.e2e


@contextlib.asynccontextmanager
async def _mcp_client(real_client: NotebookLMClient) -> AsyncIterator[Client]:
    """Yield an in-memory FastMCP ``Client`` bound to ``real_client``.

    Wraps the already-open E2E ``client`` fixture in a no-op async-context-manager
    factory so the server lifespan re-yields the same client (the fixture owns the
    open/close lifecycle; the factory must NOT close it).
    """

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[NotebookLMClient]:
        yield real_client

    server = create_server(client_factory=factory)
    async with Client(server) as mcp_client:
        yield mcp_client


async def _call(
    real_client: NotebookLMClient, name: str, args: dict[str, Any] | None = None
) -> Any:
    """Call one MCP tool and return its structured content."""
    async with _mcp_client(real_client) as mcp_client:
        result = await mcp_client.call_tool(name, args or {})
    return result.structured_content


@requires_auth
class TestMcpReadOnly:
    """Read-only MCP tools against the live account."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_notebook_list(self, client):
        """``notebook_list`` returns the live notebook set through MCP."""
        structured = await _call(client, "notebook_list")
        assert isinstance(structured, dict)
        assert "notebooks" in structured
        assert isinstance(structured["notebooks"], list)
        for nb in structured["notebooks"]:
            assert isinstance(nb, dict)
            assert nb.get("id")

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_server_info(self, client):
        """``server_info`` reports the version and a healthy auth probe."""
        structured = await _call(client, "server_info")
        assert structured["server"] == "notebooklm"
        assert structured["version"]
        # Auth came from real storage for the E2E run, so the probe must pass.
        assert structured["auth"]["authenticated"] is True
        assert structured["auth"]["sid_cookie"] is True


@requires_auth
class TestMcpManifest:
    """Tool-manifest presence + annotation contract against the live server."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_manifest_presence_and_annotations(self, client):
        """Core tools present; deletes DESTRUCTIVE+confirm; reads READ_ONLY."""
        async with _mcp_client(client) as mcp_client:
            tools = await mcp_client.list_tools()
        by_name = {tool.name: tool for tool in tools}

        # A representative slice of the core surface must be present.
        core = {
            "notebook_list",
            "notebook_create",
            "notebook_describe",
            "notebook_rename",
            "notebook_delete",
            "source_list",
            "chat_ask",
            "artifact_list",
            "research_status",
            "note_list",
            "server_info",
        }
        missing = core - set(by_name)
        assert not missing, f"core tools missing from the manifest: {sorted(missing)}"

        # Every delete is DESTRUCTIVE and exposes a ``confirm`` parameter.
        for name in ("notebook_delete", "source_delete", "note_delete"):
            tool = by_name[name]
            assert tool.annotations is not None, f"{name} has no annotations"
            assert tool.annotations.destructiveHint is True, f"{name} missing destructiveHint"
            assert "confirm" in tool.inputSchema.get("properties", {}), (
                f"{name} must expose a 'confirm' parameter"
            )

        # Every read tool carries readOnlyHint.
        for name in ("notebook_list", "source_list", "artifact_list", "server_info"):
            tool = by_name[name]
            assert tool.annotations is not None, f"{name} has no annotations"
            assert tool.annotations.readOnlyHint is True, f"{name} missing readOnlyHint"


@requires_auth
class TestMcpLifecycle:
    """Full create -> describe -> rename -> delete lifecycle through MCP tools."""

    @pytest.mark.asyncio
    async def test_create_describe_rename_delete(
        self, client, created_notebooks, cleanup_notebooks
    ):
        title = f"E2E-MCP-{uuid4().hex[:8]}"
        renamed = f"{title}-renamed"

        # Create via MCP.
        created = await _call(client, "notebook_create", {"title": title})
        nb_id = created["notebook_id"]
        assert nb_id
        assert created["title"] == title
        created_notebooks.append(nb_id)

        # Describe via MCP (resolves the full id directly — read path).
        described = await _call(client, "notebook_describe", {"notebook": nb_id})
        assert described["notebook_id"] == nb_id
        assert "description" in described

        # Rename via MCP.
        renamed_result = await _call(
            client, "notebook_rename", {"notebook": nb_id, "new_title": renamed}
        )
        assert renamed_result == {"notebook_id": nb_id, "new_title": renamed}

        # Delete via MCP — first preview (confirm omitted), then confirm=True.
        preview = await _call(client, "notebook_delete", {"notebook": nb_id})
        assert preview["status"] == "needs_confirmation"
        assert preview["preview"]["notebook_id"] == nb_id

        deleted = await _call(client, "notebook_delete", {"notebook": nb_id, "confirm": True})
        assert deleted == {"status": "deleted", "notebook_id": nb_id}
        created_notebooks.remove(nb_id)


@requires_auth
class TestMcpNameResolution:
    """Name-resolution against live data: resolve a notebook by its title."""

    @pytest.mark.asyncio
    async def test_resolve_notebook_by_title(self, client, created_notebooks, cleanup_notebooks):
        """A notebook created with a unique title is reachable by that title."""
        title = f"E2E-MCP-Name-{uuid4().hex[:8]}"
        created = await _call(client, "notebook_create", {"title": title})
        nb_id = created["notebook_id"]
        created_notebooks.append(nb_id)

        # Drive describe with the TITLE (not the id) — the MCP resolver must
        # case-insensitively match the title against the live notebook list and
        # land on the same id.
        described = await _call(client, "notebook_describe", {"notebook": title})
        assert described["notebook_id"] == nb_id

        # Case-insensitive match also resolves to the same id.
        described_upper = await _call(client, "notebook_describe", {"notebook": title.upper()})
        assert described_upper["notebook_id"] == nb_id
