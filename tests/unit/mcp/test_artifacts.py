"""Unit tests for the artifact MCP tools.

Drives each tool through the in-memory FastMCP ``Client`` against a server bound
to the mocked ``NotebookLMClient``, asserting the serialized
``structured_content``. Covers each tool's happy path, name-vs-id resolution
reaching the tool, the per-``type`` ``artifact_generate`` / ``artifact_download``
enum dispatch, the start→status poll shape, and error projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm._types.artifacts import (  # noqa: E402
    QUIZ_VARIANT,
    ArtifactStatus,
    ArtifactTypeCode,
)
from notebooklm.exceptions import (  # noqa: E402 - after importorskip guard
    ArtifactNotFoundError,
    NotebookNotFoundError,
)
from notebooklm.types import Artifact, ArtifactType, GenerationState  # noqa: E402

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

NB_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "task-abc-123"

#: Real-``Artifact`` builders for the download core (it filters on
#: ``isinstance(a, Artifact)`` + the int type code + ``is_completed``).
_AUDIO_ARTIFACT = Artifact(
    id="art1",
    title="Podcast",
    _artifact_type=ArtifactTypeCode.AUDIO.value,
    status=int(ArtifactStatus.COMPLETED),
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
)
_QUIZ_ARTIFACT = Artifact(
    id="q1",
    title="Quiz",
    _artifact_type=ArtifactTypeCode.QUIZ.value,
    status=int(ArtifactStatus.COMPLETED),
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    _variant=QUIZ_VARIANT,
)


@dataclass
class FakeArtifact:
    id: str
    title: str
    kind: ArtifactType = ArtifactType.AUDIO
    is_completed: bool = True
    created_at: datetime = field(default_factory=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc))


@dataclass
class FakeStatus:
    task_id: str
    status: GenerationState = GenerationState.COMPLETED
    url: str | None = "https://example.com/out.mp3"
    error: str | None = None
    error_code: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def is_complete(self) -> bool:
        return self.status == GenerationState.COMPLETED


# ---------------------------------------------------------------------------
# artifact_list
# ---------------------------------------------------------------------------


async def test_artifact_list(mcp_call, mock_client) -> None:
    mock_client.artifacts.list = AsyncMock(
        return_value=[FakeArtifact(id="art1", title="My Podcast")]
    )
    result = await mcp_call("artifact_list", {"notebook": NB_ID})
    assert result.structured_content["notebook_id"] == NB_ID
    assert result.structured_content["artifacts"][0]["id"] == "art1"
    mock_client.artifacts.list.assert_awaited_once_with(NB_ID)


async def test_artifact_list_resolves_notebook_by_name(mcp_call, mock_client) -> None:
    @dataclass
    class FakeNotebook:
        id: str
        title: str

    mock_client.notebooks.list = AsyncMock(
        return_value=[FakeNotebook(id=NB_ID, title="My Notebook")]
    )
    mock_client.artifacts.list = AsyncMock(return_value=[])
    result = await mcp_call("artifact_list", {"notebook": "My Notebook"})
    assert result.structured_content["notebook_id"] == NB_ID
    mock_client.artifacts.list.assert_awaited_with(NB_ID)


# ---------------------------------------------------------------------------
# artifact_generate
# ---------------------------------------------------------------------------


async def test_artifact_generate_audio(mcp_call, mock_client) -> None:
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    result = await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "audio"})
    assert result.structured_content["kind"] == "audio"
    assert result.structured_content["task_id"] == TASK_ID
    mock_client.artifacts.generate_audio.assert_awaited_once()
    # notebook id is the first positional arg.
    assert mock_client.artifacts.generate_audio.await_args.args[0] == NB_ID


async def test_artifact_generate_quiz_routes_to_quiz(mcp_call, mock_client) -> None:
    mock_client.artifacts.generate_quiz = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    result = await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "quiz"})
    assert result.structured_content["kind"] == "quiz"
    mock_client.artifacts.generate_quiz.assert_awaited_once()


async def test_artifact_generate_video_routes_to_video(mcp_call, mock_client) -> None:
    mock_client.artifacts.generate_video = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "video"})
    mock_client.artifacts.generate_video.assert_awaited_once()


async def test_artifact_generate_report_routes_to_report(mcp_call, mock_client) -> None:
    mock_client.artifacts.generate_report = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "report", "report_format": "study-guide"},
    )
    mock_client.artifacts.generate_report.assert_awaited_once()


async def test_artifact_generate_passes_source_ids(mcp_call, mock_client) -> None:
    # Full-UUID source ids take resolve_source's fast path (no listing) and pass
    # straight through — the style MCP supplies.
    src_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    src_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "audio", "source_ids": [src_a, src_b]},
    )
    kwargs = mock_client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] == (src_a, src_b)


async def test_artifact_generate_resolves_source_id_prefix(mcp_call, mock_client) -> None:
    """A non-UUID source ref is resolved to its full id (like every sibling tool),
    not forwarded raw to the backend."""

    @dataclass
    class _Src:
        id: str
        title: str = "Doc"

    full = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    mock_client.sources.list = AsyncMock(return_value=[_Src(id=full)])
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "audio", "source_ids": [full[:12]]},
    )
    kwargs = mock_client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] == (full,)


async def test_artifact_generate_omitting_source_ids_uses_all(mcp_call, mock_client) -> None:
    """Omitting ``source_ids`` must pass ``source_ids=None`` (=> all sources), NOT an
    empty tuple. An empty list reaches the backend as 'zero sources', which it refuses
    for source-needing kinds (quiz/audio/flashcards), returning a null id surfaced as
    '… generation is unavailable'."""
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "audio"})
    kwargs = mock_client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] is None


async def test_artifact_generate_empty_source_ids_uses_all(mcp_call, mock_client) -> None:
    """An EXPLICIT empty list is the same contract as omitting: => None (all sources),
    never [] (which the backend refuses). Pins the full empty-vs-None contract."""
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate", {"notebook": NB_ID, "artifact_type": "audio", "source_ids": []}
    )
    kwargs = mock_client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] is None


# Full-UUID source ids take resolve_source's fast path (no listing needed), so the
# string-shape coercion tests below need no ``sources.list`` mock.
_SRC_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_SRC_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


async def test_artifact_generate_source_ids_json_string(mcp_call, mock_client) -> None:
    """``source_ids`` sent as a JSON-array string is tolerated (coerce_list)."""
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "audio", "source_ids": f'["{_SRC_A}","{_SRC_B}"]'},
    )
    kwargs = mock_client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] == (_SRC_A, _SRC_B)


async def test_artifact_generate_source_ids_comma_string(mcp_call, mock_client) -> None:
    """``source_ids`` sent as a comma-separated string is tolerated (coerce_list)."""
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "audio", "source_ids": f"{_SRC_A},{_SRC_B}"},
    )
    kwargs = mock_client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] == (_SRC_A, _SRC_B)


async def test_artifact_generate_source_ids_scalar_string(mcp_call, mock_client) -> None:
    """``source_ids`` sent as a bare scalar string is tolerated (coerce_list)."""
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "audio", "source_ids": _SRC_A},
    )
    kwargs = mock_client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] == (_SRC_A,)


async def test_artifact_generate_source_ids_empty_string_uses_all(mcp_call, mock_client) -> None:
    """An empty string coerces to [] => collapses to None (all sources)."""
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "audio", "source_ids": ""},
    )
    kwargs = mock_client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] is None


async def test_artifact_generate_source_ids_whitespace_uses_all(mcp_call, mock_client) -> None:
    """A whitespace-only string coerces to [] => collapses to None (all sources)."""
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "audio", "source_ids": "   "},
    )
    kwargs = mock_client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] is None


async def test_artifact_generate_unknown_type_is_validation_error(mcp_call, mock_client) -> None:
    """An unknown artifact_type is rejected at the Literal schema boundary."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "bogus"})
    assert "audio" in str(excinfo.value) and "report" in str(excinfo.value)


