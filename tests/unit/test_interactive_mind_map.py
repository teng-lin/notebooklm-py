"""Phase-1: recognition of interactive (studio-artifact) mind maps.

The web GUI now generates an *interactive* mind map as a studio artifact in the
type-4 (QUIZ) family with variant 4 — distinct from the note-backed mind map the
library surfaces with the synthetic type code 5. These tests pin the wire
recognition: kind mapping, the listing-filter union, the `is_interactive_mind_map`
discriminator, and the download-guard message. See issue #1256.
"""

from __future__ import annotations

import json
import warnings

import pytest

from notebooklm._artifact_listing import _matches_artifact_type
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

from notebooklm._artifact_downloads import ArtifactDownloadService  # noqa: E402
from notebooklm.types import ArtifactDownloadError  # noqa: E402

# Raw studio row whose [9][1][0] == 4 → Artifact.is_interactive_mind_map is True.
_INTERACTIVE_ROW = ["int_mm", "MM", 4, None, 3, None, None, None, None, [None, [4]]]


def _download_service(studio_rows, note_rows):
    listing = MagicMock()
    listing.list_raw = AsyncMock(return_value=studio_rows)
    mind_maps = MagicMock()
    mind_maps.list_mind_maps = AsyncMock(return_value=note_rows)
    mind_maps.extract_content = MagicMock(side_effect=lambda row: row[1])
    return ArtifactDownloadService(rpc=MagicMock(), listing=listing, mind_maps=mind_maps)


@pytest.mark.asyncio
async def test_download_interactive_id_with_zero_note_backed_maps(tmp_path):
    """The common interactive-only case: must NOT misreport as 'not ready'."""
    svc = _download_service(studio_rows=[_INTERACTIVE_ROW], note_rows=[])
    with pytest.raises(ArtifactDownloadError) as ei:
        await svc.download_mind_map("nb", str(tmp_path / "x.json"), artifact_id="int_mm")
    assert "interactive" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_download_interactive_id_with_unrelated_note_backed_maps(tmp_path):
    """Interactive id while other note-backed maps exist: not 'not found'."""
    note = ["other_note", '{"name": "x", "children": []}']
    svc = _download_service(studio_rows=[_INTERACTIVE_ROW], note_rows=[note])
    with pytest.raises(ArtifactDownloadError) as ei:
        await svc.download_mind_map("nb", str(tmp_path / "x.json"), artifact_id="int_mm")
    assert "interactive" in str(ei.value).lower()


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
