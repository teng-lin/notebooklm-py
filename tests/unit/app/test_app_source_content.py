"""Unit tests for the transport-neutral ``notebooklm._app.source_content`` core.

These pin the read-only source-content fetchers at the ``_app`` boundary
(independent of the Click adapter): each executor returns a typed result the
transport adapter renders into its own envelope vocabulary.

* :func:`execute_source_get` — single-source fetch via ``get_or_none`` (``None``
  when the backend no longer has it).
* :func:`execute_source_fulltext` — fulltext fetch, ``output_format`` threaded
  through.
* :func:`execute_source_guide` — guide summary + keyword normalisation (the
  keyword strip/filter + the :attr:`SourceGuideResult.is_empty` projection).
* :func:`execute_source_stale` — freshness predicate + the
  :attr:`SourceStaleResult.stale` inversion.

Pure-service tests (no Click / CliRunner): the command-layer rendering /
exit-code policy is exercised in
``tests/unit/cli/test_source_content_rendering.py`` and
``tests/unit/cli/test_source_refresh.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.source_content import (
    SourceFulltextPlan,
    SourceGetPlan,
    SourceGuidePlan,
    SourceGuideResult,
    SourceStalePlan,
    SourceStaleResult,
    execute_source_fulltext,
    execute_source_get,
    execute_source_guide,
    execute_source_stale,
)
from notebooklm.types import Source, SourceFulltext, SourceGuide


def _client() -> MagicMock:
    client = MagicMock()
    client.sources = MagicMock()
    return client


# ===========================================================================
# execute_source_get
# ===========================================================================


@pytest.mark.asyncio
async def test_get_returns_source() -> None:
    client = _client()
    src = Source(id="src_1", title="One")
    client.sources.get_or_none = AsyncMock(return_value=src)
    result = await execute_source_get(client, SourceGetPlan(notebook_id="nb_1", source_id="src_1"))
    assert result.source is src
    assert result.notebook_id == "nb_1"
    assert result.source_id == "src_1"
    client.sources.get_or_none.assert_awaited_once_with("nb_1", "src_1")


@pytest.mark.asyncio
async def test_get_returns_none_when_missing() -> None:
    client = _client()
    client.sources.get_or_none = AsyncMock(return_value=None)
    result = await execute_source_get(client, SourceGetPlan(notebook_id="nb_1", source_id="gone"))
    assert result.source is None


# ===========================================================================
# execute_source_fulltext
# ===========================================================================


@pytest.mark.asyncio
async def test_fulltext_threads_output_format() -> None:
    client = _client()
    ft = SourceFulltext(source_id="src_1", title="One", content="body", char_count=4)
    client.sources.get_fulltext = AsyncMock(return_value=ft)
    result = await execute_source_fulltext(
        client, SourceFulltextPlan(notebook_id="nb_1", source_id="src_1", output_format="markdown")
    )
    assert result.fulltext is ft
    client.sources.get_fulltext.assert_awaited_once_with("nb_1", "src_1", output_format="markdown")


# ===========================================================================
# execute_source_guide — summary + keyword normalisation
# ===========================================================================


@pytest.mark.asyncio
async def test_guide_normalises_keywords() -> None:
    client = _client()
    client.sources.get_guide = AsyncMock(
        return_value=SourceGuide(summary="A summary", keywords=("  alpha ", "beta", "", "  "))
    )
    result = await execute_source_guide(
        client, SourceGuidePlan(notebook_id="nb_1", source_id="src_1")
    )
    assert result.summary == "A summary"
    # Stripped + blank-filtered.
    assert result.keywords == ("alpha", "beta")
    assert result.is_empty is False


@pytest.mark.asyncio
async def test_guide_empty_summary_and_keywords_is_empty() -> None:
    client = _client()
    client.sources.get_guide = AsyncMock(return_value=SourceGuide(summary="   ", keywords=()))
    result = await execute_source_guide(
        client, SourceGuidePlan(notebook_id="nb_1", source_id="src_1")
    )
    assert result.summary == "   "
    assert result.keywords == ()
    assert result.is_empty is True


@pytest.mark.asyncio
async def test_guide_non_str_summary_coerced_to_empty() -> None:
    client = _client()
    # Defensive: a malformed backend payload with a non-str summary.
    guide = MagicMock()
    guide.summary = None
    guide.keywords = ["kw"]
    client.sources.get_guide = AsyncMock(return_value=guide)
    result = await execute_source_guide(
        client, SourceGuidePlan(notebook_id="nb_1", source_id="src_1")
    )
    assert result.summary == ""
    assert result.keywords == ("kw",)
    # Summary empty but keywords present → not empty.
    assert result.is_empty is False


@pytest.mark.asyncio
async def test_guide_non_iterable_keywords_coerced_to_empty() -> None:
    client = _client()
    guide = MagicMock()
    guide.summary = "S"
    guide.keywords = None
    client.sources.get_guide = AsyncMock(return_value=guide)
    result = await execute_source_guide(
        client, SourceGuidePlan(notebook_id="nb_1", source_id="src_1")
    )
    assert result.keywords == ()


def test_guide_result_is_empty_property_summary_only() -> None:
    # A summary alone is enough to be non-empty.
    assert SourceGuideResult(source_id="s", summary="x", keywords=()).is_empty is False


# ===========================================================================
# execute_source_stale — freshness predicate
# ===========================================================================


@pytest.mark.asyncio
async def test_stale_fresh_source() -> None:
    client = _client()
    client.sources.check_freshness = AsyncMock(return_value=True)
    result = await execute_source_stale(
        client, SourceStalePlan(notebook_id="nb_1", source_id="src_1")
    )
    assert result.is_fresh is True
    assert result.stale is False


@pytest.mark.asyncio
async def test_stale_outdated_source() -> None:
    client = _client()
    client.sources.check_freshness = AsyncMock(return_value=False)
    result = await execute_source_stale(
        client, SourceStalePlan(notebook_id="nb_1", source_id="src_1")
    )
    assert result.is_fresh is False
    assert result.stale is True


def test_stale_result_stale_is_inverse_of_is_fresh() -> None:
    assert SourceStaleResult(notebook_id="n", source_id="s", is_fresh=True).stale is False
    assert SourceStaleResult(notebook_id="n", source_id="s", is_fresh=False).stale is True
