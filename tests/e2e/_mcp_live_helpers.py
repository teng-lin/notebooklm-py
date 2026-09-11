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
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from fastmcp import Client

from notebooklm import NotebookLMClient
from notebooklm.mcp._smoke import (
    DOWNLOADABLE_ARTIFACT_TYPES as DOWNLOADABLE_ARTIFACT_TYPES,
)
from notebooklm.mcp._smoke import (
    pick_downloadable_artifact as pick_downloadable_artifact,
)
from notebooklm.mcp.server import create_server

from ._generation_helpers import _TYPED_RATE_LIMIT_ATTR

_ANDROID_INVENTORY_ONLY_SLIDE_DETAIL = "PDF URL not available in artifact data"


def structured_failure_detail(result: object) -> str | None:
    """Return the typed MCP failure detail without accepting legacy flat errors."""

    if not isinstance(result, Mapping):
        return None
    failure = result.get("failure")
    if not isinstance(failure, Mapping):
        return None
    detail = failure.get("detail")
    return detail if isinstance(detail, str) else None


def is_android_inventory_only_slide_failure(
    result: object,
    *,
    backend: str,
    artifact_type: str,
) -> bool:
    """Recognize only the typed failure for an Android slide without a PDF URL."""

    detail = structured_failure_detail(result)
    failure = result.get("failure") if isinstance(result, Mapping) else None
    return (
        isinstance(result, Mapping)
        and isinstance(failure, Mapping)
        and backend == "android"
        and artifact_type == "slide-deck"
        and result.get("outcome") == "error"
        and failure.get("reason") == "download_failed"
        and detail is not None
        and _ANDROID_INVENTORY_ONLY_SLIDE_DETAIL in detail
    )


def _only_typed_rate_limit_skip(error: BaseException) -> BaseException | None:
    """Unwrap FastMCP task groups only when every leaf is our quota skip."""

    pending = [error]
    leaves: list[BaseException] = []
    while pending:
        current = pending.pop()
        children = getattr(current, "exceptions", None)
        if isinstance(children, tuple) and children:
            pending.extend(children)
        else:
            leaves.append(current)
    if leaves and all(
        isinstance(leaf, pytest.skip.Exception)
        and getattr(leaf, _TYPED_RATE_LIMIT_ATTR, False) is True
        for leaf in leaves
    ):
        return leaves[0]
    return None


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
    try:
        async with mcp_client(real_client) as client:
            result = await client.call_tool(name, args or {})
    except BaseException as error:
        # pytest's skip signal intentionally derives from BaseException. FastMCP's
        # in-memory task groups preserve it, but nest it in BaseExceptionGroup
        # layers while unwinding. Recover only our machine-marked quota signal;
        # mixed groups and every unrelated base exception still fail loudly.
        rate_limit_skip = _only_typed_rate_limit_skip(error)
        if rate_limit_skip is None:
            raise
        raise rate_limit_skip from None
    # Every tool in this suite returns a structured dict on success. Assert it here
    # so a caller subscripting the result fails LOUDLY (with the tool name) instead
    # of with an opaque ``NoneType`` subscript error — and so the assertion can't
    # be silently masked into a passing test by a ``(x or {})`` fallback.
    assert result.structured_content is not None, (
        f"MCP tool {name!r} returned no structured content"
    )
    return result.structured_content
