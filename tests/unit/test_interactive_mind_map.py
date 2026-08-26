"""Phase-1: recognition of interactive (studio-artifact) mind maps.

The web GUI now generates an *interactive* mind map as a studio artifact in the
type-4 (QUIZ) family with variant 4 — distinct from the note-backed mind map the
library adapts using the genuine backend mind-map type code 5. These tests pin
the wire recognition: kind mapping, the listing-filter union, the
`is_interactive_mind_map` discriminator, and downloading the interactive tree
via GET_INTERACTIVE_HTML. See issue #1256.
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
    assert _art(5, None).is_interactive_mind_map is False  # adapted note-backed row


# --- T1.3: listing-filter union ----------------------------------------------


def test_list_mind_map_matches_both_backings():
    assert _matches_artifact_type(_art(5, None), ArtifactType.MIND_MAP)  # note-backed
    assert _matches_artifact_type(_art(4, 4), ArtifactType.MIND_MAP)  # interactive
    assert not _matches_artifact_type(_art(4, 2), ArtifactType.MIND_MAP)  # quiz


def test_list_unknown_excludes_interactive_but_keeps_genuine_unknown():
    assert not _matches_artifact_type(_art(4, 4), ArtifactType.UNKNOWN)
    assert _matches_artifact_type(_art(4, 3), ArtifactType.UNKNOWN)  # genuine unknown variant


# --- P5.8 semantic download guard ---------------------------------------------

from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from notebooklm._semantic.records import (  # noqa: E402
    ARTIFACT_DOWNLOAD_DEF,
    ArtifactDownloadInput,
    ArtifactDownloadResult,
    MindMapRepresentationRecord,
)
from notebooklm._studio.representations import ArtifactRepresentationService  # noqa: E402
from notebooklm._studio.serialization import StudioSerializationClient  # noqa: E402
from notebooklm._web.codec.artifacts import decode_artifact_representation  # noqa: E402
from notebooklm.types import ArtifactNotReadyError  # noqa: E402

_INTERACTIVE_ROW = ["int_mm", "MM", 4, None, 3, None, None, None, None, [None, [4]]]


def _download_service(studio_rows, note_rows, *, interactive_tree=None):
    backend = MagicMock()
    representation_rows = tuple(decode_artifact_representation(row) for row in studio_rows)
    note_records = tuple(
        MindMapRepresentationRecord(str(row[0]), "Mind Map", row[1]) for row in note_rows
    )

    async def invoke(definition, value, *, deadline):
        assert definition is ARTIFACT_DOWNLOAD_DEF
        assert isinstance(value, ArtifactDownloadInput)
        if value.action == "catalog":
            return ArtifactDownloadResult(representations=representation_rows)
        if value.action == "mind_maps":
            return ArtifactDownloadResult(mind_maps=note_records)
        if value.action == "mind_map_tree":
            return ArtifactDownloadResult(content=interactive_tree)
        raise AssertionError(value.action)

    backend.invoke = AsyncMock(side_effect=invoke)
    serializer = StudioSerializationClient()
    return ArtifactRepresentationService(
        backend,
        remote=MagicMock(),
        serialization=serializer,
    )


@pytest.mark.asyncio
async def test_download_interactive_id_with_zero_note_backed_maps(tmp_path):
    tree = '{"name": "Root", "children": [{"name": "A"}]}'
    svc = _download_service([_INTERACTIVE_ROW], [], interactive_tree=tree)
    out = str(tmp_path / "x.json")

    result = await svc.download_mind_map("nb", out, artifact_id="int_mm")

    assert result == out
    assert json.loads((tmp_path / "x.json").read_text(encoding="utf-8"))["name"] == "Root"
    assert any(
        call.args[1].action == "mind_map_tree" for call in svc._backend.invoke.await_args_list
    )


@pytest.mark.asyncio
async def test_download_interactive_tree_absent_leaf_is_not_ready(tmp_path):
    svc = _download_service([_INTERACTIVE_ROW], [])
    with pytest.raises(ArtifactNotReadyError):
        await svc.download_mind_map("nb", str(tmp_path / "x.json"), artifact_id="int_mm")


@pytest.mark.asyncio
async def test_download_note_backed_numeric_id_is_normalized_before_service(tmp_path):
    content = '{"name": "Root", "children": []}'
    svc = _download_service([], [[12345, content]])
    out = str(tmp_path / "mm.json")

    assert await svc.download_mind_map("nb", out, artifact_id="12345") == out
    assert json.loads((tmp_path / "mm.json").read_text(encoding="utf-8"))["name"] == "Root"


# --- #1270 sub-fix 2: the transient type-4 discriminator ----------------------


def test_is_unclassified_type4_property():
    assert _art(4, None).is_unclassified_type4 is True  # settling window
    assert _art(4, 4).is_unclassified_type4 is False  # resolved interactive
    assert _art(4, 2).is_unclassified_type4 is False  # resolved quiz
    assert _art(5, None).is_unclassified_type4 is False  # adapted note-backed row
