"""Direct coverage for :class:`ArtifactGenerationService`.

The facade-level suites drive ``ArtifactsAPI`` and stop at the happy path.
These cases exercise the service's own seams: the ``language=None`` /
``source_ids=None`` resolution shared by every ``generate_*`` entry point, the
``generate_video`` option-compatibility rules, the mind-map JSON leaf handling,
and the row-shape guards in ``_parse_generation_result``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from notebooklm._types.notes import Note
from notebooklm._types.research import MindMapResult
from notebooklm._web.artifact.generation import ArtifactGenerationService
from notebooklm.exceptions import (
    ArtifactFeatureUnavailableError,
    DecodingError,
    ValidationError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.types import VideoFormat, VideoStyle

pytestmark = pytest.mark.asyncio


@dataclass
class _RecordedCall:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _FakeRpc:
    """Records dispatches and replays a scripted result per method."""

    def __init__(self, results: dict[RPCMethod, Any] | None = None) -> None:
        self._results = results or {}
        self.calls: list[_RecordedCall] = []

    async def rpc_call(self, method, params, source_path="/", **kwargs):  # noqa: ANN001
        self.calls.append(_RecordedCall(method, params, {"source_path": source_path, **kwargs}))
        # An unscripted method answers with the standard accepted-task row.
        return self._results.get(method, [["artifact-1", None, None, None, 2]])

    @property
    def only(self) -> _RecordedCall:
        [call] = self.calls
        return call


@dataclass
class _FakeNotebooks:
    source_ids: list[str] = field(default_factory=lambda: ["resolved-src"])
    asked: list[str] = field(default_factory=list)

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        self.asked.append(notebook_id)
        return list(self.source_ids)


@dataclass
class _FakeNoteService:
    note: Note | None = None
    created: list[tuple[str, str, str]] = field(default_factory=list)

    async def create_note(self, notebook_id: str, *, title: str, content: str) -> Note:
        self.created.append((notebook_id, title, content))
        if self.note is not None:
            return self.note
        return Note(
            id="note-1",
            notebook_id=notebook_id,
            title=title,
            content=content,
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )


def _service(
    *,
    rpc: _FakeRpc | None = None,
    notebooks: _FakeNotebooks | None = None,
    notes: _FakeNoteService | None = None,
) -> tuple[ArtifactGenerationService, _FakeRpc, _FakeNotebooks, _FakeNoteService]:
    rpc = rpc or _FakeRpc()
    notebooks = notebooks or _FakeNotebooks()
    notes = notes or _FakeNoteService()
    service = ArtifactGenerationService(rpc=rpc, notebooks=notebooks, note_service=notes)
    return service, rpc, notebooks, notes


# ---------------------------------------------------------------------------
# Shared language / source-id resolution
# ---------------------------------------------------------------------------

#: ``(method name, extra kwargs)`` for every entry point that resolves both
#: ``language=None`` (via ``NOTEBOOKLM_HL``) and ``source_ids=None``.
_LANGUAGE_AWARE = [
    ("generate_audio", {}),
    ("generate_video", {}),
    ("generate_cinematic_video", {}),
    ("generate_report", {}),
    ("generate_study_guide", {}),
    ("generate_infographic", {}),
    ("generate_slide_deck", {}),
    ("generate_data_table", {}),
    ("generate_mind_map", {}),
]

#: Entry points with no ``language`` parameter — source-id resolution only.
_SOURCE_ONLY = [("generate_quiz", {}), ("generate_flashcards", {})]


@pytest.mark.parametrize("name", [n for n, _ in _LANGUAGE_AWARE])
async def test_language_none_resolves_from_the_environment(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_HL", "fr")
    service, rpc, _, _ = _service(rpc=_FakeRpc({RPCMethod.GENERATE_MIND_MAP: [[None]]}))

    await getattr(service, name)("nb1", language=None)

    # The resolved tag reaches the wire params rather than a literal ``None``.
    assert "fr" in repr(rpc.only.params)


@pytest.mark.parametrize("name", [n for n, _ in _LANGUAGE_AWARE + _SOURCE_ONLY])
async def test_source_ids_none_resolves_from_the_notebook(name: str) -> None:
    service, rpc, notebooks, _ = _service(rpc=_FakeRpc({RPCMethod.GENERATE_MIND_MAP: [[None]]}))

    await getattr(service, name)("nb1", source_ids=None)

    assert notebooks.asked == ["nb1"]
    assert "resolved-src" in repr(rpc.only.params)


@pytest.mark.parametrize("name", [n for n, _ in _LANGUAGE_AWARE + _SOURCE_ONLY])
async def test_explicit_source_ids_skip_the_notebook_lookup(name: str) -> None:
    service, rpc, notebooks, _ = _service(rpc=_FakeRpc({RPCMethod.GENERATE_MIND_MAP: [[None]]}))

    await getattr(service, name)("nb1", source_ids=["explicit-src"])

    assert notebooks.asked == []
    assert "explicit-src" in repr(rpc.only.params)


async def test_generate_study_guide_delegates_to_generate_report() -> None:
    service, rpc, _, _ = _service()

    await service.generate_study_guide("nb1", source_ids=["s1"], extra_instructions="focus")

    assert rpc.only.method is RPCMethod.CREATE_ARTIFACT
    assert rpc.only.kwargs["source_path"] == "/notebook/nb1"


# ---------------------------------------------------------------------------
# generate_video wire dispatch (validation belongs to ArtifactsAPI)
# ---------------------------------------------------------------------------


async def test_generate_video_accepts_short_with_auto_select_style() -> None:
    """``AUTO_SELECT`` is the short format's own default, not an override."""
    service, rpc, _, _ = _service()

    await service.generate_video(
        "nb1",
        source_ids=["s1"],
        video_format=VideoFormat.SHORT,
        video_style=VideoStyle.AUTO_SELECT,
    )

    assert rpc.only.method is RPCMethod.CREATE_ARTIFACT


