"""Phase-1: recognition of interactive (studio-artifact) mind maps.

The web GUI now generates an *interactive* mind map as a studio artifact in the
type-4 (QUIZ) family with variant 4 — distinct from the note-backed mind map the
library surfaces with the synthetic type code 5. These tests pin the wire
recognition: kind mapping, the listing-filter union, the `is_interactive_mind_map`
discriminator, and downloading the interactive tree via GET_INTERACTIVE_HTML.
See issue #1256.
"""

from __future__ import annotations

import json
import warnings

import pytest

from notebooklm._artifact.listing import _matches_artifact_type
from notebooklm._types.artifacts import _map_artifact_kind, _warned_artifact_types
from notebooklm._types.common import UnknownTypeWarning
from notebooklm.rpc.types import INTERACTIVE_MIND_MAP_VARIANT
from notebooklm.types import Artifact, ArtifactType


@pytest.fixture(autouse=True)
def _clear_warned_set():
    # `_warned_artifact_types` is a module-level set: a warning fires only once
    # per (type, variant) for the whole session, so reset around each test or
    # the `pytest.warns`/no-warning assertions become order-dependent (P1.c).
    _warned_artifact_types.clear()
    yield
    _warned_artifact_types.clear()


def _art(type_code: int, variant: int | None = None) -> Artifact:
    return Artifact(id="art_1", title="MM", _artifact_type=type_code, status=3, _variant=variant)


# --- T1.1: the constant -------------------------------------------------------


def test_interactive_mind_map_variant_constant():
    assert INTERACTIVE_MIND_MAP_VARIANT == 4
    from notebooklm.rpc import INTERACTIVE_MIND_MAP_VARIANT as reexported

    assert reexported == 4


# --- T1.2: kind mapping -------------------------------------------------------


def test_variant_4_maps_to_mind_map_without_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error", UnknownTypeWarning)  # any warning → test failure
        assert _map_artifact_kind(4, 4) == ArtifactType.MIND_MAP


def test_quiz_and_flashcards_variants_unchanged():
    assert _map_artifact_kind(4, 1) == ArtifactType.FLASHCARDS
    assert _map_artifact_kind(4, 2) == ArtifactType.QUIZ


@pytest.mark.parametrize("variant", [3, None])
def test_other_type4_variants_still_warn_unknown(variant):
    with pytest.warns(UnknownTypeWarning):
        assert _map_artifact_kind(4, variant) == ArtifactType.UNKNOWN


# --- T1.4: the discriminator --------------------------------------------------


def test_is_interactive_mind_map_property():
    assert _art(4, 4).is_interactive_mind_map is True
    assert _art(4, 2).is_interactive_mind_map is False  # quiz
    assert _art(4, 1).is_interactive_mind_map is False  # flashcards
    assert _art(5, None).is_interactive_mind_map is False  # note-backed synthetic


# --- T1.3: listing-filter union ----------------------------------------------


def test_list_mind_map_matches_both_backings():
    assert _matches_artifact_type(_art(5, None), ArtifactType.MIND_MAP)  # note-backed
    assert _matches_artifact_type(_art(4, 4), ArtifactType.MIND_MAP)  # interactive
    assert not _matches_artifact_type(_art(4, 2), ArtifactType.MIND_MAP)  # quiz


def test_list_unknown_excludes_interactive_but_keeps_genuine_unknown():
    assert not _matches_artifact_type(_art(4, 4), ArtifactType.UNKNOWN)
    assert _matches_artifact_type(_art(4, 3), ArtifactType.UNKNOWN)  # genuine unknown variant


# --- T1.5: download guard -----------------------------------------------------

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from notebooklm._artifact.downloads import ArtifactDownloadService  # noqa: E402
from notebooklm._runtime.contracts import RpcCaller  # noqa: E402
from notebooklm.rpc.types import RPCMethod  # noqa: E402
from notebooklm.types import ArtifactNotReadyError  # noqa: E402

# Raw studio row whose [9][1][0] == 4 → Artifact.is_interactive_mind_map is True.
_INTERACTIVE_ROW = ["int_mm", "MM", 4, None, 3, None, None, None, None, [None, [4]]]


def _download_service(studio_rows, note_rows, *, interactive_tree=None):
    listing = MagicMock()
    listing.list_raw = AsyncMock(return_value=studio_rows)
    mind_maps = MagicMock()
    mind_maps.list_mind_maps = AsyncMock(return_value=note_rows)
    mind_maps.extract_content = MagicMock(side_effect=lambda row: row[1])
    if interactive_tree is not None:
        # GET_INTERACTIVE_HTML response: the JSON tree lives at [0][9][3].
        response = [[None] * 9 + [[None, None, None, interactive_tree]]]
    else:
        # An empty GET_INTERACTIVE_HTML response: the service reads an absent
        # tree as "not ready" via the real _get_interactive_mind_map_tree path
        # (no method monkeypatch — ADR-0007).
        response = None
    # Wire rpc_call via the MagicMock constructor (not post-hoc attribute
    # assignment) so the ADR-0007 meta-lint stays clean.
    rpc = MagicMock(spec=RpcCaller, rpc_call=AsyncMock(return_value=response))
    return ArtifactDownloadService(rpc=rpc, listing=listing, mind_maps=mind_maps)