async def test_artifact_generate_bad_enum_is_validation_error(mcp_call, mock_client) -> None:
    """A bad per-kind option (e.g. report_format) projects as VALIDATION."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_generate",
            {"notebook": NB_ID, "artifact_type": "report", "report_format": "nonsense"},
        )
    assert "VALIDATION" in str(excinfo.value)


async def test_artifact_generate_bad_language_is_validation_error(mcp_call, mock_client) -> None:
    """An unsupported ``language`` projects as VALIDATION up front (not forwarded raw)."""
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_generate",
            {"notebook": NB_ID, "artifact_type": "audio", "language": "klingon"},
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.artifacts.generate_audio.assert_not_called()


async def test_artifact_generate_valid_language_passes(mcp_call, mock_client) -> None:
    """A supported language code is accepted and forwarded."""
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    result = await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "audio", "language": "es"},
    )
    assert result.structured_content["kind"] == "audio"
    mock_client.artifacts.generate_audio.assert_awaited_once()


# ---------------------------------------------------------------------------
# artifact_generate — per-kind options (#1654)
# ---------------------------------------------------------------------------


async def test_artifact_generate_video_options(mcp_call, mock_client) -> None:
    """video format/style/style_prompt all reach generate_video (custom style path)."""
    mock_client.artifacts.generate_video = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {
            "notebook": NB_ID,
            "artifact_type": "video",
            "video_format": "brief",
            "style": "custom",
            "style_prompt": "hand-drawn diagrams",
        },
    )
    kwargs = mock_client.artifacts.generate_video.await_args.kwargs
    assert kwargs["video_format"].name == "BRIEF"
    assert kwargs["video_style"].name == "CUSTOM"
    assert kwargs["style_prompt"] == "hand-drawn diagrams"


async def test_artifact_generate_slide_deck_options(mcp_call, mock_client) -> None:
    mock_client.artifacts.generate_slide_deck = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {
            "notebook": NB_ID,
            "artifact_type": "slide-deck",
            "deck_format": "presenter",
            "deck_length": "short",
        },
    )
    kwargs = mock_client.artifacts.generate_slide_deck.await_args.kwargs
    assert kwargs["slide_format"].name == "PRESENTER_SLIDES"
    assert kwargs["slide_length"].name == "SHORT"


async def test_artifact_generate_infographic_options(mcp_call, mock_client) -> None:
    mock_client.artifacts.generate_infographic = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {
            "notebook": NB_ID,
            "artifact_type": "infographic",
            "orientation": "portrait",
            "detail": "detailed",
            "style": "professional",
        },
    )
    kwargs = mock_client.artifacts.generate_infographic.await_args.kwargs
    assert kwargs["orientation"].name == "PORTRAIT"
    assert kwargs["detail_level"].name == "DETAILED"
    assert kwargs["style"].name == "PROFESSIONAL"


async def test_artifact_generate_mind_map_interactive_default(mcp_call, mock_client) -> None:
    """Omitted ``map_kind`` defaults to interactive → routes to ``mind_maps.generate``."""
    mock_client.mind_maps.generate = AsyncMock(return_value={"id": "mm1"})
    await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "mind-map"})
    mock_client.mind_maps.generate.assert_awaited_once()
    mock_client.artifacts.generate_mind_map.assert_not_called()


async def test_artifact_generate_mind_map_note_backed_routes(mcp_call, mock_client) -> None:
    """``map_kind=note-backed`` routes to ``artifacts.generate_mind_map`` instead."""
    mock_client.artifacts.generate_mind_map = AsyncMock(return_value={"id": "mm1"})
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "mind-map", "map_kind": "note-backed"},
    )
    mock_client.artifacts.generate_mind_map.assert_awaited_once()
    mock_client.mind_maps.generate.assert_not_called()


async def test_artifact_generate_mind_map_forwards_instructions(mcp_call, mock_client) -> None:
    """``instructions`` reaches the mind-map client call (the dropped-instructions fix).

    MCP stores the tool ``instructions`` arg as ``raw_args["description"]``, but the
    mind-map plan reads ``raw_args["instructions"]`` — so MCP also sets that key. Without
    the fix, mind-map instructions were silently discarded.
    """
    mock_client.mind_maps.generate = AsyncMock(return_value={"id": "mm1"})
    await mcp_call(
        "artifact_generate",
        {
            "notebook": NB_ID,
            "artifact_type": "mind-map",
            "instructions": "focus on the timeline",
        },
    )
    kwargs = mock_client.mind_maps.generate.await_args.kwargs
    assert kwargs["instructions"] == "focus on the timeline"


@pytest.mark.parametrize(
    "artifact_type,opts",
    [
        ("video", {"style": "professional"}),  # infographic-only value, invalid for video
        ("infographic", {"style": "classic"}),  # video-only value, invalid for infographic
        ("mind-map", {"map_kind": "bogus"}),  # core wouldn't catch — MCP must
        ("slide-deck", {"deck_format": "nonsense"}),
    ],
    ids=["video-bad-style", "infographic-bad-style", "bad-map-kind", "bad-deck-format"],
)
async def test_artifact_generate_bad_option_value_is_validation_error(
    mcp_call, mock_client, artifact_type: str, opts: dict
) -> None:
    """A value outside the kind's choice set projects as VALIDATION.

    The per-type ``style`` cases prove the video/infographic style sets are enforced
    separately (the two overlap only on auto/anime/kawaii).
    """
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_generate",
            {"notebook": NB_ID, "artifact_type": artifact_type, **opts},
        )
    assert "VALIDATION" in str(excinfo.value)


@pytest.mark.parametrize(
    "artifact_type,opts",
    [
        ("quiz", {"orientation": "portrait"}),  # infographic option on quiz
        ("video", {"deck_format": "presenter"}),  # slide-deck option on video
        ("audio", {"video_format": "brief"}),  # video option on audio
        ("video", {"map_kind": "interactive"}),  # mind-map option on video
        ("cinematic-video", {"style": "classic"}),  # cinematic-video exposes NO options
    ],
    ids=[
        "orientation-on-quiz",
        "deck-on-video",
        "video-on-audio",
        "mapkind-on-video",
        "style-on-cinematic",
    ],
)
async def test_artifact_generate_wrong_kind_option_is_validation_error(
    mcp_call, mock_client, artifact_type: str, opts: dict
) -> None:
    """An option valid for some OTHER kind is rejected, not silently ignored.

    The neutral core ignores irrelevant extras, so this rejection lives in the MCP tool;
    without it an agent's mis-targeted option would silently no-op.
    """
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_generate",
            {"notebook": NB_ID, "artifact_type": artifact_type, **opts},
        )
    assert "VALIDATION" in str(excinfo.value)


async def test_artifact_generate_wrong_kind_message_for_optionless_kind(
    mcp_call, mock_client
) -> None:
    """A kind with no per-kind options reports that clearly (not ``accepts []``)."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_generate",
            {"notebook": NB_ID, "artifact_type": "cinematic-video", "style": "classic"},
        )
    assert "no per-kind options" in str(excinfo.value)