async def test_generate_video_forwards_the_validated_custom_style_prompt() -> None:
    service, rpc, _, _ = _service()

    await service.generate_video(
        "nb1",
        source_ids=["s1"],
        video_style=VideoStyle.CUSTOM,
        style_prompt="neon skyline",
    )

    assert "neon skyline" in repr(rpc.only.params)


# ---------------------------------------------------------------------------
# revise_slide / retry_failed
# ---------------------------------------------------------------------------


async def test_revise_slide_rejects_a_negative_index() -> None:
    service, rpc, _, _ = _service()

    with pytest.raises(ValidationError, match="slide_index must be >= 0"):
        await service.revise_slide("nb1", "art-1", -1, "brighter")

    assert rpc.calls == []


async def test_revise_slide_raises_when_the_server_returns_no_row() -> None:
    service, _, _, _ = _service(rpc=_FakeRpc({RPCMethod.REVISE_SLIDE: None}))

    with pytest.raises(ArtifactFeatureUnavailableError):
        await service.revise_slide("nb1", "art-1", 0, "brighter")


async def test_revise_slide_returns_the_accepted_task() -> None:
    service, rpc, _, _ = _service(
        rpc=_FakeRpc({RPCMethod.REVISE_SLIDE: [["art-1", None, None, None, 2]]})
    )

    status = await service.revise_slide("nb1", "art-1", 2, "brighter")

    assert status.task_id == "art-1"
    assert rpc.only.kwargs["raise_on_null_status"] is True


async def test_retry_failed_raises_when_the_server_returns_no_row() -> None:
    service, _, _, _ = _service(rpc=_FakeRpc({RPCMethod.RETRY_ARTIFACT: None}))

    with pytest.raises(ArtifactFeatureUnavailableError) as excinfo:
        await service.retry_failed("nb1", "art-1")

    assert excinfo.value.method_id == RPCMethod.RETRY_ARTIFACT.value


async def test_retry_failed_rejects_a_row_with_an_empty_artifact_id() -> None:
    """#1342: a row that created no task raises rather than reporting failure."""
    service, _, _, _ = _service(
        rpc=_FakeRpc({RPCMethod.RETRY_ARTIFACT: [["", None, None, None, 2]]})
    )

    with pytest.raises(DecodingError, match="No artifact id"):
        await service.retry_failed("nb1", "art-1")


async def test_retry_failed_reports_a_null_artifact_id_as_feature_gated() -> None:
    service, _, _, _ = _service(
        rpc=_FakeRpc({RPCMethod.RETRY_ARTIFACT: [[None, None, None, None, 2]]})
    )

    with pytest.raises(ArtifactFeatureUnavailableError) as excinfo:
        await service.retry_failed("nb1", "art-1")

    assert excinfo.value.method_id == RPCMethod.RETRY_ARTIFACT.value


async def test_retry_failed_returns_the_requeued_artifact_id() -> None:
    service, rpc, _, _ = _service(
        rpc=_FakeRpc({RPCMethod.RETRY_ARTIFACT: [["art-1", None, None, None, 1]]})
    )

    status = await service.retry_failed("nb1", "art-1")

    assert status.task_id == "art-1"
    assert status.status == "pending"
    assert rpc.only.kwargs["source_path"] == "/notebook/nb1"


# ---------------------------------------------------------------------------
# _call_generate / _parse_generation_result
# ---------------------------------------------------------------------------


