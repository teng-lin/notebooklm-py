"""Unit tests for the ``source discover`` CLI service adapter.

Focuses on the presentation half owned by
``cli.services.source_discover.execute_source_discover``: that ``--no-truncate``
actually reaches the rendered cells (long ``title`` / ``why_relevant`` text is
preserved instead of being pre-truncated before the renderer sees it).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm.cli.services.source_discover import (
    SourceDiscoverPlan,
    execute_source_discover,
)
from notebooklm.types import DiscoveredSource

_LONG_TITLE = "Quantum advantage and the road to fault-tolerant hardware in 2026 and beyond"
_LONG_WHY = (
    "An in-depth survey covering variational algorithms, error correction, and "
    "their application to genuinely complex real-world systems."
)


def _client_returning(sources: list[DiscoveredSource]) -> MagicMock:
    client = MagicMock()
    client.sources.discover = AsyncMock(return_value=sources)
    return client


def _plan(*, no_truncate: bool) -> SourceDiscoverPlan:
    return SourceDiscoverPlan(
        notebook_id="nb_1",
        query="quantum computing",
        search_source="web",
        json_output=False,
        limit=None,
        no_truncate=no_truncate,
    )


@pytest.fixture
def long_sources() -> list[DiscoveredSource]:
    return [
        DiscoveredSource(
            url="https://example.test/quantum",
            title=_LONG_TITLE,
            why_relevant=_LONG_WHY,
            source_type=1,
        )
    ]


@pytest.mark.asyncio
async def test_no_truncate_preserves_full_text(long_sources) -> None:
    client = _client_returning(long_sources)
    render = await execute_source_discover(client, _plan(no_truncate=True))

    assert render.no_truncate is True
    title_cell, _url, why_cell = render.rows[0]
    assert title_cell == _LONG_TITLE
    assert why_cell == _LONG_WHY
    assert "…" not in title_cell and "…" not in why_cell


@pytest.mark.asyncio
async def test_default_truncates_long_text(long_sources) -> None:
    client = _client_returning(long_sources)
    render = await execute_source_discover(client, _plan(no_truncate=False))

    assert render.no_truncate is False
    title_cell, _url, why_cell = render.rows[0]
    assert title_cell.endswith("…")
    assert why_cell.endswith("…")
    assert len(title_cell) < len(_LONG_TITLE)
