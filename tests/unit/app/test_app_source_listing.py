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
from notebooklm.types import (
    SOURCE_STATUS_LABELS,
    Source,
    SourceStatus,
    source_status_to_str,
)


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
    resolver.assert_awaited_once_with(client, "nb_1", "Papers")
    client.labels.sources.assert_awaited_once_with("nb_1", "lbl_full_id")
    # The full-list path is NOT taken when a filter is supplied.
    client.sources.list.assert_not_called()


@pytest.mark.asyncio
async def test_presentation_options_are_not_forwarded_to_resolver() -> None:
    client = _client()
    client.labels.sources = AsyncMock(return_value=[])
    resolver = AsyncMock(return_value="lbl_id")

    await fetch_sources(client, "nb_1", label_filter="Topics", label_resolver=resolver)

    resolver.assert_awaited_once_with(client, "nb_1", "Topics")


@pytest.mark.asyncio
async def test_status_filter_narrows_to_matching_rows() -> None:
    """``status_filter`` keeps only rows whose rendered label matches (#2138)."""
    client = _client()
    ready = Source(id="s1", title="Done", status=SourceStatus.READY)
    stuck = Source(id="s2", title="probe.xlsx", status=SourceStatus.PREPARING)
    client.sources.list = AsyncMock(return_value=[ready, stuck])

    result = await fetch_sources(
        client, "nb_1", label_filter=None, label_resolver=AsyncMock(), status_filter="preparing"
    )

    assert result == [stuck]


@pytest.mark.asyncio
async def test_status_filter_surfaces_the_orphan_error_does_not() -> None:
    """The #2138 reconciliation gap, stated as a test.

    A file add that fails after registration leaves its row at ``PREPARING``,
    not ``ERROR`` — so the ``error`` filter a caller would reach for first
    returns nothing while the row is still there consuming quota. ``preparing``
    is the query that finds it.
    """
    client = _client()
    orphan = Source(id="s_orphan", title="probe_excel.xlsx", status=SourceStatus.PREPARING)
    client.sources.list = AsyncMock(return_value=[orphan])

    assert (
        await fetch_sources(
            client, "nb_1", label_filter=None, label_resolver=AsyncMock(), status_filter="error"
        )
        == []
    )
    assert await fetch_sources(
        client, "nb_1", label_filter=None, label_resolver=AsyncMock(), status_filter="preparing"
    ) == [orphan]


@pytest.mark.asyncio
async def test_status_filter_composes_with_label_filter() -> None:
    """Both filters narrow the same set, and both run inside the fetch."""
    client = _client()
    ready = Source(id="s1", status=SourceStatus.READY)
    stuck = Source(id="s2", status=SourceStatus.PREPARING)
    client.labels.sources = AsyncMock(return_value=[ready, stuck])
    client.sources.list = AsyncMock()

    result = await fetch_sources(
        client,
        "nb_1",
        label_filter="Papers",
        label_resolver=AsyncMock(return_value="lbl_id"),
        status_filter="preparing",
    )

    assert result == [stuck]
    client.labels.sources.assert_awaited_once_with("nb_1", "lbl_id")
    client.sources.list.assert_not_called()


@pytest.mark.asyncio
async def test_no_status_filter_returns_every_row() -> None:
    """The filter is opt-in — omitting it must not drop non-ready rows."""
    client = _client()
    rows = [
        Source(id="s1", status=SourceStatus.READY),
        Source(id="s2", status=SourceStatus.PREPARING),
        Source(id="s3", status=SourceStatus.ERROR),
    ]
    client.sources.list = AsyncMock(return_value=rows)

    result = await fetch_sources(client, "nb_1", label_filter=None, label_resolver=AsyncMock())

    assert result == rows


def test_status_filter_vocabulary_tracks_the_enum() -> None:
    """``SOURCE_STATUS_LABELS`` is derived, not restated (#2138).

    The CLI's ``--status`` choices come from this tuple, so a new
    ``SourceStatus`` member becomes filterable as soon as it is mapped rather
    than silently missing from the filter vocabulary.
    """
    assert set(SOURCE_STATUS_LABELS) == {source_status_to_str(s) for s in SourceStatus}
    assert "preparing" in SOURCE_STATUS_LABELS


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
