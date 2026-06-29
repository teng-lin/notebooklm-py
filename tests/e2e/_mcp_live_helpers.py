"""Shared helpers for the live MCP e2e suites.

A ``_``-prefixed (non-``test_``) module so the per-suite modules
(``test_mcp.py``, ``test_mcp_http.py``, ``test_mcp_contracts.py``) can share the
in-memory FastMCP driver + the downloadable-artifact mapping WITHOUT importing
one ``test_*`` module from another (forbidden by
``tests/_guardrails/test_no_cross_test_imports.py``).

Imported only by modules that have already ``pytest.importorskip("fastmcp")``,
so the ``fastmcp`` import here is safe (it never loads on a no-``mcp`` install).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastmcp import Client

from notebooklm import NotebookLMClient
from notebooklm.mcp.server import create_server

#: Serialized ``Artifact._artifact_type`` values (underscored) whose download is
#: wired through ``artifact_download``. The download tool's spec keys are
#: hyphenated, so callers translate ``_`` → ``-`` (see :func:`download_type`).
DOWNLOADABLE_ARTIFACT_TYPES = {
    "audio",
    "video",
    "slide_deck",
    "infographic",
    "report",
    "mind_map",
    "data_table",
    "quiz",
    "flashcards",
}


def download_type(serialized_artifact_type: str) -> str:
    """Map a serialized ``_artifact_type`` (``slide_deck``) to a download key (``slide-deck``)."""
    return serialized_artifact_type.replace("_", "-")


def pick_downloadable_artifact(artifacts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first ready, downloadable artifact in ``artifacts`` (or ``None``).

    "Ready" tolerates a missing ``status`` (some serializations omit it) as well
    as the terminal ``ready``/``completed`` states; "downloadable" is membership
    in :data:`DOWNLOADABLE_ARTIFACT_TYPES`. Lets a test reuse whatever artifact a
    notebook already has and skip cleanly when none qualifies.
    """
    return next(
        (
            a
            for a in artifacts
            if a.get("_artifact_type") in DOWNLOADABLE_ARTIFACT_TYPES
            and a.get("status") in (None, "ready", "completed")
        ),
        None,
    )


@contextlib.asynccontextmanager
async def mcp_client(real_client: NotebookLMClient) -> AsyncIterator[Client]:
    """Yield an in-memory FastMCP ``Client`` bound to ``real_client``.

    Wraps the already-open E2E ``client`` fixture in a no-op async-context-manager
    factory so the server lifespan re-yields the same client (the fixture owns the
    open/close lifecycle; the factory must NOT close it).
    """

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[NotebookLMClient]:
        yield real_client

    server = create_server(client_factory=factory)
    async with Client(server) as client:
        yield client


async def call_tool(
    real_client: NotebookLMClient, name: str, args: dict[str, Any] | None = None
) -> Any:
    """Call one MCP tool over the in-memory transport and return its structured content."""
    async with mcp_client(real_client) as client:
        result = await client.call_tool(name, args or {})
    # Every tool in this suite returns a structured dict on success. Assert it here
    # so a caller subscripting the result fails LOUDLY (with the tool name) instead
    # of with an opaque ``NoneType`` subscript error — and so the assertion can't
    # be silently masked into a passing test by a ``(x or {})`` fallback.
    assert result.structured_content is not None, (
        f"MCP tool {name!r} returned no structured content"
    )
    return result.structured_content
