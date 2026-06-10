"""Tests for the public ``get_or_none`` lookup across all five namespaces.

ADR-0019 (error-and-return contract) reserves ``None``-on-miss for an explicit
``get_or_none`` while ``get`` raises on a miss (the raise-flip itself lands in
v0.8.0, issue #1247). This module pins ``get_or_none`` for ``notebooks``,
``sources``, ``artifacts``, ``notes``, and ``mind_maps``: each returns the
object on a hit, ``None`` on a genuine miss, and re-raises (never swallows) a
transport-level :class:`~notebooklm.exceptions.RPCError`.

The :func:`~notebooklm._lookup.unwrap_or_raise` helper that backs the future
``get``-raises wiring is covered directly here too (ADR-0019 Enforcement
tier-2).
"""

from __future__ import annotations

import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._lookup import unwrap_or_raise
from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._mind_maps_api import MindMapsAPI
from notebooklm._note_service import NoteService
from notebooklm._notebooks import NotebooksAPI
from notebooklm._notes import NotesAPI
from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import RPCError
from notebooklm.types import MindMap, MindMapKind, Source

# ---------------------------------------------------------------------------
# unwrap_or_raise helper (in isolation)
# ---------------------------------------------------------------------------


class TestUnwrapOrRaise:
    def test_returns_object_unchanged_when_present(self):
        sentinel = object()
        assert unwrap_or_raise(sentinel, RuntimeError("unused")) is sentinel

    def test_raises_supplied_exception_when_none(self):
        exc = RPCError("missing")
        with pytest.raises(RPCError) as caught:
            unwrap_or_raise(None, exc)
        # The exact instance is raised — callers attach resource-specific context.
        assert caught.value is exc

    def test_falsy_but_non_none_value_is_returned(self):
        # ``None`` is the only miss sentinel — other falsy values pass through.
        assert unwrap_or_raise(0, RuntimeError("unused")) == 0
        assert unwrap_or_raise("", RuntimeError("unused")) == ""
        assert unwrap_or_raise([], RuntimeError("unused")) == []


# ---------------------------------------------------------------------------
# Per-namespace fixtures
# ---------------------------------------------------------------------------


def _make_notebooks_api(rpc_call: AsyncMock) -> NotebooksAPI:
    # ADR-0007: configure the rpc_call seam via constructor injection
    # (``make_fake_core(rpc_call=...)``) rather than dotted AsyncMock attribute
    # assignment, which the forbidden-monkeypatch lint rejects.
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=rpc_call)
    return NotebooksAPI(core.rpc_executor, sources_api=MagicMock())


@pytest.fixture
def sources_api():
    return SourcesAPI(MagicMock(), uploader=MagicMock())


@pytest.fixture
def artifacts_api():
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock(), get_source_ids=AsyncMock(return_value=[]))
    mind_maps = MagicMock(spec=NoteBackedMindMapService)
    mind_maps.list_mind_maps = AsyncMock(return_value=[])
    notebooks = MagicMock()
    notebooks.get_source_ids = AsyncMock(return_value=[])
    return ArtifactsAPI(
        rpc=core,
        drain=core,
        lifecycle=core,
        notebooks=notebooks,
        mind_maps=mind_maps,
        note_service=MagicMock(spec=NoteService),
    )


@pytest.fixture
def notes_api():
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock())
    note_service = NoteService(core)
    mind_maps = NoteBackedMindMapService(note_service)
    return NotesAPI(notes=note_service, mind_maps=mind_maps)


@pytest.fixture
def mind_maps_api():
    rpc = MagicMock(rpc_call=AsyncMock(return_value=None))
    mind_maps = MagicMock()
    mind_maps.list_mind_maps = AsyncMock(return_value=[])
    mind_maps.extract_content = MagicMock(side_effect=lambda row: row[1])
    artifacts = MagicMock()
    artifacts.list = AsyncMock(return_value=[])
    notebooks = MagicMock()
    return MindMapsAPI(rpc=rpc, mind_maps=mind_maps, artifacts=artifacts, notebooks=notebooks)


# ---------------------------------------------------------------------------
# notebooks.get_or_none — arity-1 (notebook_id)
# ---------------------------------------------------------------------------


