"""Shared harness for the MCP VCR integration tests.

Mirrors ``tests/integration/test_mcp_notebook_list_vcr.py`` (the original single
MCP VCR test) but factors the server-build boilerplate into a reusable helper so
each test module stays a thin "drive one tool, assert the wire shape" body.

Every test drives the in-memory FastMCP :class:`fastmcp.Client` against a server
whose lifespan binds a REAL :class:`~notebooklm.client.NotebookLMClient` (the
``client_factory`` seam the unit tests use, but pointed at a real client instead
of a mock). VCR replays HTTP, so no real auth/network is needed — the mock auth
from :func:`get_vcr_auth` is accepted because cassettes replay regardless of
tokens.

Recording is NEVER triggered here: ``NOTEBOOKLM_VCR_RECORD`` is deliberately not
set, and these tests only reuse cassettes the CLI VCR suite already recorded.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

# ``tests/integration`` is not covered by the mcp package's collect-ignore skip,
# so this conftest guards its own fastmcp import. Without the ``mcp`` extra every
# module that imports this conftest is skipped cleanly at collection.
pytest.importorskip("fastmcp")

# Make the ``tests`` directory importable for the ``vcr_config`` sibling import
# (mirrors the other top-level VCR integration modules).
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastmcp import Client  # noqa: E402 - after importorskip guard

from notebooklm import NotebookLMClient  # noqa: E402
from notebooklm.mcp.server import create_server  # noqa: E402
from tests.integration.conftest import get_vcr_auth  # noqa: E402

__all__ = ["build_mcp_client", "build_zero_retry_mcp_client"]


def _real_client_factory(
    *,
    mutate: Callable[[NotebookLMClient], None] | None = None,
) -> Callable[[], contextlib.AbstractAsyncContextManager[NotebookLMClient]]:
    """Return a ``client_factory`` yielding a real, VCR-backed client.

    ``mutate`` (optional) runs against the freshly-constructed client before it
    is opened — used by :func:`build_zero_retry_mcp_client` to zero the retry
    budgets so a single-interaction error cassette is not re-POSTed.
    """

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[NotebookLMClient]:
        auth = await get_vcr_auth()
        client = NotebookLMClient(auth)
        if mutate is not None:
            mutate(client)
        async with client:
            yield client

    return factory


def build_mcp_client() -> Client:
    """Build an in-memory FastMCP ``Client`` over a real, VCR-backed server.

    The returned client is an async context manager — ``async with`` it, then
    ``await client.call_tool(name, args)``. The bound ``NotebookLMClient`` opens
    inside the server loop (honoring the ADR-0004 loop-affinity contract) and is
    closed when the ``async with`` block exits.
    """
    server = create_server(client_factory=_real_client_factory())
    return Client(server)


def build_zero_retry_mcp_client() -> Client:
    """Build an MCP client whose bound client has every retry budget zeroed.

    A default ``NotebookLMClient`` would re-POST a failing request under its
    retry budget, and a single-interaction error cassette would then raise
    ``CannotOverwriteExistingCassetteException`` looking for a second
    interaction. Zeroing the three ``chain_host`` retry tunables surfaces the
    first recorded synthetic error immediately — the same seam
    ``cli_vcr/test_error_contract.py`` rebinds.
    """

    def _zero_retries(client: NotebookLMClient) -> None:
        client._composed.chain_host._rate_limit_max_retries = 0
        client._composed.chain_host._server_error_max_retries = 0
        client._composed.chain_host._refresh_retry_delay = 0

    server = create_server(client_factory=_real_client_factory(mutate=_zero_retries))
    return Client(server)
