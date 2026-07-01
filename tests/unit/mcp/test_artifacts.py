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
from notebooklm._types.mind_maps import MindMapKind  # noqa: E402
from notebooklm.exceptions import (  # noqa: E402 - after importorskip guard
    ArtifactNotFoundError,
    NotebookNotFoundError,
    RateLimitError,
)
from notebooklm.mcp.tools.artifacts import _KIND_OPTIONS  # noqa: E402
from notebooklm.types import Artifact, ArtifactType, GenerationState  # noqa: E402

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

NB_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "task-abc-123"


def _schema_enum(prop: dict[str, Any]) -> set[str] | None:
    """The JSON-schema ``enum`` for a tool param, or ``None`` if it has none.

    Handles BOTH shapes FastMCP/Pydantic emits: a required ``Literal`` renders a
    flat ``{"enum": [...]}``; an optional ``Literal[...] | None`` renders
    ``{"anyOf": [{"enum": [...], "type": "string"}, {"type": "null"}]}``. A
    free-text ``str``/``str | None`` param has no ``enum`` branch → ``None``.
    """
    if "enum" in prop:
        return set(prop["enum"])
    # ``anyOf`` is Pydantic v2's shape for ``T | None`` today; also scan ``oneOf``
    # so the helper survives a future schema-generation switch to the JSON-Schema
    # mutually-exclusive form rather than silently returning ``None``.
    for branch in (prop.get("anyOf") or []) + (prop.get("oneOf") or []):
        if "enum" in branch:
            return set(branch["enum"])
    return None


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
class FakeMindMap:
    """Minimal ``MindMap`` stand-in for the rename/delete mind-map probes.

    ``rename_artifact`` reads ``.id`` + ``.kind`` off ``mind_maps.list`` rows;
    ``delete_artifact`` reads ``.id`` off ``mind_maps.list_note_backed`` rows.
    """

    id: str
    kind: MindMapKind = MindMapKind.INTERACTIVE


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
    sc = result.structured_content
    assert sc["notebook_id"] == NB_ID
    assert sc["artifacts"][0]["id"] == "art1"
    # Pin the pagination meta too, so a total/offset/has_more regression can't slip
    # through a subset assertion (unlike the sibling *_list tests).
    assert sc["total"] == 1
    assert sc["offset"] == 0
    assert sc["has_more"] is False
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


async def test_artifact_generate_two_title_refs_list_once_order_preserved(
    mcp_call, mock_client
) -> None:
    """Two non-UUID refs resolve via a single ``sources.list`` snapshot, in input order."""

    @dataclass
    class _Src:
        id: str
        title: str | None

    src_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    src_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    mock_client.sources.list = AsyncMock(
        return_value=[_Src(id=src_a, title="Alpha"), _Src(id=src_b, title="Beta")]
    )
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "audio", "source_ids": ["Beta", "Alpha"]},
    )
    mock_client.sources.list.assert_awaited_once_with(NB_ID)
    kwargs = mock_client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] == (src_b, src_a)


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
    ],
    ids=["video-bad-style", "infographic-bad-style"],
)
async def test_artifact_generate_cross_kind_style_is_validation_error(
    mcp_call, mock_client, artifact_type: str, opts: dict
) -> None:
    """A ``style`` value that IS in the global union Literal but invalid for THIS kind
    projects as VALIDATION via the runtime ``_KIND_OPTIONS`` loop.

    ``style`` is a single union Literal (video ∪ infographic), so these values pass
    the schema boundary and must be narrowed per-kind at runtime — proving the
    video/infographic style sets stay enforced separately (they overlap only on
    auto/anime/kawaii).
    """
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_generate",
            {"notebook": NB_ID, "artifact_type": artifact_type, **opts},
        )
    msg = str(excinfo.value)
    assert "VALIDATION" in msg
    # ...and NOT a boundary rejection: these values are in the global union Literal,
    # so they pass Pydantic and are caught by the runtime per-kind narrowing.
    assert "literal_error" not in msg


