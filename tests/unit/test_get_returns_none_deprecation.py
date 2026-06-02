"""Tests for the get()-returns-``None`` deprecation layer (#1206).

The deprecation layer makes ``sources.get`` / ``artifacts.get`` / ``notes.get``
emit a :class:`DeprecationWarning` when they are about to return ``None`` on a
miss, while keeping the ``None``-returning *behavior* unchanged. The actual flip
to raising ``*NotFoundError`` lands separately in v0.8.0 (issue #1247).

Covered here:
  * ``warn_get_returns_none`` message shape + ``NOTEBOOKLM_QUIET_DEPRECATIONS``
    suppression (the helper in isolation).
  * Each public ``get()`` warns on a miss and still returns ``None``.
  * The private ``_get_or_none()`` never warns (internal optional-lookup path).
  * ``notebooks.get`` still *raises* ``NotebookNotFoundError`` (unchanged).
  * No internal CLI not-found path self-warns (warnings escalated to errors).
"""

from __future__ import annotations

import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm import _deprecation
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._note_service import NoteService
from notebooklm._notes import NotesAPI
from notebooklm._sources import SourcesAPI
from notebooklm.types import Source

# ---------------------------------------------------------------------------
# warn_get_returns_none helper (in isolation)
# ---------------------------------------------------------------------------


class TestWarnGetReturnsNone:
    def test_emits_deprecation_warning_naming_resource_and_v080(self):
        with pytest.warns(DeprecationWarning) as record:
            _deprecation.warn_get_returns_none("source")
        assert len(record) == 1
        message = str(record[0].message)
        # Names the resource, the v0.8.0 flip, the *NotFoundError migration,
        # and the tracking issue.
        assert "sources.get()" in message
        assert "v0.8.0" in message
        assert "SourceNotFoundError" in message
        assert "try/except" in message
        assert f"#{_deprecation.GET_RETURNS_NONE_FLIP_ISSUE}" in message

    @pytest.mark.parametrize(
        ("resource", "exc_name"),
        [
            ("source", "SourceNotFoundError"),
            ("artifact", "ArtifactNotFoundError"),
            ("note", "NoteNotFoundError"),
        ],
    )
    def test_names_matching_not_found_error_per_resource(self, resource, exc_name):
        with pytest.warns(DeprecationWarning, match=exc_name):
            _deprecation.warn_get_returns_none(resource)

    def test_removal_version_is_parametrised(self):
        with pytest.warns(DeprecationWarning, match="v9.9.9"):
            _deprecation.warn_get_returns_none("source", removal="9.9.9")

    def test_existing_exception_named_unqualified(self):
        # SourceNotFoundError exists today, so the hint names it directly with
        # no "(added in ...)" qualifier.
        with pytest.warns(DeprecationWarning) as record:
            _deprecation.warn_get_returns_none("source")
        message = str(record[0].message)
        assert "try/except SourceNotFoundError." in message
        assert "added in" not in message

    def test_note_not_found_error_now_named_unqualified(self):
        # NoteNotFoundError now exists (defined ahead of the v0.8.0 flip
        # #1247), so the migration hint names it directly with no
        # "(added in ...)" qualifier — a notes caller who follows the advice
        # can import it today.
        with pytest.warns(DeprecationWarning) as record:
            _deprecation.warn_get_returns_none("note")
        message = str(record[0].message)
        assert "try/except NoteNotFoundError." in message
        assert "added in" not in message

    def test_snake_case_resource_maps_to_pascal_case_exception(self):
        # A multi-word resource name must PascalCase into the real class name
        # ("mind_map" -> "MindMapNotFoundError", not "Mind_mapNotFoundError"),
        # so the hint names the exception that actually exists and stays
        # unqualified.
        with pytest.warns(DeprecationWarning) as record:
            _deprecation.warn_get_returns_none("mind_map")
        message = str(record[0].message)
        assert "MindMapNotFoundError" in message
        assert "Mind_map" not in message
        assert "added in" not in message

    def test_not_yet_existing_exception_is_version_qualified(self):
        # A *NotFoundError that does not yet exist must be flagged as
        # not-yet-available so a caller following the migration advice doesn't
        # hit an ImportError on a not-yet-defined class. No resource ships such
        # an exception today, so exercise the qualification branch with a
        # synthetic resource name.
        with pytest.warns(DeprecationWarning) as record:
            _deprecation.warn_get_returns_none("widget")
        message = str(record[0].message)
        assert "WidgetNotFoundError (added in v0.8.0)" in message

    @pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"])
    def test_quiet_env_var_suppresses(self, monkeypatch, truthy):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", truthy)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            # Must NOT raise: suppression returns before warnings.warn.
            _deprecation.warn_get_returns_none("source")

    @pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off"])
    def test_non_truthy_env_var_does_not_suppress(self, monkeypatch, falsy):
        monkeypatch.setenv("NOTEBOOKLM_QUIET_DEPRECATIONS", falsy)
        with pytest.warns(DeprecationWarning):
            _deprecation.warn_get_returns_none("source")


# ---------------------------------------------------------------------------
# Per-API fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def notes_api():
    from _fixtures.fake_core import make_fake_core

    core = make_fake_core(rpc_call=AsyncMock())
    note_service = NoteService(core)
    mind_maps = NoteBackedMindMapService(note_service)
    return NotesAPI(
        notes=note_service,
        mind_maps=mind_maps,
    )


