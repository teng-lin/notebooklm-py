"""Transport-neutral tests for the unified mind-map base class."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._android.mind_maps import AndroidMindMapsAPI
from notebooklm._mind_maps_api import MindMapsAPI
from notebooklm._web.mind_maps import WebMindMapsAPI
from notebooklm.types import Artifact, MindMap, MindMapKind


class _FakeMindMapsAPI(MindMapsAPI):
    """Minimal backend proving shared workflows need only the rename hook."""

    def __init__(
        self,
        *,
        note_backed: list[MindMap],
        artifacts: Any,
        notes: Any,
    ) -> None:
        super().__init__(artifacts=artifacts, notes=notes)
        self._note_backed = note_backed
        self.renames: list[tuple[str, str, str]] = []

    async def list_note_backed(self, notebook_id: str) -> list[MindMap]:
        return list(self._note_backed)

    async def _list_studio_mind_map_rows(self, notebook_id: str) -> list[Artifact]:
        return await self._artifacts.list(notebook_id)

    async def _start_interactive_mind_map(
        self,
        notebook_id: str,
        source_ids: list[str] | None,
        *,
        language: str | None,
        instructions: str | None,
    ) -> str:
        raise NotImplementedError

    async def _read_interactive_tree(
        self,
        notebook_id: str,
        mind_map_id: str,
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    async def _send_rename_note_backed(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
    ) -> None:
        self.renames.append((notebook_id, mind_map_id, new_title))


def _api(*, note_backed: list[MindMap] | None = None, artifacts: list[Artifact] | None = None):
    artifact_api = MagicMock()
    artifact_api.list = AsyncMock(return_value=artifacts or [])
    artifact_api.rename = AsyncMock()
    artifact_api.delete = AsyncMock()
    notes = MagicMock()
    notes.delete_mind_map = AsyncMock()
    return (
        _FakeMindMapsAPI(
            note_backed=note_backed or [],
            artifacts=artifact_api,
            notes=notes,
        ),
        artifact_api,
        notes,
    )


@pytest.mark.asyncio
async def test_list_composes_note_backed_and_interactive_maps() -> None:
    note_map = MindMap(
        id="note-map",
        notebook_id="nb",
        title="Note map",
        kind=MindMapKind.NOTE_BACKED,
    )
    interactive = Artifact(
        id="interactive",
        title="Interactive",
        _artifact_type=4,
        status=3,
        _variant=4,
    )
    api, artifacts, _ = _api(note_backed=[note_map], artifacts=[interactive])

    result = await api.list("nb")

    assert [item.id for item in result] == ["note-map", "interactive"]
    artifacts.list.assert_awaited_once_with("nb")


@pytest.mark.asyncio
async def test_generate_preserves_string_enum_compatibility_for_note_backed_kind() -> None:
    api, artifacts, _ = _api()
    artifacts.generate_mind_map = AsyncMock(
        return_value=SimpleNamespace(
            note_id="note-map",
            created_at=None,
            mind_map={"name": "Legacy string kind"},
        )
    )

    result = await api.generate(
        "nb",
        ["source"],
        kind=cast(Any, "note_backed"),
    )

    assert result.id == "note-map"
    assert result.kind == MindMapKind.NOTE_BACKED
    artifacts.generate_mind_map.assert_awaited_once_with("nb", ["source"], "en", None)


@pytest.mark.asyncio
async def test_rename_dispatches_note_backed_through_the_sole_hook() -> None:
    note_map = MindMap(
        id="note-map",
        notebook_id="nb",
        title="Note map",
        kind=MindMapKind.NOTE_BACKED,
    )
    api, artifacts, _ = _api(note_backed=[note_map])

    assert await api.rename("nb", "note-map", "Renamed", return_object=False) is None

    assert api.renames == [("nb", "note-map", "Renamed")]
    artifacts.rename.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_dispatches_through_neutral_notes_and_artifacts() -> None:
    api, artifacts, notes = _api()

    await api.delete("nb", "note-map", kind=MindMapKind.NOTE_BACKED)
    await api.delete("nb", "interactive", kind=MindMapKind.INTERACTIVE)

    notes.delete_mind_map.assert_awaited_once_with("nb", "note-map")
    artifacts.delete.assert_awaited_once_with("nb", "interactive")


def test_web_backend_inherits_every_base_concrete_workflow_and_its_docs() -> None:
    concrete = {"list", "get", "get_or_none", "generate", "get_tree", "rename", "delete"}
    for method_name in concrete:
        assert method_name not in WebMindMapsAPI.__dict__
        assert getattr(WebMindMapsAPI, method_name) is getattr(MindMapsAPI, method_name)
        assert (
            getattr(WebMindMapsAPI, method_name).__doc__
            == getattr(MindMapsAPI, method_name).__doc__
        )

    for method_name in {"list_note_backed"}:
        assert (
            getattr(WebMindMapsAPI, method_name).__doc__
            == getattr(MindMapsAPI, method_name).__doc__
        )

    assert WebMindMapsAPI.__doc__ == MindMapsAPI.__doc__


def test_exact_abstract_set_and_frontends_are_concrete() -> None:
    assert MindMapsAPI.__abstractmethods__ == frozenset(
        {
            "_list_studio_mind_map_rows",
            "_read_interactive_tree",
            "_send_rename_note_backed",
            "_start_interactive_mind_map",
            "list_note_backed",
        }
    )
    assert WebMindMapsAPI.__abstractmethods__ == frozenset()
    assert AndroidMindMapsAPI.__abstractmethods__ == frozenset()


def test_android_backend_inherits_neutral_workflows() -> None:
    for method_name in {"delete", "generate", "get", "get_or_none", "get_tree", "list", "rename"}:
        assert method_name not in AndroidMindMapsAPI.__dict__
        assert getattr(AndroidMindMapsAPI, method_name) is getattr(MindMapsAPI, method_name)


def test_interactive_wait_failure_policy_preserves_each_backend_contract() -> None:
    assert MindMapsAPI._reject_unsuccessful_interactive_wait is False
    assert WebMindMapsAPI._reject_unsuccessful_interactive_wait is False
    assert AndroidMindMapsAPI._reject_unsuccessful_interactive_wait is True


def test_android_rename_inherits_the_scoped_base_workflow() -> None:
    assert "rename" not in AndroidMindMapsAPI.__dict__
    assert AndroidMindMapsAPI.rename is MindMapsAPI.rename


def test_default_client_assembly_keeps_the_web_frontend() -> None:
    import notebooklm
    import notebooklm._client_assembly as assembly

    assert assembly.WebMindMapsAPI is WebMindMapsAPI
    assert "AndroidMindMapsAPI" not in assembly.__dict__
    assert not hasattr(notebooklm, "AndroidMindMapsAPI")