@pytest.mark.asyncio
async def test_download_interactive_id_with_zero_note_backed_maps(tmp_path):
    """The common interactive-only case: fetch the tree via GET_INTERACTIVE_HTML."""
    tree = '{"name": "Root", "children": [{"name": "A"}]}'
    svc = _download_service(studio_rows=[_INTERACTIVE_ROW], note_rows=[], interactive_tree=tree)
    out = str(tmp_path / "x.json")
    result = await svc.download_mind_map("nb", out, artifact_id="int_mm")
    assert result == out
    assert json.loads((tmp_path / "x.json").read_text(encoding="utf-8")) == {
        "name": "Root",
        "children": [{"name": "A"}],
    }
    # Lock the retrieval contract: the tree must come from GET_INTERACTIVE_HTML
    # addressed to this artifact id (not some other RPC / id that happens to
    # yield the same parsed tree).
    method, params = svc._rpc.rpc_call.await_args.args[:2]
    assert method is RPCMethod.GET_INTERACTIVE_HTML
    assert params == ["int_mm"]
    assert svc._rpc.rpc_call.await_args.kwargs["source_path"] == "/notebook/nb"


@pytest.mark.asyncio
async def test_download_interactive_id_with_unrelated_note_backed_maps(tmp_path):
    """Interactive id while other note-backed maps exist: still downloads the interactive tree."""
    note = ["other_note", '{"name": "x", "children": []}']
    tree = '{"name": "Root", "children": []}'
    svc = _download_service(studio_rows=[_INTERACTIVE_ROW], note_rows=[note], interactive_tree=tree)
    out = str(tmp_path / "x.json")
    result = await svc.download_mind_map("nb", out, artifact_id="int_mm")
    assert result == out
    assert json.loads((tmp_path / "x.json").read_text(encoding="utf-8")) == {
        "name": "Root",
        "children": [],
    }


@pytest.mark.asyncio
async def test_download_interactive_id_tree_not_ready_raises(tmp_path):
    """Interactive artifact present but its tree is not yet readable -> not ready."""
    # No interactive_tree wired -> GET_INTERACTIVE_HTML returns an empty
    # response, which the real _get_interactive_mind_map_tree maps to None; the
    # service treats an absent tree as "not ready" rather than "not found".
    svc = _download_service(studio_rows=[_INTERACTIVE_ROW], note_rows=[])
    with pytest.raises(ArtifactNotReadyError):
        await svc.download_mind_map("nb", str(tmp_path / "x.json"), artifact_id="int_mm")


@pytest.mark.asyncio
async def test_download_note_backed_id_still_works(tmp_path):
    """The guard must not disturb genuine note-backed downloads."""
    content = '{"name": "Root", "children": []}'
    svc = _download_service(studio_rows=[], note_rows=[["note_mm", content]])
    out = str(tmp_path / "mm.json")
    result = await svc.download_mind_map("nb", out, artifact_id="note_mm")
    assert result == out
    assert json.loads((tmp_path / "mm.json").read_text(encoding="utf-8")) == {
        "name": "Root",
        "children": [],
    }


# --- #1270: download fail-loud + numeric-id seam ------------------------------

from notebooklm.exceptions import UnknownRPCMethodError  # noqa: E402


@pytest.mark.asyncio
async def test_download_interactive_tree_real_drift_reraises(tmp_path):
    """A genuine [0][9] reshape in the tree response fails loud (issue #1270)."""
    # GET_INTERACTIVE_HTML returns a short [0] row: descent to [0][9] fails
    # before the leaf -> drift -> UnknownRPCMethodError (not silent not-ready).
    listing = MagicMock()
    listing.list_raw = AsyncMock(return_value=[_INTERACTIVE_ROW])
    mind_maps = MagicMock()
    mind_maps.list_mind_maps = AsyncMock(return_value=[])
    rpc = MagicMock(spec=RpcCaller, rpc_call=AsyncMock(return_value=[[1, 2, 3]]))
    svc = ArtifactDownloadService(rpc=rpc, listing=listing, mind_maps=mind_maps)
    with pytest.raises(UnknownRPCMethodError):
        await svc.download_mind_map("nb", str(tmp_path / "x.json"), artifact_id="int_mm")


@pytest.mark.asyncio
async def test_download_interactive_tree_absent_leaf_is_not_ready(tmp_path):
    """A populated options block missing only the [3] leaf reads as not ready."""
    listing = MagicMock()
    listing.list_raw = AsyncMock(return_value=[_INTERACTIVE_ROW])
    mind_maps = MagicMock()
    mind_maps.list_mind_maps = AsyncMock(return_value=[])
    # [0][9] present but too short to carry the [3] tree leaf.
    response = [[None] * 9 + [[None, None]]]
    rpc = MagicMock(spec=RpcCaller, rpc_call=AsyncMock(return_value=response))
    svc = ArtifactDownloadService(rpc=rpc, listing=listing, mind_maps=mind_maps)
    with pytest.raises(ArtifactNotReadyError):
        await svc.download_mind_map("nb", str(tmp_path / "x.json"), artifact_id="int_mm")


@pytest.mark.asyncio
async def test_download_note_backed_numeric_id_matches_via_noterow(tmp_path):
    """A numeric row id is str-coerced through NoteRow(mm).id, not raw mm[0]."""
    content = '{"name": "Root", "children": []}'
    # Row id is the integer 12345; the caller passes the string "12345".
    svc = _download_service(studio_rows=[], note_rows=[[12345, content]])
    out = str(tmp_path / "mm.json")
    result = await svc.download_mind_map("nb", out, artifact_id="12345")
    assert result == out
    assert json.loads((tmp_path / "mm.json").read_text(encoding="utf-8")) == {
        "name": "Root",
        "children": [],
    }


# --- #1270 sub-fix 2: the transient type-4 discriminator ----------------------


def test_is_unclassified_type4_property():
    assert _art(4, None).is_unclassified_type4 is True  # settling window
    assert _art(4, 4).is_unclassified_type4 is False  # resolved interactive
    assert _art(4, 2).is_unclassified_type4 is False  # resolved quiz
    assert _art(5, None).is_unclassified_type4 is False  # note-backed synthetic