async def test_null_result_names_the_requested_artifact_type() -> None:
    service, _, _, _ = _service(rpc=_FakeRpc({RPCMethod.CREATE_ARTIFACT: None}))

    with pytest.raises(ArtifactFeatureUnavailableError, match="Quiz generation is unavailable"):
        await service.generate_quiz("nb1", source_ids=["s1"])


async def test_null_artifact_id_raises_feature_unavailable() -> None:
    service, _, _, _ = _service(
        rpc=_FakeRpc({RPCMethod.CREATE_ARTIFACT: [[None, None, None, None, 2]]})
    )

    with pytest.raises(ArtifactFeatureUnavailableError, match="Artifact generation is unavailable"):
        await service.generate_audio("nb1", source_ids=["s1"])


async def test_empty_artifact_id_raises_decoding_error() -> None:
    """A present-but-empty id is drift, not a gated feature."""
    service, _, _, _ = _service(
        rpc=_FakeRpc({RPCMethod.CREATE_ARTIFACT: [["", None, None, None, 2]]})
    )

    with pytest.raises(DecodingError, match="No artifact id"):
        await service.generate_audio("nb1", source_ids=["s1"])


async def test_generation_debug_label_tolerates_a_short_param_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The debug label is best-effort: a short descriptor logs ``unknown``."""
    service, _, _, _ = _service()

    with caplog.at_level("DEBUG", logger="notebooklm._artifact.generation"):
        await service._call_generate("nb1", ["ctx", "nb1"])

    assert "type=unknown" in caplog.text


# ---------------------------------------------------------------------------
# generate_mind_map
# ---------------------------------------------------------------------------


async def test_mind_map_absent_leaf_returns_an_empty_result() -> None:
    service, _, _, notes = _service(rpc=_FakeRpc({RPCMethod.GENERATE_MIND_MAP: []}))

    result = await service.generate_mind_map("nb1", source_ids=["s1"])

    assert result == MindMapResult(mind_map=None, note_id=None)
    assert notes.created == []


async def test_mind_map_json_leaf_is_parsed_and_titled_from_its_root_name() -> None:
    service, _, _, notes = _service(
        rpc=_FakeRpc({RPCMethod.GENERATE_MIND_MAP: [['{"name": "Photosynthesis"}']]})
    )

    result = await service.generate_mind_map("nb1", source_ids=["s1"])

    assert result.mind_map == {"name": "Photosynthesis"}
    assert result.note_id == "note-1"
    assert notes.created == [("nb1", "Photosynthesis", '{"name": "Photosynthesis"}')]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param('{"name": ""}', id="empty-name"),
        pytest.param('{"name": 7}', id="non-string-name"),
        pytest.param('{"nodes": []}', id="no-name-key"),
        pytest.param("[1, 2]", id="non-dict-root"),
    ],
)
async def test_mind_map_falls_back_to_the_default_title(payload: str) -> None:
    """#1270: only a non-empty ``str`` name may become the note title."""
    service, _, _, notes = _service(rpc=_FakeRpc({RPCMethod.GENERATE_MIND_MAP: [[payload]]}))

    await service.generate_mind_map("nb1", source_ids=["s1"])

    [(_, title, _content)] = notes.created
    assert title == "Mind Map"


async def test_mind_map_unparseable_json_is_stored_verbatim() -> None:
    service, _, _, notes = _service(
        rpc=_FakeRpc({RPCMethod.GENERATE_MIND_MAP: [["not json at all"]]})
    )

    result = await service.generate_mind_map("nb1", source_ids=["s1"])

    assert result.mind_map == "not json at all"
    assert notes.created == [("nb1", "Mind Map", "not json at all")]


async def test_mind_map_non_string_leaf_is_reserialised_for_the_note_body() -> None:
    service, _, _, notes = _service(
        rpc=_FakeRpc({RPCMethod.GENERATE_MIND_MAP: [[{"name": "Tree"}]]})
    )

    result = await service.generate_mind_map("nb1", source_ids=["s1"])

    assert result.mind_map == {"name": "Tree"}
    assert notes.created == [("nb1", "Tree", '{"name": "Tree"}')]


async def test_mind_map_note_without_an_id_reports_note_id_none() -> None:
    """Defensive: the public contract says ``note_id is None`` means unsaved."""
    notes = _FakeNoteService(note=Note(id="", notebook_id="nb1", title="Mind Map", content="{}"))
    service, _, _, _ = _service(rpc=_FakeRpc({RPCMethod.GENERATE_MIND_MAP: [["{}"]]}), notes=notes)

    result = await service.generate_mind_map("nb1", source_ids=["s1"])

    assert result.note_id is None
