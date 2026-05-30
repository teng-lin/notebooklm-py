"""Unit tests for the unified ``MindMapsAPI`` dispatch (issue #1256 Phase 2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._mind_maps_api import MindMapsAPI
from notebooklm.exceptions import ArtifactError
from notebooklm.rpc.types import RPCMethod
from notebooklm.types import Artifact, MindMapKind, MindMapResult


def _interactive_artifact(artifact_id: str, title: str = "INT") -> Artifact:
    return Artifact(id=artifact_id, title=title, _artifact_type=4, status=3, _variant=4)


def _make_api(*, note_rows=None, interactive=None):
    # ADR-007: configure the rpc_call seam via MagicMock(...) construction
    # keyword (and configure_mock(...) for per-test overrides below) rather
    # than dotted AsyncMock attribute assignment, which the forbidden-
    # monkeypatch lint rejects on the rpc_call seam.
    rpc = MagicMock(rpc_call=AsyncMock(return_value=None))
    mind_maps = MagicMock()
    mind_maps.list_mind_maps = AsyncMock(return_value=note_rows or [])
    mind_maps.extract_content = MagicMock(side_effect=lambda row: row[1])
    mind_maps.rename_mind_map = AsyncMock()
    mind_maps.delete_mind_map = AsyncMock(return_value=True)
    artifacts = MagicMock()
    artifacts.list = AsyncMock(return_value=interactive or [])
    artifacts.rename = AsyncMock()
    artifacts.delete = AsyncMock(return_value=True)
    artifacts.generate_mind_map = AsyncMock()
    artifacts.wait_for_completion = AsyncMock()
    notebooks = MagicMock()
    notebooks.get_source_ids = AsyncMock(return_value=["s1"])
    api = MindMapsAPI(rpc=rpc, mind_maps=mind_maps, artifacts=artifacts, notebooks=notebooks)
    return api, rpc, mind_maps, artifacts, notebooks


@pytest.mark.asyncio
async def test_list_unions_both_backings():
    api, *_ = _make_api(
        note_rows=[["note_mm", '{"name": "NB", "children": []}']],
        interactive=[_interactive_artifact("int_mm")],
    )
    result = await api.list("nb")
    by_id = {m.id: m for m in result}
    assert by_id["note_mm"].kind == MindMapKind.NOTE_BACKED
    assert by_id["note_mm"].tree == {"name": "NB", "children": []}
    assert by_id["int_mm"].kind == MindMapKind.INTERACTIVE
    assert by_id["int_mm"].tree is None  # interactive tree fetched lazily via get_tree


@pytest.mark.asyncio
async def test_rename_dispatches_by_kind():
    api, _, mind_maps, artifacts, _ = _make_api()
    await api.rename("nb", "note_mm", "X", kind=MindMapKind.NOTE_BACKED)
    mind_maps.rename_mind_map.assert_awaited_once_with("nb", "note_mm", "X")
    artifacts.rename.assert_not_awaited()

    await api.rename("nb", "int_mm", "Y", kind=MindMapKind.INTERACTIVE)
    artifacts.rename.assert_awaited_once_with("nb", "int_mm", "Y")


@pytest.mark.asyncio
async def test_delete_dispatches_by_kind():
    api, _, mind_maps, artifacts, _ = _make_api()
    assert await api.delete("nb", "note_mm", kind=MindMapKind.NOTE_BACKED) is True
    mind_maps.delete_mind_map.assert_awaited_once_with("nb", "note_mm")
    assert await api.delete("nb", "int_mm", kind=MindMapKind.INTERACTIVE) is True
    artifacts.delete.assert_awaited_once_with("nb", "int_mm")


@pytest.mark.asyncio
async def test_get_tree_note_backed_parses_content():
    api, *_ = _make_api(note_rows=[["note_mm", '{"name": "NB", "children": [1]}']])
    tree = await api.get_tree("nb", "note_mm", kind=MindMapKind.NOTE_BACKED)
    assert tree == {"name": "NB", "children": [1]}


@pytest.mark.asyncio
async def test_get_tree_interactive_reads_v9rmvd_position():
    api, rpc, *_ = _make_api()
    row = [None] * 10
    row[9] = [None, None, None, '{"name": "I", "children": []}']  # [0][9][3] = tree
    rpc.configure_mock(rpc_call=AsyncMock(return_value=[row]))
    tree = await api.get_tree("nb", "int_mm", kind=MindMapKind.INTERACTIVE)
    assert tree == {"name": "I", "children": []}
    assert rpc.rpc_call.call_args[0][0] == RPCMethod.GET_INTERACTIVE_HTML


@pytest.mark.asyncio
async def test_generate_note_backed_delegates():
    api, _, _, artifacts, _ = _make_api()
    artifacts.generate_mind_map = AsyncMock(
        return_value=MindMapResult(mind_map={"name": "G", "children": []}, note_id="n1")
    )
    mm = await api.generate("nb", ["s1"], kind=MindMapKind.NOTE_BACKED)
    assert mm.kind == MindMapKind.NOTE_BACKED
    assert mm.id == "n1"
    assert mm.title == "G"
    assert mm.tree == {"name": "G", "children": []}


@pytest.mark.asyncio
async def test_generate_interactive_creates_and_polls():
    api, rpc, _, artifacts, notebooks = _make_api(
        interactive=[_interactive_artifact("new_int", "T")]
    )
    rpc.configure_mock(
        rpc_call=AsyncMock(return_value=[["new_int", "T", 4]])  # CREATE_ARTIFACT echo
    )
    mm = await api.generate("nb", kind=MindMapKind.INTERACTIVE, wait=True)
    assert rpc.rpc_call.call_args[0][0] == RPCMethod.CREATE_ARTIFACT
    notebooks.get_source_ids.assert_awaited_once_with("nb")  # source ids resolved
    artifacts.wait_for_completion.assert_awaited_once_with("nb", "new_int")
    assert mm.kind == MindMapKind.INTERACTIVE
    assert mm.id == "new_int"


@pytest.mark.asyncio
async def test_generate_interactive_raises_when_no_artifact_id():
    api, rpc, *_ = _make_api()
    rpc.configure_mock(rpc_call=AsyncMock(return_value=None))  # CREATE_ARTIFACT yields no id
    with pytest.raises(ArtifactError, match="no artifact id"):
        await api.generate("nb", ["s1"], kind=MindMapKind.INTERACTIVE)


@pytest.mark.asyncio
async def test_detect_kind_raises_when_absent():
    api, *_ = _make_api()
    with pytest.raises(ValueError, match="not found"):
        await api.rename("nb", "ghost", "X")
