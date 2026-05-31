"""Unit tests for the unified ``MindMapsAPI`` dispatch (issue #1256 Phase 2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._mind_maps_api import MindMapsAPI, extract_interactive_tree_leaf
from notebooklm.exceptions import ArtifactError, UnknownRPCMethodError
from notebooklm.rpc.types import RPCMethod
from notebooklm.types import Artifact, MindMapKind, MindMapResult


def _interactive_artifact(artifact_id: str, title: str = "INT") -> Artifact:
    return Artifact(id=artifact_id, title=title, _artifact_type=4, status=3, _variant=4)


def _pending_type4_artifact(artifact_id: str, title: str = "INT") -> Artifact:
    # Just-created interactive map: completed (status=3) but the variant slot
    # at [9][1][0] is not yet populated, so _variant reads None.
    return Artifact(id=artifact_id, title=title, _artifact_type=4, status=3, _variant=None)


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
    # The explicit-interactive path pre-validates the id (issue #1270), so the
    # interactive artifact must exist for the rename to dispatch.
    # return_object=False keeps this focused on dispatch (no hydrate re-fetch).
    api, _, mind_maps, artifacts, _ = _make_api(interactive=[_interactive_artifact("int_mm")])
    assert (
        await api.rename("nb", "note_mm", "X", kind=MindMapKind.NOTE_BACKED, return_object=False)
        is None
    )
    mind_maps.rename_mind_map.assert_awaited_once_with("nb", "note_mm", "X")
    artifacts.rename.assert_not_awaited()

    await api.rename("nb", "int_mm", "Y", kind=MindMapKind.INTERACTIVE, return_object=False)
    # The interactive artifact rename is delegated with return_object=False so
    # the unified API hydrates once (not twice) when an object is requested.
    artifacts.rename.assert_awaited_once_with("nb", "int_mm", "Y", return_object=False)


@pytest.mark.asyncio
async def test_rename_returns_renamed_mind_map():
    # Note-backed: the post-rename list reflects the new title; rename returns it.
    # Current row shape carries the title in the inner envelope at row[1][4]
    # (see NoteRow.title); extract_content reads row[1] (the JSON tree string).
    api, _, mind_maps, artifacts, _ = _make_api(
        note_rows=[
            [
                "note_mm",
                ["note_mm", '{"name": "NB", "children": []}', None, None, "New Title"],
            ]
        ]
    )
    result = await api.rename("nb", "note_mm", "New Title", kind=MindMapKind.NOTE_BACKED)
    assert result is not None
    assert result.id == "note_mm"
    assert result.kind == MindMapKind.NOTE_BACKED
    # Server-reflected title (NoteRow.title slot), not the input echoed back —
    # guards against re-fetching a stale row with the old title.
    assert result.title == "New Title"


@pytest.mark.asyncio
async def test_rename_missing_raises():
    # Auto-detect path: the id is in neither backing → ValueError.
    api, *_ = _make_api()
    with pytest.raises(ValueError, match="not found"):
        await api.rename("nb", "ghost", "X")


@pytest.mark.asyncio
async def test_delete_dispatches_by_kind():
    api, _, mind_maps, artifacts, _ = _make_api()
    assert await api.delete("nb", "note_mm", kind=MindMapKind.NOTE_BACKED) is None
    mind_maps.delete_mind_map.assert_awaited_once_with("nb", "note_mm")
    assert await api.delete("nb", "int_mm", kind=MindMapKind.INTERACTIVE) is None
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
async def test_generate_interactive_creates_polls_and_fetches_tree():
    api, rpc, _, artifacts, notebooks = _make_api(
        interactive=[_interactive_artifact("new_int", "T")]
    )
    tree_row = [None] * 10
    tree_row[9] = [None, None, None, '{"name": "I", "children": []}']  # [0][9][3] = tree
    rpc.configure_mock(
        rpc_call=AsyncMock(
            side_effect=[
                [["new_int", "T", 4]],  # 1: CREATE_ARTIFACT echo
                [tree_row],  # 2: GET_INTERACTIVE_HTML tree (post-completion)
            ]
        )
    )
    mm = await api.generate("nb", kind=MindMapKind.INTERACTIVE, wait=True)
    assert rpc.rpc_call.call_args_list[0][0][0] == RPCMethod.CREATE_ARTIFACT
    notebooks.get_source_ids.assert_awaited_once_with("nb")  # source ids resolved
    artifacts.wait_for_completion.assert_awaited_once_with("nb", "new_int")
    assert mm.kind == MindMapKind.INTERACTIVE
    assert mm.id == "new_int"
    # Converged surface: interactive generate returns the tree, like note-backed.
    assert mm.tree == {"name": "I", "children": []}


@pytest.mark.asyncio
async def test_generate_interactive_wait_false_skips_tree():
    api, rpc, _, artifacts, _ = _make_api(interactive=[_interactive_artifact("new_int")])
    rpc.configure_mock(rpc_call=AsyncMock(return_value=[["new_int", "T", 4]]))
    mm = await api.generate("nb", ["s1"], kind=MindMapKind.INTERACTIVE, wait=False)
    assert mm.tree is None  # pending; no tree fetched
    artifacts.wait_for_completion.assert_not_awaited()
    assert rpc.rpc_call.await_count == 1  # only CREATE_ARTIFACT, no get_tree


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


# --- #1270 sub-fix 1: get_tree drift vs absent-leaf ---------------------------


@pytest.mark.asyncio
async def test_get_tree_interactive_absent_leaf_tolerated_with_warning(caplog):
    """A populated options block missing only the [3] tree leaf is 'not ready'."""
    api, rpc, *_ = _make_api()
    row = [None] * 10
    row[9] = [None, None]  # [0][9] present but too short to carry the [3] leaf
    rpc.configure_mock(rpc_call=AsyncMock(return_value=[row]))
    import logging

    with caplog.at_level(logging.WARNING, logger="notebooklm._mind_maps_api"):
        tree = await api.get_tree("nb", "int_mm", kind=MindMapKind.INTERACTIVE)
    assert tree is None  # tolerated as not-yet-populated
    # ...but a WARNING with the rpcid/source leaves a drift breadcrumb.
    assert any(RPCMethod.GET_INTERACTIVE_HTML.value in r.message for r in caplog.records)
    assert any("_mind_maps_api.get_tree" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_get_tree_interactive_real_drift_reraises():
    """Genuine [0][9] reshape must fail loud, not masquerade as 'not ready'."""
    api, rpc, *_ = _make_api()
    # [0] is a short row: descent to [0][9] fails before reaching the leaf.
    rpc.configure_mock(rpc_call=AsyncMock(return_value=[[1, 2, 3]]))
    with pytest.raises(UnknownRPCMethodError):
        await api.get_tree("nb", "int_mm", kind=MindMapKind.INTERACTIVE)


@pytest.mark.asyncio
async def test_get_tree_interactive_null_response_returns_none():
    """A null GET_INTERACTIVE_HTML response stays 'not ready' (no drift)."""
    api, rpc, *_ = _make_api()
    rpc.configure_mock(rpc_call=AsyncMock(return_value=None))
    assert await api.get_tree("nb", "int_mm", kind=MindMapKind.INTERACTIVE) is None


def test_extract_interactive_tree_leaf_helper():
    """The shared helper: null -> None, drift -> raise, present -> value."""
    assert extract_interactive_tree_leaf(None, source="t") is None
    row = [None] * 10
    row[9] = [None, None, None, "TREE"]
    assert extract_interactive_tree_leaf([row], source="t") == "TREE"
    # [0] too short to reach [0][9] -> drift.
    with pytest.raises(UnknownRPCMethodError):
        extract_interactive_tree_leaf([[1, 2, 3]], source="t")
    # A non-list [0][9] is drift too (not a tolerated short-list).
    non_list_row = [None] * 10
    non_list_row[9] = "not-a-list"
    with pytest.raises(UnknownRPCMethodError):
        extract_interactive_tree_leaf([non_list_row], source="t")
    # A list [0][9] that is too short for index 3 is the tolerated not-ready leaf.
    short_row = [None] * 10
    short_row[9] = [None, None]
    assert extract_interactive_tree_leaf([short_row], source="t") is None


# --- #1270 sub-fix 2: transient type-4 variant=None classification -----------


@pytest.mark.asyncio
async def test_find_interactive_matches_pending_variant_none_by_id():
    """generate(wait=True) must keep the real title during the settling window."""
    api, rpc, _, artifacts, _ = _make_api(
        interactive=[_pending_type4_artifact("new_int", "Real Title")]
    )
    tree_row = [None] * 10
    tree_row[9] = [None, None, None, '{"name": "I", "children": []}']
    rpc.configure_mock(
        rpc_call=AsyncMock(
            side_effect=[
                [["new_int", "Real Title", 4]],  # CREATE_ARTIFACT echo
                [tree_row],  # GET_INTERACTIVE_HTML tree
            ]
        )
    )
    mm = await api.generate("nb", kind=MindMapKind.INTERACTIVE, wait=True)
    assert mm.id == "new_int"
    # Did NOT degrade to the title="Mind Map" placeholder.
    assert mm.title == "Real Title"
    assert mm.tree == {"name": "I", "children": []}


@pytest.mark.asyncio
async def test_generate_interactive_unresolved_id_falls_back_to_placeholder():
    """If even the unfiltered list never shows the id, fall back gracefully."""
    api, rpc, _, artifacts, _ = _make_api(interactive=[])
    rpc.configure_mock(
        rpc_call=AsyncMock(
            side_effect=[
                [["ghost_int", "T", 4]],  # CREATE_ARTIFACT echo
                None,  # GET_INTERACTIVE_HTML tree not ready
            ]
        )
    )
    mm = await api.generate("nb", kind=MindMapKind.INTERACTIVE, wait=True)
    assert mm.id == "ghost_int"
    assert mm.title == "Mind Map"  # placeholder fallback preserved


# --- #1270 sub-fix 3: rename(kind=INTERACTIVE) pre-validates the id ----------


@pytest.mark.asyncio
async def test_rename_interactive_bad_id_raises_not_silent_noop():
    api, _, _, artifacts, _ = _make_api(interactive=[_interactive_artifact("real_int")])
    with pytest.raises(ValueError, match="not found"):
        await api.rename("nb", "ghost", "X", kind=MindMapKind.INTERACTIVE)
    artifacts.rename.assert_not_awaited()  # never dispatched the no-op RPC


@pytest.mark.asyncio
async def test_rename_interactive_good_id_dispatches():
    api, _, _, artifacts, _ = _make_api(interactive=[_interactive_artifact("real_int")])
    await api.rename("nb", "real_int", "X", kind=MindMapKind.INTERACTIVE, return_object=False)
    # The unified API delegates with return_object=False (it hydrates once, here
    # skipped) — the artifact rename is not asked to re-fetch.
    artifacts.rename.assert_awaited_once_with("nb", "real_int", "X", return_object=False)


@pytest.mark.asyncio
async def test_rename_interactive_rejects_settling_type4_variant_none():
    """The unclassified-type4 fallback is scoped to generate only: rename/detect
    must NOT accept a settling (or malformed) quiz/flashcard as a mind map."""
    api, _, _, artifacts, _ = _make_api(interactive=[_pending_type4_artifact("settling")])
    # Explicit-interactive rename: the strict path rejects a variant=None row.
    with pytest.raises(ValueError, match="not found"):
        await api.rename("nb", "settling", "X", kind=MindMapKind.INTERACTIVE)
    artifacts.rename.assert_not_awaited()
    # Auto-detect (kind=None) also rejects it -> ValueError, never dispatched.
    with pytest.raises(ValueError, match="not found"):
        await api.rename("nb", "settling", "X")
    artifacts.rename.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_tree_interactive_non_list_options_block_reraises():
    """A non-list [0][9] is genuine drift -> fail loud, not 'not ready'."""
    api, rpc, *_ = _make_api()
    row = [None] * 10
    row[9] = "not-a-list"  # [0][9] is no longer a list -> drift
    rpc.configure_mock(rpc_call=AsyncMock(return_value=[row]))
    with pytest.raises(UnknownRPCMethodError):
        await api.get_tree("nb", "int_mm", kind=MindMapKind.INTERACTIVE)


# --- #1270 sub-fix 4: non-str title coercion ---------------------------------


@pytest.mark.asyncio
async def test_generate_note_backed_non_str_name_falls_back_to_placeholder():
    api, _, _, artifacts, _ = _make_api()
    artifacts.generate_mind_map = AsyncMock(
        return_value=MindMapResult(mind_map={"name": 123, "children": []}, note_id="n1")
    )
    mm = await api.generate("nb", ["s1"], kind=MindMapKind.NOTE_BACKED)
    assert mm.title == "Mind Map"  # numeric name rejected, placeholder used


@pytest.mark.asyncio
async def test_generate_note_backed_empty_name_falls_back_to_placeholder():
    api, _, _, artifacts, _ = _make_api()
    artifacts.generate_mind_map = AsyncMock(
        return_value=MindMapResult(mind_map={"name": "", "children": []}, note_id="n1")
    )
    mm = await api.generate("nb", ["s1"], kind=MindMapKind.NOTE_BACKED)
    assert mm.title == "Mind Map"  # empty name rejected, placeholder used