async def test_artifact_generate_style_prompt_requires_custom(mcp_call, mock_client) -> None:
    """``style_prompt`` without ``style=custom`` is rejected (core cross-field rule)."""
    mock_client.artifacts.generate_video = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_generate",
            {"notebook": NB_ID, "artifact_type": "video", "style_prompt": "hand-drawn"},
        )
    assert "VALIDATION" in str(excinfo.value)


def test_kind_options_match_core_maps() -> None:
    """The MCP per-kind choice tuples are DUPLICATED from the core's private maps (the
    CLI/MCP boundary forbids importing them at runtime). Pin them equal so they can't
    silently drift — the parity tests only exercise valid values and would miss a
    *subset* drift (MCP wrongly rejecting a value the core accepts)."""
    from notebooklm._app import generate_plans as gp
    from notebooklm.mcp.tools.artifacts import _KIND_OPTIONS

    assert _KIND_OPTIONS["audio"]["audio_format"] == tuple(gp._AUDIO_FORMAT_MAP)
    assert _KIND_OPTIONS["audio"]["audio_length"] == tuple(gp._AUDIO_LENGTH_MAP)
    assert _KIND_OPTIONS["video"]["video_format"] == tuple(gp._VIDEO_FORMAT_MAP)
    assert _KIND_OPTIONS["video"]["style"] == tuple(gp._VIDEO_STYLE_MAP)
    assert _KIND_OPTIONS["slide-deck"]["deck_format"] == tuple(gp._SLIDE_FORMAT_MAP)
    assert _KIND_OPTIONS["slide-deck"]["deck_length"] == tuple(gp._SLIDE_LENGTH_MAP)
    assert _KIND_OPTIONS["quiz"]["quantity"] == tuple(gp._QUIZ_QUANTITY_MAP)
    assert _KIND_OPTIONS["quiz"]["difficulty"] == tuple(gp._QUIZ_DIFFICULTY_MAP)
    # flashcards reuses the same core maps today; pin independently so a future
    # flashcards-specific map can't drift the MCP set unnoticed.
    assert _KIND_OPTIONS["flashcards"]["quantity"] == tuple(gp._QUIZ_QUANTITY_MAP)
    assert _KIND_OPTIONS["flashcards"]["difficulty"] == tuple(gp._QUIZ_DIFFICULTY_MAP)
    assert _KIND_OPTIONS["infographic"]["orientation"] == tuple(gp._INFOGRAPHIC_ORIENTATION_MAP)
    assert _KIND_OPTIONS["infographic"]["detail"] == tuple(gp._INFOGRAPHIC_DETAIL_MAP)
    assert _KIND_OPTIONS["infographic"]["style"] == tuple(gp._INFOGRAPHIC_STYLE_MAP)
    assert _KIND_OPTIONS["report"]["report_format"] == tuple(gp._REPORT_FORMAT_MAP)