class TestNotebooksGetOrNone:
    @pytest.mark.asyncio
    async def test_returns_notebook_on_hit(self):
        # A well-formed GET_NOTEBOOK payload: result[0] carries [title, sources, id].
        api = _make_notebooks_api(AsyncMock(return_value=[["X", [], "nb_1"]]))
        result = await api.get_or_none("nb_1")
        assert result is not None
        assert result.id == "nb_1"

    @pytest.mark.asyncio
    async def test_returns_none_on_miss(self):
        # Empty/degenerate payload is the unknown-id shape notebooks.get raises on.
        api = _make_notebooks_api(AsyncMock(return_value=[[]]))
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await api.get_or_none("nb_missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_propagates_rpc_error(self):
        # A generic transport RPCError is NOT a NotebookNotFoundError, so it must
        # propagate rather than collapse to None.
        api = _make_notebooks_api(AsyncMock(side_effect=RPCError("boom")))
        with pytest.raises(RPCError):
            await api.get_or_none("nb_1")


# ---------------------------------------------------------------------------
# sources.get_or_none — arity-2 (notebook_id, source_id)
# ---------------------------------------------------------------------------


class TestSourcesGetOrNone:
    @pytest.mark.asyncio
    async def test_returns_source_on_hit(self, sources_api):
        sources_api.list = AsyncMock(return_value=[Source(id="src_1", title="X")])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await sources_api.get_or_none("nb_1", "src_1")
        assert result is not None
        assert result.id == "src_1"

    @pytest.mark.asyncio
    async def test_returns_none_on_miss(self, sources_api):
        sources_api.list = AsyncMock(return_value=[])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await sources_api.get_or_none("nb_1", "missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_propagates_rpc_error(self, sources_api):
        sources_api.list = AsyncMock(side_effect=RPCError("boom"))
        with pytest.raises(RPCError):
            await sources_api.get_or_none("nb_1", "src_1")


# ---------------------------------------------------------------------------
# artifacts.get_or_none — arity-2 (notebook_id, artifact_id)
# ---------------------------------------------------------------------------


class TestArtifactsGetOrNone:
    @pytest.mark.asyncio
    async def test_returns_artifact_on_hit(self, artifacts_api):
        found = MagicMock()
        found.id = "art_1"
        artifacts_api.list = AsyncMock(return_value=[found])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await artifacts_api.get_or_none("nb_1", "art_1")
        assert result is found

    @pytest.mark.asyncio
    async def test_returns_none_on_miss(self, artifacts_api):
        artifacts_api.list = AsyncMock(return_value=[])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await artifacts_api.get_or_none("nb_1", "missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_propagates_rpc_error(self, artifacts_api):
        artifacts_api.list = AsyncMock(side_effect=RPCError("boom"))
        with pytest.raises(RPCError):
            await artifacts_api.get_or_none("nb_1", "art_1")


# ---------------------------------------------------------------------------
# notes.get_or_none — arity-2 (notebook_id, note_id)
# ---------------------------------------------------------------------------


class TestNotesGetOrNone:
    @pytest.mark.asyncio
    async def test_returns_note_on_hit(self, notes_api):
        notes_api._get_all_notes_and_mind_maps = AsyncMock(
            return_value=[["note_1", ["note_1", "Body", None, None, "Title"]]]
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await notes_api.get_or_none("nb_1", "note_1")
        assert result is not None
        assert result.id == "note_1"

    @pytest.mark.asyncio
    async def test_returns_none_on_miss(self, notes_api):
        notes_api._get_all_notes_and_mind_maps = AsyncMock(return_value=[])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await notes_api.get_or_none("nb_1", "missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_id_match_reads_through_note_row_adapter(self, notes_api):
        """The id-slot comparison goes through ``NoteRow.id`` (#1485).

        ``NoteRow.id`` stringifies the raw slot (the unified ``SourceRow.id``
        convention), so a non-string wire id still matches its string form
        instead of silently flipping a found note to not-found.
        """
        notes_api._get_all_notes_and_mind_maps = AsyncMock(
            return_value=[[12345, ["12345", "Body", None, None, "Title"]]]
        )
        result = await notes_api.get_or_none("nb_1", "12345")
        assert result is not None
        assert result.id == "12345"

    @pytest.mark.asyncio
    async def test_propagates_rpc_error(self, notes_api):
        notes_api._get_all_notes_and_mind_maps = AsyncMock(side_effect=RPCError("boom"))
        with pytest.raises(RPCError):
            await notes_api.get_or_none("nb_1", "note_1")


# ---------------------------------------------------------------------------
# mind_maps.get_or_none — arity-2 (notebook_id, mind_map_id)
# ---------------------------------------------------------------------------


class TestMindMapsGetOrNone:
    @pytest.mark.asyncio
    async def test_returns_mind_map_on_hit(self, mind_maps_api):
        found = MindMap(id="mm_1", notebook_id="nb_1", title="X", kind=MindMapKind.NOTE_BACKED)
        mind_maps_api.list = AsyncMock(return_value=[found])
        # mind_maps.get() was never in the deprecation family, so neither the hit
        # nor the miss path may warn — pin that for symmetry with the others.
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await mind_maps_api.get_or_none("nb_1", "mm_1")
        assert result is found

    @pytest.mark.asyncio
    async def test_returns_none_on_miss(self, mind_maps_api):
        mind_maps_api.list = AsyncMock(return_value=[])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await mind_maps_api.get_or_none("nb_1", "missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_propagates_rpc_error(self, mind_maps_api):
        mind_maps_api.list = AsyncMock(side_effect=RPCError("boom"))
        with pytest.raises(RPCError):
            await mind_maps_api.get_or_none("nb_1", "mm_1")
