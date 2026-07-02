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

#: Merged ``studio_list`` item ``type`` values (hyphenated, the shared Studio
#: vocabulary) whose download is wired through ``studio_download``. An item's
#: ``type`` doubles as the ``studio_download`` ``artifact_type`` key, so no
#: translation is needed (unlike the old underscored ``_artifact_type`` codes).
DOWNLOADABLE_ARTIFACT_TYPES = {
    "audio",
    "video",
    "slide-deck",
    "infographic",
    "report",
    "mind-map",
    "data-table",
    "quiz",
    "flashcards",
}


def pick_downloadable_artifact(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the first ready, downloadable artifact among merged studio ``items``.

    Operates on the unified ``studio_list`` item shape: a hyphenated ``type``
    discriminator (``note`` items and non-downloadable types are skipped) plus a
    tolerant ``status_label`` check ("ready" tolerates a missing/None label as well
    as the terminal ``ready``/``completed`` states). Lets a test reuse whatever
    artifact a notebook already has and skip cleanly when none qualifies.
    """
    return next(
        (
            it
            for it in items
            if it.get("type") in DOWNLOADABLE_ARTIFACT_TYPES
            and it.get("status_label") in (None, "ready", "completed")
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
