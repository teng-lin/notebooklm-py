"""Unit tests for the MCP name/partial-id resolver.

``mcp/_resolve.py`` adds case-insensitive exact-TITLE matching on top of the
neutral ``_app.resolve.resolve_ref`` (full/partial-UUID + exact-id +
ambiguity). Routing is by token shape (``^[0-9a-fA-F-]+$``): hex-ish tokens take
the id/prefix path, everything else takes the title path. A full canonical UUID
is returned without any list call.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py. The
# resolver itself imports no fastmcp, but the guard keeps this module consistent
# with the rest of the mcp suite and self-protecting if collected directly.
pytest.importorskip("fastmcp")

from notebooklm._app.resolve import AmbiguousIdError  # noqa: E402 - after importorskip guard
from notebooklm.exceptions import (  # noqa: E402 - after importorskip guard
    NotebookNotFoundError,
    SourceNotFoundError,
)
from notebooklm.mcp._resolve import (  # noqa: E402 - after importorskip guard
    resolve_notebook,
    resolve_source,
)

FULL_A = "abc12345-6789-4abc-def0-1234567890ab"
FULL_B = "abc12345-6789-4abc-def0-ffffffffffff"


@dataclass
class _NB:
    id: str
    title: str


@dataclass
class _Src:
    id: str
    title: str | None


def _client(notebooks: list[_NB] | None = None, sources: list[_Src] | None = None) -> AsyncMock:
    client = AsyncMock()
    client.notebooks.list = AsyncMock(return_value=notebooks or [])
    client.sources.list = AsyncMock(return_value=sources or [])
    return client


# --------------------------------------------------------------------------- #
# resolve_notebook
# --------------------------------------------------------------------------- #
async def test_full_uuid_skips_the_list_call() -> None:
    client = _client(notebooks=[_NB(FULL_A, "Alpha")])
    assert await resolve_notebook(client, FULL_A) == FULL_A
    client.notebooks.list.assert_not_called()


async def test_exact_id_match() -> None:
    client = _client(notebooks=[_NB("deadbeef", "Alpha"), _NB("cafef00d", "Beta")])
    assert await resolve_notebook(client, "deadbeef") == "deadbeef"
    client.notebooks.list.assert_awaited_once()


async def test_unique_prefix_match() -> None:
    client = _client(notebooks=[_NB("deadbeef0001", "Alpha"), _NB("cafef00d", "Beta")])
    assert await resolve_notebook(client, "dead") == "deadbeef0001"


async def test_title_match_case_insensitive() -> None:
    client = _client(notebooks=[_NB("deadbeef", "My Notebook"), _NB("cafef00d", "Other")])
    assert await resolve_notebook(client, "my notebook") == "deadbeef"


async def test_title_match_casefold_non_ascii() -> None:
    """casefold (not lower) — 'STRASSE' must match the title 'Straße' (ß -> ss)."""
    client = _client(notebooks=[_NB("deadbeef", "Straße"), _NB("cafef00d", "Other")])
    assert await resolve_notebook(client, "STRASSE") == "deadbeef"


async def test_ambiguous_prefix_raises_with_candidates() -> None:
    client = _client(notebooks=[_NB("deadbeef01", "A"), _NB("deadbeef02", "B")])
    with pytest.raises(AmbiguousIdError) as caught:
        await resolve_notebook(client, "deadbeef")
    assert set(caught.value.candidate_ids) == {"deadbeef01", "deadbeef02"}


async def test_ambiguous_title_raises_with_candidates() -> None:
    client = _client(notebooks=[_NB("deadbeef", "Dup"), _NB("cafef00d", "dup")])
    with pytest.raises(AmbiguousIdError) as caught:
        await resolve_notebook(client, "Dup")
    assert set(caught.value.candidate_ids) == {"deadbeef", "cafef00d"}


async def test_no_match_title_raises_not_found() -> None:
    client = _client(notebooks=[_NB("deadbeef", "Alpha")])
    with pytest.raises(NotebookNotFoundError):
        await resolve_notebook(client, "Nonexistent Title")


async def test_no_match_prefix_raises_not_found() -> None:
    client = _client(notebooks=[_NB("deadbeef", "Alpha")])
    with pytest.raises(NotebookNotFoundError):
        await resolve_notebook(client, "ffff")


@pytest.mark.parametrize("title", ["beef", "ABBA", "1234", "DEADBEEF"])
async def test_hex_only_title_falls_back_to_title(title: str) -> None:
    """A notebook whose TITLE is all-hex resolves by name (id/prefix path misses)."""
    client = _client(notebooks=[_NB("0000aaaa1111", title), _NB("cafef00d", "Other")])
    assert await resolve_notebook(client, title) == "0000aaaa1111"


async def test_hex_token_prefers_id_over_title() -> None:
    """When a hex token is BOTH a valid id-prefix and a title, the id/prefix wins."""
    client = _client(notebooks=[_NB("beef0001", "Real Title"), _NB("cafef00d", "beef")])
    # 'beef' is a unique id-prefix of beef0001 *and* the title of cafef00d; the
    # id/prefix path must win, so the result is beef0001 (not cafef00d).
    assert await resolve_notebook(client, "beef") == "beef0001"


async def test_ambiguous_hex_prefix_does_not_fall_back_to_title() -> None:
    """An ambiguous hex PREFIX raises AmbiguousIdError — it never falls to title."""
    client = _client(
        notebooks=[_NB("beef0001", "A"), _NB("beef0002", "B"), _NB("cafef00d", "beef")]
    )
    with pytest.raises(AmbiguousIdError) as caught:
        await resolve_notebook(client, "beef")
    assert set(caught.value.candidate_ids) == {"beef0001", "beef0002"}


async def test_hex_token_matching_neither_id_nor_title_raises_not_found() -> None:
    """A hex token that is neither an id-prefix nor a title still raises NotFound."""
    client = _client(notebooks=[_NB("cafef00d", "Alpha")])
    with pytest.raises(NotebookNotFoundError):
        await resolve_notebook(client, "beef")


# --------------------------------------------------------------------------- #
# resolve_source
# --------------------------------------------------------------------------- #
async def test_source_full_uuid_skips_list() -> None:
    client = _client(sources=[_Src(FULL_A, "Doc")])
    assert await resolve_source(client, "nb-1", FULL_A) == FULL_A
    client.sources.list.assert_not_called()


async def test_source_prefix_match_lists_within_notebook() -> None:
    client = _client(sources=[_Src("ab0001cdef", "Doc"), _Src("cd0002abef", "Doc2")])
    assert await resolve_source(client, "nb-1", "ab0001") == "ab0001cdef"
    client.sources.list.assert_awaited_once_with("nb-1")


async def test_source_title_match() -> None:
    client = _client(sources=[_Src("ab0001cdef", "Report.pdf"), _Src("cd0002abef", "Notes")])
    assert await resolve_source(client, "nb-1", "report.pdf") == "ab0001cdef"


async def test_source_ambiguous_title_raises() -> None:
    client = _client(sources=[_Src("ab0001cdef", "Dup"), _Src("cd0002abef", "dup")])
    with pytest.raises(AmbiguousIdError) as caught:
        await resolve_source(client, "nb-1", "Dup")
    assert set(caught.value.candidate_ids) == {"ab0001cdef", "cd0002abef"}


async def test_source_no_match_raises_source_not_found() -> None:
    client = _client(sources=[_Src("ab0001cdef", "Doc")])
    with pytest.raises(SourceNotFoundError):
        await resolve_source(client, "nb-1", "Missing Title")


async def test_source_title_match_skips_none_titled() -> None:
    """A source with no title cannot match a title query."""
    client = _client(sources=[_Src("ab0001cdef", None), _Src("cd0002abef", "Real")])
    assert await resolve_source(client, "nb-1", "Real") == "cd0002abef"


async def test_source_hex_only_title_falls_back_to_title() -> None:
    """A source whose TITLE is all-hex resolves by name (id/prefix path misses)."""
    client = _client(sources=[_Src("0000aaaa1111", "beef"), _Src("cd0002abef", "Notes")])
    assert await resolve_source(client, "nb-1", "beef") == "0000aaaa1111"
