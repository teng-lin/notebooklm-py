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

from notebooklm._lookup import unwrap_or_raise
from notebooklm._web.artifacts import WebArtifactsAPI
from notebooklm._web.mind_maps import NoteBackedMindMapService, WebMindMapsAPI
from notebooklm._web.notebooks import WebNotebooksAPI
from notebooklm._web.notes import NoteService, WebNotesAPI
from notebooklm._web.sources import WebSourcesAPI
from notebooklm.exceptions import ClientError, NotebookNotFoundError, RPCError
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


def _make_notebooks_api(rpc_call: AsyncMock) -> WebNotebooksAPI:
    # ADR-0007: configure the rpc_call seam via constructor injection
    # (``make_fake_core(rpc_call=...)``) rather than dotted AsyncMock attribute
    # assignment, which the forbidden-monkeypatch lint rejects.
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=rpc_call)
    return WebNotebooksAPI(core.rpc_executor, sources_api=MagicMock())


@pytest.fixture
def sources_api():
    return WebSourcesAPI(MagicMock(), supervisor=MagicMock(), uploader=MagicMock())


@pytest.fixture
def artifacts_api():
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock(), get_source_ids=AsyncMock(return_value=[]))
    mind_maps = MagicMock(spec=NoteBackedMindMapService)
    mind_maps.list_mind_maps = AsyncMock(return_value=[])
    notebooks = MagicMock()
    notebooks.get_source_ids = AsyncMock(return_value=[])
    return WebArtifactsAPI(
        rpc=core,
        supervisor=core,
        notebooks=notebooks,
        mind_maps=mind_maps,
        note_service=MagicMock(spec=NoteService),
    )


@pytest.fixture
def notes_api():
    from tests._fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock())
    note_service = NoteService(core, supervisor=core)
    mind_maps = NoteBackedMindMapService(note_service)
    return WebNotesAPI(notes=note_service, mind_maps=mind_maps)


@pytest.fixture
def mind_maps_api():
    rpc = MagicMock(rpc_call=AsyncMock(return_value=None))
    mind_maps = MagicMock()
    mind_maps.list_mind_maps = AsyncMock(return_value=[])
    mind_maps.extract_content = MagicMock(side_effect=lambda row: row[1])
    artifacts = MagicMock()
    artifacts.list = AsyncMock(return_value=[])
    notebooks = MagicMock()
    return WebMindMapsAPI(
        rpc=rpc,
        mind_maps=mind_maps,
        artifacts=artifacts,
        notebooks=notebooks,
        notes=MagicMock(),
    )


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

    @pytest.mark.asyncio
    async def test_returns_none_on_grpc_not_found(self):
        # The live backend answers an unknown id with a proper RPC error (gRPC
        # status 5), which the decoder raises as ClientError — a *sibling* of
        # NotebookNotFoundError under RPCError, not an ancestor. Before #2132
        # this propagated straight out of the sanctioned None-on-miss lookup.
        api = _make_notebooks_api(
            AsyncMock(side_effect=ClientError("The server rejected this request.", rpc_code=5))
        )
        assert await api.get_or_none("nb_missing") is None

    @pytest.mark.asyncio
    async def test_grpc_not_found_as_string_code_also_returns_none(self):
        # ``rpc_code`` is typed ``str | int | None`` — a string "5" is the same
        # status and must not slip past the comparison.
        api = _make_notebooks_api(AsyncMock(side_effect=ClientError("rejected", rpc_code="5")))
        assert await api.get_or_none("nb_missing") is None

    @pytest.mark.asyncio
    async def test_permission_denied_is_not_a_miss(self):
        # The decoder routes status 7 (PERMISSION_DENIED) through the SAME
        # ClientError branch as status 5. A notebook the caller may not read is
        # not a notebook that does not exist: collapsing it to None would tell
        # the caller the resource is absent and hide the access failure.
        api = _make_notebooks_api(AsyncMock(side_effect=ClientError("denied", rpc_code=7)))
        with pytest.raises(ClientError):
            await api.get_or_none("nb_forbidden")

    @pytest.mark.asyncio
    async def test_non_numeric_rpc_code_is_not_a_miss(self):
        # The decoder also puts non-numeric labels in ``rpc_code``; the
        # comparison must answer "not a miss" rather than raise on int().
        api = _make_notebooks_api(
            AsyncMock(side_effect=ClientError("boom", rpc_code="USER_DISPLAYABLE_ERROR"))
        )
        with pytest.raises(ClientError):
            await api.get_or_none("nb_1")

    @pytest.mark.asyncio
    async def test_get_raises_typed_not_found_on_grpc_not_found(self):
        # get_or_none only works because get() honours its documented contract.
        # Pin the translation at the source, not just its downstream effect.
        api = _make_notebooks_api(AsyncMock(side_effect=ClientError("rejected", rpc_code=5)))
        with pytest.raises(NotebookNotFoundError):
            await api.get("nb_missing")

    @pytest.mark.asyncio
    async def test_translation_carries_the_diagnostic_onto_the_typed_error(self):
        # Status 5 also means "the notebook is under another signed-in
        # account", and the decoder synthesises that guidance into its message.
        # Every adapter renders str(exc), never __cause__, so the guidance has
        # to survive on the raised exception itself — not only in the chain.
        original = ClientError(
            "The server rejected this request (not found). "
            "If you have multiple Google accounts signed in, "
            "this is commonly an account-routing mismatch.",
            rpc_code=5,
            raw_response="wire-bytes",
        )
        api = _make_notebooks_api(AsyncMock(side_effect=original))

        with pytest.raises(NotebookNotFoundError) as caught:
            await api.get("nb_missing")

        raised = caught.value
        assert "nb_missing" in str(raised)
        assert "account-routing mismatch" in str(raised)
        # The wire status and debugging context ride along too, so a caller can
        # branch on the code without re-reading the chained cause.
        assert raised.rpc_code == 5
        assert raised.raw_response == "wire-bytes"
        assert raised.__cause__ is original

    @pytest.mark.asyncio
    async def test_plain_absence_message_has_no_trailing_separator(self):
        # The degenerate-payload path supplies no diagnostic, so the message
        # must stay exactly as it always read — the detail separator is only
        # appended when there is something to append.
        api = _make_notebooks_api(AsyncMock(return_value=[[]]))
        with pytest.raises(NotebookNotFoundError) as caught:
            await api.get("nb_missing")
        assert str(caught.value) == "Notebook not found: nb_missing"


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
