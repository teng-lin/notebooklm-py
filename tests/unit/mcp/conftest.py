"""Fixtures for MCP tool/server unit tests.

The MCP server is exercised through FastMCP's in-memory :class:`fastmcp.Client`
against a server whose lifespan yields a mocked ``NotebookLMClient`` (injected
via the ``client_factory`` seam). Tests configure the mock's namespace methods
(``mock_client.notebooks.list = AsyncMock(return_value=...)``) and assert on the
serialized ``structured_content`` plus that the right API method was called.

Return values can be plain local ``@dataclass`` fakes — ``to_jsonable`` converts
any dataclass — so tests need not construct real core types.
"""

from __future__ import annotations

import contextlib
import importlib.util
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# The canonical contributor install (`uv sync --frozen --extra browser --extra dev
# --extra markdown`) omits the `mcp` extra, so `fastmcp` may be absent. A bare
# top-level ``pytest.importorskip`` in a *conftest* raises during conftest import,
# which pytest reports as a collection ERROR (not a clean skip) — so instead we
# tell pytest to ignore this whole directory when fastmcp is missing. The default
# `uv run pytest` then stays green without the extra; with the extra these tests
# RUN. Each test/guardrail module that imports fastmcp also carries its own
# ``pytest.importorskip("fastmcp")`` as a belt-and-suspenders guard.
def _fastmcp_available() -> bool:
    """Whether the optional ``mcp`` extra (fastmcp) is importable.

    ``find_spec`` returns ``None`` for an absent leaf but can *raise*
    ``ModuleNotFoundError`` if an ancestor is missing — treat either as absent.
    """
    try:
        return importlib.util.find_spec("fastmcp") is not None
    except ModuleNotFoundError:
        return False


collect_ignore_glob: list[str] = []
if not _fastmcp_available():
    collect_ignore_glob = ["*"]
else:
    from fastmcp import Client, FastMCP

    from notebooklm.mcp.server import create_server

# Public client namespaces the tools reach through. Each is a MagicMock whose
# async methods tests override with AsyncMock.
_NAMESPACES = (
    "notebooks",
    "sources",
    "chat",
    "artifacts",
    "research",
    "notes",
    "sharing",
    "labels",
    "settings",
    "mind_maps",
)


@pytest.fixture
def mock_client() -> MagicMock:
    """A ``MagicMock`` standing in for ``NotebookLMClient`` with namespace attrs."""
    client = MagicMock()
    for namespace in _NAMESPACES:
        setattr(client, namespace, MagicMock())

    from notebooklm._web.sources.batch import SourceUrlBatchItem

    async def _batch_add(notebook_id: str, urls: list[str]) -> list[SourceUrlBatchItem]:
        """Adapter-test seam: model typed batch outcomes through mocked add_url.

        Production reaches the real one-RPC ``SourcesAPI._add_urls_batch``;
        this keeps existing tool tests focused on their JSON contract while
        dedicated source-batch service tests pin the wire call count/shape.
        """
        from notebooklm._app.source_batch import batch_item_is_fatal
        from notebooklm._idempotency import mark_unconfirmed
        from notebooklm.exceptions import NetworkError, RateLimitError, ServerError

        outcomes: list[SourceUrlBatchItem] = []
        for url in urls:
            try:
                source = await client.sources.add_url(notebook_id, url)
            except Exception as exc:  # noqa: BLE001 - scripted per-item outcome
                # Production sends every URL in one write. Any transport-class
                # failure leaves its committed subset unknown and is therefore
                # projected as non-retriable RPC, not as NETWORK / 429 / 5xx.
                if isinstance(exc, (NetworkError, RateLimitError, ServerError)):
                    mark_unconfirmed(exc)
                if batch_item_is_fatal(exc):
                    raise
                outcomes.append(SourceUrlBatchItem(url=url, error=exc))  # type: ignore[arg-type]
            else:
                outcomes.append(SourceUrlBatchItem(url=url, source=source))
        return outcomes

    client.sources._add_urls_batch = AsyncMock(side_effect=_batch_add)
    # `_app.download.execute_download` probes `client.artifacts._list_for_download`
    # (the #1488 raw-rows fast path). A bare MagicMock auto-vivifies it as a
    # truthy, non-awaitable attr; pin it to None so download tests exercise the
    # public `.list` fallback they mock (the fast path is covered by the _app
    # download tests, which use the real client).
    client.artifacts._list_for_download = None
    # Identity accessors used by ``server_info(include_account=True)`` — both are
    # top-level client methods (not namespace attrs), so pin them explicitly:
    # ``get_account_email`` is awaited, ``get_account_authuser`` is sync.
    client.get_account_email = AsyncMock(return_value=None)
    client.get_account_authuser = MagicMock(return_value=0)
    client.chat.get_conversation_id = AsyncMock(return_value=None)
    return client


def _server_for(mock_client: MagicMock) -> FastMCP:
    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[MagicMock]:
        yield mock_client

    return create_server(client_factory=factory)


@pytest.fixture
def server_factory(mock_client: MagicMock) -> Callable[[], FastMCP]:
    """Return a zero-arg builder for a server bound to ``mock_client``."""
    return lambda: _server_for(mock_client)


@pytest.fixture
def mcp_call(mock_client: MagicMock) -> Callable[..., Any]:
    """Return ``async (tool_name, args=None) -> ToolResult`` against the mock."""

    async def _call(tool_name: str, args: dict[str, Any] | None = None) -> Any:
        async with Client(_server_for(mock_client)) as client:
            return await client.call_tool(tool_name, args or {})

    return _call


@pytest.fixture
def mcp_list_tools(mock_client: MagicMock) -> Callable[[], Any]:
    """Return ``async () -> list[Tool]`` (the registered tool manifest)."""

    async def _list() -> Any:
        async with Client(_server_for(mock_client)) as client:
            return await client.list_tools()

    return _list


__all__ = ["AsyncMock"]  # re-exported for convenience in tool tests
