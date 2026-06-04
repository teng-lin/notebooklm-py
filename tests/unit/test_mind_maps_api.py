"""Unit tests for the unified ``MindMapsAPI`` dispatch (issue #1256 Phase 2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._mind_maps_api import MindMapsAPI, extract_interactive_tree_leaf
from notebooklm.exceptions import (
    ArtifactError,
    ArtifactFeatureUnavailableError,
    MindMapNotFoundError,
    NotFoundError,
    UnknownRPCMethodError,
)
from notebooklm.rpc.types import RPCMethod
from notebooklm.types import Artifact, MindMapKind, MindMapResult


def _interactive_artifact(artifact_id: str, title: str = "INT") -> Artifact:
    return Artifact(id=artifact_id, title=title, _artifact_type=4, status=3, _variant=4)


def _pending_type4_artifact(artifact_id: str, title: str = "INT") -> Artifact:
    # Just-created interactive map: completed (status=3) but the variant slot
    # at [9][1][0] is not yet populated, so _variant reads None.
    return Artifact(id=artifact_id, title=title, _artifact_type=4, status=3, _variant=None)


def _make_api(*, note_rows=None, interactive=None):
    # ADR-0007: configure the rpc_call seam via MagicMock(...) construction
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
    # Auto-detect path: the id is in neither backing → MindMapNotFoundError.
    api, *_ = _make_api()
    with pytest.raises(MindMapNotFoundError, match="not found") as excinfo:
        await api.rename("nb", "ghost", "X")
    # Catchable via the cross-domain umbrella too (ADR-0019).
    assert isinstance(excinfo.value, NotFoundError)
    assert excinfo.value.mind_map_id == "ghost"


@pytest.mark.asyncio
async def test_rename_missing_raises_even_with_return_object_false():
    # Unlike sources/artifacts (whose absence detection rides on the skipped
    # hydrate re-fetch), mind maps detect absence via a list lookup *before*
    # dispatching the rename, so return_object=False does NOT suppress the raise.
    # Auto-detect (kind=None) path.
    api, _, _, artifacts, _ = _make_api()
    with pytest.raises(MindMapNotFoundError, match="not found"):
        await api.rename("nb", "ghost", "X", return_object=False)
    artifacts.rename.assert_not_awaited()


@pytest.mark.asyncio
async def test_rename_explicit_kind_missing_raises_even_with_return_object_false():
    # The pre-dispatch guarantee holds on the explicit-kind paths too: NOTE_BACKED
    # raises from rename_mind_map and INTERACTIVE raises from the _find_interactive
    # pre-validation, both before _hydrate_renamed is reached.
    api, _, mind_maps, artifacts, _ = _make_api()
    mind_maps.rename_mind_map = AsyncMock(side_effect=MindMapNotFoundError("ghost"))
    with pytest.raises(MindMapNotFoundError, match="ghost"):
        await api.rename("nb", "ghost", "X", kind=MindMapKind.NOTE_BACKED, return_object=False)

    with pytest.raises(MindMapNotFoundError, match="not found"):
        await api.rename("nb", "ghost", "X", kind=MindMapKind.INTERACTIVE, return_object=False)
    artifacts.rename.assert_not_awaited()  # never dispatched the no-op RPC


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
async def test_generate_interactive_raises_feature_unavailable_when_no_artifact_id():
    # ADR-0019 async-kickoff null contract (issue #1359): a null/degenerate
    # CREATE_ARTIFACT raises ArtifactFeatureUnavailableError (no task created),
    # matching the sibling generate_* / retry_failed null-create paths.
    api, rpc, *_ = _make_api()
    rpc.configure_mock(rpc_call=AsyncMock(return_value=None))  # CREATE_ARTIFACT yields no id
    with pytest.raises(ArtifactFeatureUnavailableError) as exc_info:
        await api.generate("nb", ["s1"], kind=MindMapKind.INTERACTIVE)
    # Carries the artifact-type + the CREATE_ARTIFACT method id for diagnostics.
    assert exc_info.value.artifact_type == "mind_map"
    assert exc_info.value.method_id == RPCMethod.CREATE_ARTIFACT.value
    # Non-breaking: still catchable as the broad ArtifactError (it is a subclass).
    assert isinstance(exc_info.value, ArtifactError)


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
    with pytest.raises(MindMapNotFoundError, match="not found"):
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
    with pytest.raises(MindMapNotFoundError, match="not found"):
        await api.rename("nb", "settling", "X", kind=MindMapKind.INTERACTIVE)
    artifacts.rename.assert_not_awaited()
    # Auto-detect (kind=None) also rejects it -> MindMapNotFoundError, never dispatched.
    with pytest.raises(MindMapNotFoundError, match="not found"):
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


@pytest.mark.asyncio
async def test_list_excludes_non_interactive_mind_map_artifacts():
    # A MIND_MAP-type artifact that is not a settled interactive map (variant
    # not yet populated) is filtered out of the unified listing.
    api, *_ = _make_api(
        note_rows=[["note_mm", "{}"]],
        interactive=[_pending_type4_artifact("settling"), _interactive_artifact("int_mm")],
    )
    ids = {m.id for m in await api.list("nb")}
    assert ids == {"note_mm", "int_mm"}  # "settling" excluded


# --- get() lookup ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_matching_mind_map():
    api, *_ = _make_api(note_rows=[["note_mm", '{"name": "NB", "children": []}']])
    # A hit is silent — only the miss path is deprecated (#1358).
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        mm = await api.get("nb", "note_mm")
    assert mm is not None
    assert mm.id == "note_mm"
    assert mm.kind == MindMapKind.NOTE_BACKED


@pytest.mark.asyncio
async def test_get_warns_and_returns_none_when_absent():
    # Neither backing contains the id -> None, but with a DeprecationWarning
    # naming the v0.8.0 MindMapNotFoundError flip (#1358 runway for #1247).
    api, *_ = _make_api(note_rows=[["note_mm", "{}"]])
    with pytest.warns(DeprecationWarning, match="MindMapNotFoundError"):
        assert await api.get("nb", "ghost") is None


@pytest.mark.asyncio
async def test_get_or_none_silent_on_miss():
    # The sanctioned None-on-miss path stays silent (#1358): no warning even
    # though it returns None for a missing id.
    import warnings

    api, *_ = _make_api(note_rows=[["note_mm", "{}"]])
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert await api.get_or_none("nb", "ghost") is None


# --- rename(kind=None) auto-detect dispatch ---------------------------------


@pytest.mark.asyncio
async def test_rename_auto_detect_note_backed_dispatches():
    # kind=None with the id in the note collection -> note-backed rename,
    # interactive rename never consulted.
    # Decoy first row exercises the loop-continue branch before the match.
    api, _, mind_maps, artifacts, _ = _make_api(
        note_rows=[["other_mm", "{}"], ["note_mm", '{"name": "NB", "children": []}']]
    )
    await api.rename("nb", "note_mm", "X", return_object=False)
    mind_maps.rename_mind_map.assert_awaited_once_with("nb", "note_mm", "X")
    artifacts.rename.assert_not_awaited()


@pytest.mark.asyncio
async def test_rename_auto_detect_interactive_dispatches():
    # kind=None with the id absent from notes but present as an interactive
    # artifact -> artifact rename.
    api, _, mind_maps, artifacts, _ = _make_api(interactive=[_interactive_artifact("int_mm")])
    await api.rename("nb", "int_mm", "Y", return_object=False)
    artifacts.rename.assert_awaited_once_with("nb", "int_mm", "Y", return_object=False)
    mind_maps.rename_mind_map.assert_not_awaited()


@pytest.mark.asyncio
async def test_rename_hydrate_raises_when_target_vanishes():
    # Explicit note-backed rename whose post-rename refetch finds nothing
    # (vanished-between-rename-and-refetch race) -> MindMapNotFoundError from
    # _hydrate_renamed rather than a stale/None object.
    # DeprecationWarning escalated to an error pins the #1358 self-warn fix:
    # _hydrate_renamed must re-fetch via the silent _get_or_none, not the
    # warning-emitting public get() (this would fail if it regressed).
    import warnings

    api, _, mind_maps, _, _ = _make_api(note_rows=[])
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with pytest.raises(MindMapNotFoundError, match="not found"):
            await api.rename("nb", "note_mm", "X", kind=MindMapKind.NOTE_BACKED)
    mind_maps.rename_mind_map.assert_awaited_once_with("nb", "note_mm", "X")


# --- get_tree auto-detect + note-backed paths -------------------------------


@pytest.mark.asyncio
async def test_get_tree_auto_detect_note_backed_returns_tree():
    # Decoy first row exercises the loop-continue branch before the match.
    api, *_ = _make_api(
        note_rows=[["other_mm", "{}"], ["note_mm", '{"name": "NB", "children": [1]}']]
    )
    assert await api.get_tree("nb", "note_mm") == {"name": "NB", "children": [1]}


@pytest.mark.asyncio
async def test_get_tree_auto_detect_interactive_falls_through_to_rpc():
    # kind=None, id absent from notes but present as an interactive artifact ->
    # falls through to GET_INTERACTIVE_HTML and parses the tree leaf.
    api, rpc, *_ = _make_api(interactive=[_interactive_artifact("int_mm")])
    row = [None] * 10
    row[9] = [None, None, None, '{"name": "INT", "children": []}']
    rpc.configure_mock(rpc_call=AsyncMock(return_value=[row]))
    assert await api.get_tree("nb", "int_mm") == {"name": "INT", "children": []}


@pytest.mark.asyncio
async def test_get_tree_auto_detect_missing_returns_none():
    # Derived read (ADR-0019): a missing parent yields the uniform-empty value
    # (None), not a raise — get() is the existence check, not get_tree().
    api, rpc, *_ = _make_api()
    assert await api.get_tree("nb", "ghost") is None
    # Auto-detect resolved the miss without falling through to GET_INTERACTIVE_HTML.
    rpc.rpc_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_tree_note_backed_absent_returns_none():
    # Explicit kind=NOTE_BACKED with an unknown id returns None (does not raise).
    api, *_ = _make_api(note_rows=[["other", "{}"]])
    assert await api.get_tree("nb", "ghost", kind=MindMapKind.NOTE_BACKED) is None


@pytest.mark.asyncio
async def test_get_tree_note_backed_invalid_json_returns_none():
    # _parse_tree swallows a JSONDecodeError on malformed content -> None.
    api, *_ = _make_api(note_rows=[["note_mm", "not-json{"]])
    assert await api.get_tree("nb", "note_mm", kind=MindMapKind.NOTE_BACKED) is None


# --- _detect_kind via delete(kind=None) -------------------------------------


@pytest.mark.asyncio
async def test_delete_auto_detect_note_backed():
    # Decoy first row exercises the _detect_kind loop-continue branch.
    api, _, mind_maps, artifacts, _ = _make_api(note_rows=[["other_mm", "{}"], ["note_mm", "{}"]])
    await api.delete("nb", "note_mm")
    mind_maps.delete_mind_map.assert_awaited_once_with("nb", "note_mm")
    artifacts.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_auto_detect_interactive():
    api, _, mind_maps, artifacts, _ = _make_api(interactive=[_interactive_artifact("int_mm")])
    await api.delete("nb", "int_mm")
    artifacts.delete.assert_awaited_once_with("nb", "int_mm")
    mind_maps.delete_mind_map.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_auto_detect_missing_is_idempotent():
    # Auto-detect (kind=None) on an already-absent id is a no-op that returns
    # None (ADR-0019 idempotent delete), matching sources/artifacts/notes —
    # not a raise. Neither delete RPC family is dispatched.
    api, _, mind_maps, artifacts, _ = _make_api()
    assert await api.delete("nb", "ghost") is None
    mind_maps.delete_mind_map.assert_not_awaited()
    artifacts.delete.assert_not_awaited()
