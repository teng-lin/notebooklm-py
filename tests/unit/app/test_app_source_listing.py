"""Unit tests for the transport-neutral ``notebooklm._app.source_listing`` core.

These pin the one piece of genuine ``source list`` business logic at the
``_app`` boundary (independent of the Click adapter): :func:`fetch_sources` and
its label-filter branch — which sources to fetch given an optional label filter.

* No filter → ``client.sources.list(notebook_id)``.
* ``label_filter`` set → injected ``label_resolver`` resolves the ``<id|name>``
  token, then ``client.labels.sources(notebook_id, label_id)`` returns the
  group's members (a single join, no post-filter desync).
* ``json_output`` is forwarded to the resolver only (it never reaches the list).

Pure-service tests (no Click / CliRunner): the ``ListSpec`` / ``prepare_list``
presentation half stays in the CLI adapter and is exercised in
``tests/unit/cli/test_source.py::TestSourceList``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.source_listing import fetch_sources
from notebooklm.types import Source


def _client() -> MagicMock:
    client = MagicMock()
    client.sources = MagicMock()
    client.labels = MagicMock()
    return client


@pytest.mark.asyncio
async def test_no_filter_lists_all_sources() -> None:
    client = _client()
    all_sources = [Source(id="s1", title="One"), Source(id="s2", title="Two")]
    client.sources.list = AsyncMock(return_value=all_sources)
    resolver = AsyncMock()

    result = await fetch_sources(client, "nb_1", label_filter=None, label_resolver=resolver)

    assert result == all_sources
    client.sources.list.assert_awaited_once_with("nb_1")
    # The resolver and the label-members join are never touched without a filter.
    resolver.assert_not_called()
    client.labels.sources.assert_not_called()


@pytest.mark.asyncio
async def test_label_filter_resolves_then_fetches_members() -> None:
    client = _client()
    members = [Source(id="s1", title="One")]
    client.labels.sources = AsyncMock(return_value=members)
    client.sources.list = AsyncMock()
    resolver = AsyncMock(return_value="lbl_full_id")

    result = await fetch_sources(client, "nb_1", label_filter="Papers", label_resolver=resolver)

    assert result == members
    resolver.assert_awaited_once_with(client, "nb_1", "Papers", json_output=False)
    client.labels.sources.assert_awaited_once_with("nb_1", "lbl_full_id")
    # The full-list path is NOT taken when a filter is supplied.
    client.sources.list.assert_not_called()


@pytest.mark.asyncio
async def test_json_output_forwarded_to_resolver_only() -> None:
    client = _client()
    client.labels.sources = AsyncMock(return_value=[])
    resolver = AsyncMock(return_value="lbl_id")

    await fetch_sources(
        client, "nb_1", label_filter="Topics", label_resolver=resolver, json_output=True
    )

    resolver.assert_awaited_once_with(client, "nb_1", "Topics", json_output=True)


@pytest.mark.asyncio
async def test_label_resolver_receives_client_first() -> None:
    # The resolver is called as ``resolver(client, notebook_id, token, ...)``.
    client = _client()
    client.labels.sources = AsyncMock(return_value=[])
    captured: list[object] = []

    async def resolver(*args: object, **kwargs: object) -> str:
        captured.extend(args)
        return "lbl_id"

    await fetch_sources(client, "nb_1", label_filter="x", label_resolver=resolver)

    assert captured[0] is client
    assert captured[1] == "nb_1"
    assert captured[2] == "x"