@pytest.fixture
def artifacts_api():
    from _fixtures.fake_core import make_fake_core

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
def sources_api():
    return SourcesAPI(MagicMock(), uploader=MagicMock())


# ---------------------------------------------------------------------------
# Each public get() warns on a miss but still returns None
# ---------------------------------------------------------------------------


class TestPublicGetWarnsOnMiss:
    @pytest.mark.asyncio
    async def test_notes_get_warns_and_returns_none(self, notes_api):
        notes_api.list = AsyncMock(return_value=[])
        with pytest.warns(DeprecationWarning, match="NoteNotFoundError"):
            result = await notes_api.get("nb_1", "missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_artifacts_get_warns_and_returns_none(self, artifacts_api):
        artifacts_api.list = AsyncMock(return_value=[])
        with pytest.warns(DeprecationWarning, match="ArtifactNotFoundError"):
            result = await artifacts_api.get("nb_1", "missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_sources_get_warns_and_returns_none(self, sources_api):
        sources_api.list = AsyncMock(return_value=[])
        with pytest.warns(DeprecationWarning, match="SourceNotFoundError"):
            result = await sources_api.get("nb_1", "missing")
        assert result is None


# ---------------------------------------------------------------------------
# A found get() does NOT warn (only the miss path is deprecated)
# ---------------------------------------------------------------------------


class TestPublicGetDoesNotWarnOnHit:
    @pytest.mark.asyncio
    async def test_sources_get_hit_is_silent(self, sources_api):
        sources_api.list = AsyncMock(return_value=[Source(id="src_1", title="X")])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await sources_api.get("nb_1", "src_1")
        assert result is not None
        assert result.id == "src_1"

    @pytest.mark.asyncio
    async def test_artifacts_get_hit_is_silent(self, artifacts_api):
        found = MagicMock()
        found.id = "art_1"
        artifacts_api.list = AsyncMock(return_value=[found])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await artifacts_api.get("nb_1", "art_1")
        assert result is found

    @pytest.mark.asyncio
    async def test_notes_get_hit_is_silent(self, notes_api):
        # A note row whose id matches the requested note_id (item[0]).
        notes_api._get_all_notes_and_mind_maps = AsyncMock(
            return_value=[["note_1", ["note_1", "Body", None, None, "Title"]]]
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await notes_api.get("nb_1", "note_1")
        assert result is not None
        assert result.id == "note_1"


# ---------------------------------------------------------------------------
# The private _get_or_none() never warns (internal optional-lookup path)
# ---------------------------------------------------------------------------


class TestGetOrNoneNeverWarns:
    @pytest.mark.asyncio
    async def test_notes_get_or_none_silent_on_miss(self, notes_api):
        notes_api.list = AsyncMock(return_value=[])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await notes_api._get_or_none("nb_1", "missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_artifacts_get_or_none_silent_on_miss(self, artifacts_api):
        artifacts_api.list = AsyncMock(return_value=[])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await artifacts_api._get_or_none("nb_1", "missing")
        assert result is None

    @pytest.mark.asyncio
    async def test_sources_get_or_none_silent_on_miss(self, sources_api):
        sources_api.list = AsyncMock(return_value=[])
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            result = await sources_api._get_or_none("nb_1", "missing")
        assert result is None


# ---------------------------------------------------------------------------
# notebooks.get is UNCHANGED — still raises NotebookNotFoundError on a miss
# ---------------------------------------------------------------------------


class TestNotebooksGetStillRaises:
    @pytest.mark.asyncio
    async def test_notebooks_get_raises_not_found(self):
        from _fixtures.fake_core import make_fake_core
        from notebooklm._notebooks import NotebooksAPI
        from notebooklm.exceptions import NotebookNotFoundError

        # Empty/degenerate payload — the unknown-id shape notebooks.get guards.
        core = make_fake_core(rpc_call=AsyncMock(return_value=[[]]))
        api = NotebooksAPI(core.rpc_executor, sources_api=MagicMock())
        with warnings.catch_warnings():
            # No DeprecationWarning should fire on the raising path.
            warnings.simplefilter("error", DeprecationWarning)
            with pytest.raises(NotebookNotFoundError):
                await api.get("nb_missing")


# ---------------------------------------------------------------------------
# Internal callers must not self-warn — they go through _get_or_none.
# The CLI source-get service is the canonical internal not-found path; run it
# against a real SourcesAPI returning None with DeprecationWarning escalated to
# an error, proving the library never trips its own deprecation.
# ---------------------------------------------------------------------------


class TestNoInternalSelfWarn:
    @pytest.mark.asyncio
    async def test_cli_source_get_service_does_not_self_warn(self):
        from notebooklm.cli.services.source_content import (
            SourceGetPlan,
            execute_source_get,
        )

        client = MagicMock()
        # Real SourcesAPI bound to a mock core so _get_or_none runs for real.
        client.sources = SourcesAPI(MagicMock(), uploader=MagicMock())
        client.sources.list = AsyncMock(return_value=[])

        plan = SourceGetPlan(notebook_id="nb_1", source_id="missing")
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            # Would raise if execute_source_get called the warning-emitting
            # public get() instead of _get_or_none().
            result = await execute_source_get(client, plan)
        assert result.source is None