async def test_artifact_generate_exposes_new_option_params(mcp_list_tools) -> None:
    """The agent-facing tool schema exposes every new per-kind option parameter."""
    tools = await mcp_list_tools()
    schema = next(t for t in tools if t.name == "artifact_generate").inputSchema
    properties = schema.get("properties", {})
    for param in (
        "video_format",
        "style",
        "style_prompt",
        "deck_format",
        "deck_length",
        "orientation",
        "detail",
        "map_kind",
    ):
        assert param in properties, f"artifact_generate must expose {param!r}"


# ---------------------------------------------------------------------------
# artifact_status (stateless poll)
# ---------------------------------------------------------------------------


async def test_artifact_status(mcp_call, mock_client) -> None:
    mock_client.artifacts.poll_status = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    result = await mcp_call("artifact_status", {"notebook": NB_ID, "task_id": TASK_ID})
    assert result.structured_content["task_id"] == TASK_ID
    assert result.structured_content["is_complete"] is True
    assert result.structured_content["status"] == GenerationState.COMPLETED.value
    mock_client.artifacts.poll_status.assert_awaited_once_with(NB_ID, TASK_ID)


async def test_artifact_generate_then_status_poll_shape(mcp_call, mock_client) -> None:
    """The start→status poll loop: generate returns a task_id, status polls it."""
    mock_client.artifacts.generate_audio = AsyncMock(
        return_value=FakeStatus(task_id=TASK_ID, status=GenerationState.PENDING, url=None)
    )
    started = await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "audio"})
    task_id = started.structured_content["task_id"]
    assert task_id == TASK_ID

    mock_client.artifacts.poll_status = AsyncMock(
        return_value=FakeStatus(task_id=TASK_ID, status=GenerationState.COMPLETED)
    )
    polled = await mcp_call("artifact_status", {"notebook": NB_ID, "task_id": task_id})
    assert polled.structured_content["is_complete"] is True