@pytest.mark.parametrize(
    "artifact_type,opts,accepted",
    [
        (
            "report",
            {"report_format": "nonsense"},
            ("briefing-doc", "study-guide", "blog-post", "custom"),
        ),
        ("mind-map", {"map_kind": "bogus"}, ("interactive", "note-backed")),
        ("slide-deck", {"deck_format": "nonsense"}, ("detailed", "presenter")),
        # A value outside the GLOBAL union ``style`` Literal rejects at the boundary
        # too (distinct from the cross-kind cases above, which ARE in the union).
        (
            "video",
            {"style": "nonsense"},
            tuple(
                # sorted() so the parametrize id / member order is deterministic
                # across runs (set iteration order varies with hash randomization).
                sorted(
                    set(_KIND_OPTIONS["video"]["style"])
                    | set(_KIND_OPTIONS["infographic"]["style"])
                )
            ),
        ),
    ],
    ids=["bad-report-format", "bad-map-kind", "bad-deck-format", "out-of-union-style"],
)
async def test_artifact_generate_bad_option_value_is_schema_boundary_error(
    mcp_call, mock_client, artifact_type: str, opts: dict, accepted: tuple[str, ...]
) -> None:
    """An out-of-enum value for a ``Literal`` option rejects at the schema boundary
    (pydantic ``literal_error``), surfacing the accepted members — NOT the runtime
    ``"VALIDATION"`` projection (which only fires for values that pass the boundary,
    i.e. the cross-kind ``style`` cases above).

    This is the point of the Literal typing: bad values reject earlier (no
    ``"VALIDATION"`` substring — same as the unknown-``artifact_type`` case), with
    the schema enum surfaced to the agent. The ``"VALIDATION" not in`` +
    ``literal_error in`` assertions are what actually distinguish a boundary
    rejection from the runtime path (both list the accepted members)."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_generate",
            {"notebook": NB_ID, "artifact_type": artifact_type, **opts},
        )
    msg = str(excinfo.value)
    assert all(member in msg for member in accepted)
    assert "VALIDATION" not in msg
    assert "literal_error" in msg


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


async def test_artifact_generate_option_params_expose_enums(mcp_list_tools) -> None:
    """Each finite-choice option param is typed ``Literal`` → the tool schema exposes a
    JSON-schema ``enum`` matching ``_KIND_OPTIONS`` (acceptance criterion for #1666).

    The expected enum is read from ``_KIND_OPTIONS`` (pinned equal to the neutral core
    maps by ``test_kind_options_match_core_maps``), so a core-map change not mirrored
    into BOTH ``_KIND_OPTIONS`` and the signature ``Literal`` fails here. ``style`` is a
    single union Literal, so its enum is the union across video+infographic; ``quantity``
    /``difficulty`` are shared by quiz+flashcards (identical today — assert the union so
    a future flashcards-specific set is still covered)."""
    tools = await mcp_list_tools()
    schema = next(t for t in tools if t.name == "artifact_generate").inputSchema
    props = schema.get("properties", {})

    # Single-kind option params: enum == that kind's choice set.
    single_kind = {
        "report_format": "report",
        "audio_format": "audio",
        "audio_length": "audio",
        "video_format": "video",
        "deck_format": "slide-deck",
        "deck_length": "slide-deck",
        "orientation": "infographic",
        "detail": "infographic",
        "map_kind": "mind-map",
    }
    for param, kind in single_kind.items():
        assert _schema_enum(props[param]) == set(_KIND_OPTIONS[kind][param]), param

    # quantity/difficulty: shared by quiz+flashcards (union).
    for param in ("quantity", "difficulty"):
        expected = set(_KIND_OPTIONS["quiz"][param]) | set(_KIND_OPTIONS["flashcards"][param])
        assert _schema_enum(props[param]) == expected, param

    # style: single union Literal across video + infographic.
    expected_style = set(_KIND_OPTIONS["video"]["style"]) | set(
        _KIND_OPTIONS["infographic"]["style"]
    )
    assert _schema_enum(props["style"]) == expected_style


async def test_artifact_generate_free_text_params_have_no_enum(mcp_list_tools) -> None:
    """``style_prompt`` and ``language`` stay free text — NOT converted to ``Literal``.

    Uses the same nested-aware ``_schema_enum`` helper so an accidental conversion that
    hid an ``enum`` inside an ``anyOf`` branch would still be caught."""
    tools = await mcp_list_tools()
    schema = next(t for t in tools if t.name == "artifact_generate").inputSchema
    props = schema.get("properties", {})
    assert _schema_enum(props["style_prompt"]) is None
    assert _schema_enum(props["language"]) is None


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
# artifact_get_prompt
# ---------------------------------------------------------------------------


async def test_artifact_get_prompt(mcp_call, mock_client) -> None:
    """Happy path: the stored prompt string flows through unchanged."""
    mock_client.artifacts.get_prompt = AsyncMock(return_value="Summarize the intro")
    result = await mcp_call("artifact_get_prompt", {"notebook": NB_ID, "artifact": _ART_FULL})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "artifact_id": _ART_FULL,
        "prompt": "Summarize the intro",
    }
    # Full-UUID ref fast-paths: the resolver never lists artifacts.
    mock_client.artifacts.list.assert_not_called()
    mock_client.artifacts.get_prompt.assert_awaited_once_with(NB_ID, _ART_FULL)


async def test_artifact_get_prompt_none_is_success(mcp_call, mock_client) -> None:
    """``prompt=None`` (artifact records no prompt) is a valid result, not an error."""
    mock_client.artifacts.get_prompt = AsyncMock(return_value=None)
    result = await mcp_call("artifact_get_prompt", {"notebook": NB_ID, "artifact": _ART_FULL})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "artifact_id": _ART_FULL,
        "prompt": None,
    }


async def test_artifact_get_prompt_resolves_by_title(mcp_call, mock_client) -> None:
    """A title/prefix ref resolves to the artifact id before the prompt fetch."""
    art = Artifact(
        id=_ART_FULL,
        title="Podcast 1",
        _artifact_type=ArtifactTypeCode.AUDIO.value,
        status=int(ArtifactStatus.COMPLETED),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    mock_client.artifacts.list = AsyncMock(return_value=[art])
    mock_client.artifacts.get_prompt = AsyncMock(return_value="From the podcast")
    result = await mcp_call("artifact_get_prompt", {"notebook": NB_ID, "artifact": "Podcast 1"})
    assert result.structured_content["artifact_id"] == _ART_FULL
    assert result.structured_content["prompt"] == "From the podcast"
    mock_client.artifacts.get_prompt.assert_awaited_once_with(NB_ID, _ART_FULL)


async def test_artifact_get_prompt_unknown_id_projects_tool_error(mcp_call, mock_client) -> None:
    """An unknown id raises ``ArtifactNotFoundError`` (mapped to NOT_FOUND).

    ``get_prompt`` has no pre-list existence guard — the full-UUID ref reaches the
    client, whose ``get_prompt`` raises for an absent artifact."""

    def _raise(*_a: Any, **_k: Any) -> Any:
        raise ArtifactNotFoundError(_ART_FULL)

    mock_client.artifacts.get_prompt = AsyncMock(side_effect=_raise)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("artifact_get_prompt", {"notebook": NB_ID, "artifact": _ART_FULL})
    assert "NOT_FOUND" in str(excinfo.value)


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


async def test_artifact_download_by_artifact_ref_infers_type(
    mcp_call, mock_client, tmp_path
) -> None:
    """R3: an ``artifact`` name-or-id ref resolves to its type+id — no ``artifact_type``."""
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT])
    mock_client.artifacts.download_audio = AsyncMock(return_value=out)
    result = await mcp_call(
        "artifact_download", {"notebook": NB_ID, "artifact": "Podcast", "path": out}
    )
    assert result.structured_content["outcome"] == "single_downloaded"
    # The audio downloader was selected purely from the resolved artifact's kind,
    # AND the resolved id (not latest-by-type) reached it — guards a regression to
    # latest-by-type that a bare assert_awaited_once() would miss.
    assert result.structured_content["artifact"]["id"] == "art1"
    assert mock_client.artifacts.download_audio.await_args.kwargs["artifact_id"] == "art1"


async def test_artifact_download_ref_and_type_together_is_validation(mcp_call, mock_client) -> None:
    """Passing both ``artifact`` and ``artifact_type`` is rejected (one addressing mode)."""
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT])
    with pytest.raises(ToolError) as exc:
        await mcp_call(
            "artifact_download",
            {"notebook": NB_ID, "artifact": "Podcast", "artifact_type": "audio"},
        )
    assert "not both" in str(exc.value)


async def test_artifact_download_ref_and_id_together_is_validation(mcp_call, mock_client) -> None:
    """Passing `artifact` alongside `artifact_id` is rejected (would silently drop the id)."""
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT])
    with pytest.raises(ToolError) as exc:
        await mcp_call(
            "artifact_download",
            {"notebook": NB_ID, "artifact": "Podcast", "artifact_id": "art1"},
        )
    assert "not both" in str(exc.value)


async def test_artifact_download_neither_ref_nor_type_is_validation(mcp_call, mock_client) -> None:
    """Omitting both ``artifact`` and ``artifact_type`` is rejected."""
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT])
    with pytest.raises(ToolError) as exc:
        await mcp_call("artifact_download", {"notebook": NB_ID})
    assert "artifact_type" in str(exc.value)


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
    """A bad ``format`` for a type that DOES support format projects a Literal schema boundary error."""
    out = str(tmp_path / "quiz.json")
    mock_client.artifacts.list = AsyncMock(return_value=[_QUIZ_ARTIFACT])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_download",
            {"notebook": NB_ID, "artifact_type": "quiz", "path": out, "output_format": "bogus"},
        )
    assert "validation error" in str(excinfo.value)


async def test_artifact_download_bad_format_cross_validation_is_validation(
    mcp_call, mock_client, tmp_path
) -> None:
    """An in-union format value that is invalid for the specific type raises a runtime VALIDATION error."""
    out = str(tmp_path / "quiz.json")
    mock_client.artifacts.list = AsyncMock(return_value=[_QUIZ_ARTIFACT])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_download",
            {"notebook": NB_ID, "artifact_type": "quiz", "path": out, "output_format": "pdf"},
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


_AUDIO_ARTIFACT_1 = Artifact(
    id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    title="Podcast 1",
    _artifact_type=ArtifactTypeCode.AUDIO.value,
    status=int(ArtifactStatus.COMPLETED),
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
)
_AUDIO_ARTIFACT_2 = Artifact(
    id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    title="Podcast 2",
    _artifact_type=ArtifactTypeCode.AUDIO.value,
    status=int(ArtifactStatus.COMPLETED),
    created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
)


async def test_artifact_download_by_full_id(mcp_call, mock_client, tmp_path) -> None:
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT_1, _AUDIO_ARTIFACT_2])
    mock_client.artifacts.download_audio = AsyncMock(return_value=out)
    result = await mcp_call(
        "artifact_download",
        {
            "notebook": NB_ID,
            "artifact_type": "audio",
            "path": out,
            "artifact_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        },
    )
    assert result.structured_content["outcome"] == "single_downloaded"
    assert result.structured_content["artifact"]["id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    mock_client.artifacts.download_audio.assert_awaited_once_with(
        NB_ID, out, artifact_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )


async def test_artifact_download_by_unique_prefix(mcp_call, mock_client, tmp_path) -> None:
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT_1, _AUDIO_ARTIFACT_2])
    mock_client.artifacts.download_audio = AsyncMock(return_value=out)
    result = await mcp_call(
        "artifact_download",
        {
            "notebook": NB_ID,
            "artifact_type": "audio",
            "path": out,
            "artifact_id": "bbbbbbbb-bbbb",
        },
    )
    assert result.structured_content["outcome"] == "single_downloaded"
    assert result.structured_content["artifact"]["id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    mock_client.artifacts.download_audio.assert_awaited_once_with(
        NB_ID, out, artifact_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    )


async def test_artifact_download_by_id_not_found(mcp_call, mock_client, tmp_path) -> None:
    # A not-found ``artifact_id`` (a full UUID absent from the list) is a hard miss,
    # uniform with a not-found / ambiguous prefix — ``_resolve_artifact_id`` raises
    # before the download core's soft ERROR path, mirroring how a bad notebook id
    # surfaces (ToolError / NOT_FOUND).
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT_1, _AUDIO_ARTIFACT_2])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_download",
            {
                "notebook": NB_ID,
                "artifact_type": "audio",
                "path": out,
                "artifact_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            },
        )
    assert "not found" in str(excinfo.value)
    mock_client.artifacts.download_audio.assert_not_called()


async def test_artifact_download_by_uppercase_full_id(mcp_call, mock_client, tmp_path) -> None:
    # An uppercase full UUID must still resolve: resolve_ref fast-paths it verbatim,
    # so _resolve_artifact_id case-insensitively matches it back to the list's
    # canonical (lowercase) id that select_artifact compares against.
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT_1, _AUDIO_ARTIFACT_2])
    mock_client.artifacts.download_audio = AsyncMock(return_value=out)
    result = await mcp_call(
        "artifact_download",
        {
            "notebook": NB_ID,
            "artifact_type": "audio",
            "path": out,
            "artifact_id": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        },
    )
    assert result.structured_content["outcome"] == "single_downloaded"
    assert result.structured_content["artifact"]["id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    mock_client.artifacts.download_audio.assert_awaited_once_with(
        NB_ID, out, artifact_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )


async def test_artifact_download_by_id_ambiguous_prefix(mcp_call, mock_client, tmp_path) -> None:
    out = str(tmp_path / "out.mp3")
    art_same_1 = Artifact(
        id="cccccccc-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        title="Podcast A",
        _artifact_type=ArtifactTypeCode.AUDIO.value,
        status=int(ArtifactStatus.COMPLETED),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    art_same_2 = Artifact(
        id="cccccccc-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        title="Podcast B",
        _artifact_type=ArtifactTypeCode.AUDIO.value,
        status=int(ArtifactStatus.COMPLETED),
        created_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    mock_client.artifacts.list = AsyncMock(return_value=[art_same_1, art_same_2])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_download",
            {
                "notebook": NB_ID,
                "artifact_type": "audio",
                "path": out,
                "artifact_id": "cccccccc",
            },
        )
    assert "Ambiguous ID" in str(excinfo.value)
    mock_client.artifacts.download_audio.assert_not_called()


async def test_artifact_download_latest_preserved(mcp_call, mock_client, tmp_path) -> None:
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT_1, _AUDIO_ARTIFACT_2])
    mock_client.artifacts.download_audio = AsyncMock(return_value=out)
    result = await mcp_call(
        "artifact_download",
        {
            "notebook": NB_ID,
            "artifact_type": "audio",
            "path": out,
        },
    )
    assert result.structured_content["outcome"] == "single_downloaded"
    assert result.structured_content["artifact"]["id"] == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    mock_client.artifacts.download_audio.assert_awaited_once_with(
        NB_ID, out, artifact_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    )


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


# ---------------------------------------------------------------------------
# artifact_rename
# ---------------------------------------------------------------------------

_ART_FULL = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


async def test_artifact_rename_regular_typed_artifact(mcp_call, mock_client) -> None:
    """A regular artifact resolves via the typed ``Artifact`` list (NOT a dict) and
    routes to ``artifacts.rename``.

    Regression guard: the resolver must use attribute access (``a.id`` / ``a.title``)
    on the typed ``Artifact`` objects ``client.artifacts.list`` returns. The earlier
    dict-shaped helper would ``TypeError`` here. Resolving by a hex prefix exercises
    the id/prefix path against the typed list.
    """
    art = Artifact(
        id=_ART_FULL,
        title="Podcast 1",
        _artifact_type=ArtifactTypeCode.AUDIO.value,
        status=int(ArtifactStatus.COMPLETED),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    mock_client.artifacts.list = AsyncMock(return_value=[art])
    mock_client.mind_maps.list = AsyncMock(return_value=[])
    mock_client.artifacts.rename = AsyncMock()
    result = await mcp_call(
        "artifact_rename",
        {"notebook": NB_ID, "artifact": "aaaaaaaa-aaaa", "new_title": "Renamed"},
    )
    assert result.structured_content == {
        "status": "renamed",
        "notebook_id": NB_ID,
        "artifact_id": _ART_FULL,
        "new_title": "Renamed",
        "is_mind_map": False,
    }
    mock_client.artifacts.rename.assert_awaited_once_with(
        NB_ID, _ART_FULL, "Renamed", return_object=False
    )
    mock_client.mind_maps.rename.assert_not_called()


async def test_artifact_rename_interactive_mind_map_by_title(mcp_call, mock_client) -> None:
    """A mind map resolved by title routes through ``mind_maps.rename`` (is_mind_map true)."""
    mm_id = "mmmmmmmm-mmmm-mmmm-mmmm-mmmmmmmmmmmm"
    mock_client.artifacts.list = AsyncMock(
        return_value=[FakeArtifact(id=mm_id, title="My Map", kind=ArtifactType.MIND_MAP)]
    )
    mock_client.mind_maps.list = AsyncMock(
        return_value=[FakeMindMap(id=mm_id, kind=MindMapKind.INTERACTIVE)]
    )
    mock_client.mind_maps.rename = AsyncMock()
    result = await mcp_call(
        "artifact_rename",
        {"notebook": NB_ID, "artifact": "My Map", "new_title": "Renamed Map"},
    )
    assert result.structured_content["is_mind_map"] is True
    assert result.structured_content["artifact_id"] == mm_id
    mock_client.mind_maps.rename.assert_awaited_once()
    mock_client.artifacts.rename.assert_not_called()


async def test_artifact_rename_note_backed_mind_map_by_full_uuid(mcp_call, mock_client) -> None:
    """A full-UUID ref reaches the core unlisted; the core's ``mind_maps.list`` probe
    finds the note-backed map → ``mind_maps.rename`` with its kind."""
    mock_client.mind_maps.list = AsyncMock(
        return_value=[FakeMindMap(id=_ART_FULL, kind=MindMapKind.NOTE_BACKED)]
    )
    mock_client.mind_maps.rename = AsyncMock()
    result = await mcp_call(
        "artifact_rename",
        {"notebook": NB_ID, "artifact": _ART_FULL, "new_title": "Renamed"},
    )
    assert result.structured_content["is_mind_map"] is True
    assert result.structured_content["artifact_id"] == _ART_FULL
    # Full-UUID fast-path: the resolver never lists artifacts.
    mock_client.artifacts.list.assert_not_called()
    mock_client.mind_maps.rename.assert_awaited_once()
    assert mock_client.mind_maps.rename.await_args.kwargs["kind"] == MindMapKind.NOTE_BACKED


async def test_artifact_rename_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    """A prefix/title that matches no artifact projects NOT_FOUND (resolver raises
    ``ArtifactNotFoundError``, which ``mcp_errors`` maps to a ``ToolError``)."""
    mock_client.artifacts.list = AsyncMock(return_value=[])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_rename",
            {"notebook": NB_ID, "artifact": "No Such Artifact", "new_title": "X"},
        )
    assert "NOT_FOUND" in str(excinfo.value)
    mock_client.artifacts.rename.assert_not_called()
    # The tool layer asserts the wrapped ToolError/NOT_FOUND; the raw
    # ArtifactNotFoundError is asserted at the resolver layer in test_resolve.py.


# ---------------------------------------------------------------------------
# artifact_retry
# ---------------------------------------------------------------------------


async def test_artifact_retry_happy_path(mcp_call, mock_client) -> None:
    """A retry returns the kicked-off ``task_id`` (== artifact id) and new status."""
    mock_client.artifacts.retry_failed = AsyncMock(
        return_value=FakeStatus(task_id=_ART_FULL, status=GenerationState.IN_PROGRESS, url=None)
    )
    result = await mcp_call("artifact_retry", {"notebook": NB_ID, "artifact": _ART_FULL})
    assert result.structured_content == {
        "notebook_id": NB_ID,
        "artifact_id": _ART_FULL,
        "task_id": _ART_FULL,
        "status": "in_progress",
    }
    # Full-UUID ref fast-paths: the resolver never lists artifacts.
    mock_client.artifacts.list.assert_not_called()
    mock_client.artifacts.retry_failed.assert_awaited_once_with(NB_ID, _ART_FULL)


async def test_artifact_retry_resolves_by_title(mcp_call, mock_client) -> None:
    """A title/prefix ref resolves to the artifact id before the retry call."""
    art = Artifact(
        id=_ART_FULL,
        title="Podcast 1",
        _artifact_type=ArtifactTypeCode.AUDIO.value,
        status=int(ArtifactStatus.FAILED),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    mock_client.artifacts.list = AsyncMock(return_value=[art])
    mock_client.artifacts.retry_failed = AsyncMock(
        return_value=FakeStatus(task_id=_ART_FULL, status=GenerationState.IN_PROGRESS, url=None)
    )
    result = await mcp_call("artifact_retry", {"notebook": NB_ID, "artifact": "Podcast 1"})
    assert result.structured_content["artifact_id"] == _ART_FULL
    assert result.structured_content["task_id"] == _ART_FULL
    mock_client.artifacts.retry_failed.assert_awaited_once_with(NB_ID, _ART_FULL)


async def test_artifact_retry_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    """A prefix/title that matches no artifact projects NOT_FOUND at resolve time."""
    mock_client.artifacts.list = AsyncMock(return_value=[])
    mock_client.artifacts.retry_failed = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("artifact_retry", {"notebook": NB_ID, "artifact": "No Such Artifact"})
    assert "NOT_FOUND" in str(excinfo.value)
    mock_client.artifacts.retry_failed.assert_not_called()


async def test_artifact_retry_refusal_projects_tool_error(mcp_call, mock_client) -> None:
    """A synchronous client refusal (rate limit / quota) surfaces as a ToolError."""

    def _raise(*_a: Any, **_k: Any) -> Any:
        raise RateLimitError("quota exceeded")

    mock_client.artifacts.retry_failed = AsyncMock(side_effect=_raise)
    with pytest.raises(ToolError):
        await mcp_call("artifact_retry", {"notebook": NB_ID, "artifact": _ART_FULL})


# ---------------------------------------------------------------------------
# artifact_delete
# ---------------------------------------------------------------------------


async def test_artifact_delete_confirm_false_previews(mcp_call, mock_client) -> None:
    """``confirm=False`` returns a ``needs_confirmation`` preview and does NOT delete."""
    art = Artifact(
        id=_ART_FULL,
        title="Podcast 1",
        _artifact_type=ArtifactTypeCode.AUDIO.value,
        status=int(ArtifactStatus.COMPLETED),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    mock_client.artifacts.list = AsyncMock(return_value=[art])
    mock_client.artifacts.delete = AsyncMock()
    # No list_note_backed mock: the confirm=False path returns the preview before
    # ever reaching the delete core (which is what probes list_note_backed).
    result = await mcp_call(
        "artifact_delete",
        {"notebook": NB_ID, "artifact": "aaaaaaaa-aaaa"},
    )
    assert result.structured_content["status"] == "needs_confirmation"
    preview = result.structured_content["preview"]
    assert preview["action"] == "delete_artifact"
    assert preview["artifact_id"] == _ART_FULL
    assert preview["title"] == "Podcast 1"
    mock_client.artifacts.delete.assert_not_called()
    mock_client.notes.delete.assert_not_called()


async def test_artifact_delete_regular_confirmed(mcp_call, mock_client) -> None:
    """``confirm=True`` on a regular artifact hits ``artifacts.delete`` (was_note_backed false)."""
    art = Artifact(
        id=_ART_FULL,
        title="Podcast 1",
        _artifact_type=ArtifactTypeCode.AUDIO.value,
        status=int(ArtifactStatus.COMPLETED),
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    mock_client.artifacts.list = AsyncMock(return_value=[art])
    mock_client.mind_maps.list_note_backed = AsyncMock(return_value=[])
    mock_client.artifacts.delete = AsyncMock()
    result = await mcp_call(
        "artifact_delete",
        {"notebook": NB_ID, "artifact": "aaaaaaaa-aaaa", "confirm": True},
    )
    assert result.structured_content == {
        "status": "deleted",
        "notebook_id": NB_ID,
        "artifact_id": _ART_FULL,
        "was_note_backed": False,
    }
    mock_client.artifacts.delete.assert_awaited_once_with(NB_ID, _ART_FULL)
    mock_client.notes.delete.assert_not_called()


async def test_artifact_delete_note_backed_by_full_uuid_confirmed(mcp_call, mock_client) -> None:
    """A note-backed mind map (full-UUID ref) is cleared via ``notes.delete``
    (was_note_backed true). The core's probe is ``mind_maps.list_note_backed``."""
    mock_client.mind_maps.list_note_backed = AsyncMock(return_value=[FakeMindMap(id=_ART_FULL)])
    mock_client.notes.delete = AsyncMock()
    mock_client.artifacts.delete = AsyncMock()
    result = await mcp_call(
        "artifact_delete",
        {"notebook": NB_ID, "artifact": _ART_FULL, "confirm": True},
    )
    assert result.structured_content["was_note_backed"] is True
    assert result.structured_content["artifact_id"] == _ART_FULL
    # Full-UUID fast-path: the resolver never lists artifacts.
    mock_client.artifacts.list.assert_not_called()
    mock_client.notes.delete.assert_awaited_once_with(NB_ID, _ART_FULL)
    mock_client.artifacts.delete.assert_not_called()


async def test_artifact_delete_note_backed_by_title_confirmed(mcp_call, mock_client) -> None:
    """A note-backed mind map resolved by title also routes through ``notes.delete``."""
    mm_id = "mmmmmmmm-mmmm-mmmm-mmmm-mmmmmmmmmmmm"
    mock_client.artifacts.list = AsyncMock(
        return_value=[FakeArtifact(id=mm_id, title="My Map", kind=ArtifactType.MIND_MAP)]
    )
    mock_client.mind_maps.list_note_backed = AsyncMock(return_value=[FakeMindMap(id=mm_id)])
    mock_client.notes.delete = AsyncMock()
    mock_client.artifacts.delete = AsyncMock()
    result = await mcp_call(
        "artifact_delete",
        {"notebook": NB_ID, "artifact": "My Map", "confirm": True},
    )
    assert result.structured_content["was_note_backed"] is True
    assert result.structured_content["artifact_id"] == mm_id
    mock_client.notes.delete.assert_awaited_once_with(NB_ID, mm_id)
    mock_client.artifacts.delete.assert_not_called()


async def test_artifact_delete_absent_full_uuid_is_idempotent(mcp_call, mock_client) -> None:
    """Deleting an already-absent full UUID is a no-error no-op: the full-UUID
    fast-path reaches the core unlisted, the note-backed probe misses, and
    ``artifacts.delete`` (idempotent on missing) runs without raising."""
    absent = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    mock_client.mind_maps.list_note_backed = AsyncMock(return_value=[])
    mock_client.artifacts.delete = AsyncMock()
    result = await mcp_call(
        "artifact_delete",
        {"notebook": NB_ID, "artifact": absent, "confirm": True},
    )
    assert result.structured_content == {
        "status": "deleted",
        "notebook_id": NB_ID,
        "artifact_id": absent,
        "was_note_backed": False,
    }
    mock_client.artifacts.list.assert_not_called()
    mock_client.artifacts.delete.assert_awaited_once_with(NB_ID, absent)


async def test_artifact_delete_absent_prefix_projects_tool_error(mcp_call, mock_client) -> None:
    """An absent prefix/title raises at resolve time (NOT_FOUND), never reaching the
    core — distinct from the idempotent absent-full-UUID case above."""
    mock_client.artifacts.list = AsyncMock(return_value=[])
    mock_client.artifacts.delete = AsyncMock()
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_delete",
            {"notebook": NB_ID, "artifact": "No Such Artifact", "confirm": True},
        )
    assert "NOT_FOUND" in str(excinfo.value)
    mock_client.artifacts.delete.assert_not_called()
    mock_client.notes.delete.assert_not_called()