# ---------------------------------------------------------------------------
# artifact_download
# ---------------------------------------------------------------------------


async def test_artifact_download_audio(mcp_call, mock_client, tmp_path) -> None:
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT])
    mock_client.artifacts.download_audio = AsyncMock(return_value=out)
    result = await mcp_call(
        "artifact_download", {"notebook": NB_ID, "artifact_type": "audio", "path": out}
    )
    assert result.structured_content["outcome"] == "single_downloaded"
    assert result.structured_content["output_path"] == out
    mock_client.artifacts.download_audio.assert_awaited_once()


async def test_artifact_download_quiz_with_format(mcp_call, mock_client, tmp_path) -> None:
    out = str(tmp_path / "quiz.md")
    mock_client.artifacts.list = AsyncMock(return_value=[_QUIZ_ARTIFACT])
    mock_client.artifacts.download_quiz = AsyncMock(return_value=out)
    result = await mcp_call(
        "artifact_download",
        {"notebook": NB_ID, "artifact_type": "quiz", "path": out, "output_format": "markdown"},
    )
    assert result.structured_content["outcome"] == "single_downloaded"
    # The format kwarg flows through to the bound download coroutine.
    assert mock_client.artifacts.download_quiz.await_args.kwargs.get("output_format") == "markdown"


async def test_artifact_download_unknown_type_is_validation_error(mcp_call, mock_client) -> None:
    """An unknown download artifact_type is rejected at the Literal schema boundary."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_download", {"notebook": NB_ID, "artifact_type": "bogus", "path": "/tmp/x"}
        )
    assert "audio" in str(excinfo.value) and "flashcards" in str(excinfo.value)


async def test_artifact_download_bad_format_for_supported_type_is_validation(
    mcp_call, mock_client, tmp_path
) -> None:
    """A bad ``format`` for a type that DOES support format projects VALIDATION."""
    out = str(tmp_path / "quiz.json")
    mock_client.artifacts.list = AsyncMock(return_value=[_QUIZ_ARTIFACT])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_download",
            {"notebook": NB_ID, "artifact_type": "quiz", "path": out, "output_format": "bogus"},
        )
    assert "VALIDATION" in str(excinfo.value)


async def test_artifact_download_format_for_unsupported_type_is_validation(
    mcp_call, mock_client, tmp_path
) -> None:
    """Supplying ``format`` for a type WITHOUT format choices errors (was silently dropped)."""
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT])
    mock_client.artifacts.download_audio = AsyncMock(return_value=out)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_download",
            {"notebook": NB_ID, "artifact_type": "audio", "path": out, "output_format": "pdf"},
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.artifacts.download_audio.assert_not_called()


async def test_artifact_download_no_artifacts(mcp_call, mock_client, tmp_path) -> None:
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[])
    result = await mcp_call(
        "artifact_download", {"notebook": NB_ID, "artifact_type": "audio", "path": out}
    )
    assert result.structured_content["outcome"] == "no_artifacts"


# ---------------------------------------------------------------------------
# error projection
# ---------------------------------------------------------------------------


async def test_artifact_status_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise ArtifactNotFoundError(TASK_ID)

    mock_client.artifacts.poll_status = AsyncMock(side_effect=_raise)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("artifact_status", {"notebook": NB_ID, "task_id": TASK_ID})
    assert "NOT_FOUND" in str(excinfo.value)


async def test_artifact_list_notebook_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    mock_client.notebooks.list = AsyncMock(return_value=[])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("artifact_list", {"notebook": "No Such Notebook"})
    assert "NOT_FOUND" in str(excinfo.value)
    _ = NotebookNotFoundError  # imported for symmetry with sibling suites
