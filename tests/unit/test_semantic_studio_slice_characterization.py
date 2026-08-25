"""Migration sentinels and inventory characterization for the P5 Studio/Artifacts slice.

This characterization suite freezes and validates the complete ArtifactsAPI and Studio
contract before P5 family services decompose the internal verb services:
1. Every current Artifact field and nested value populated by list/get with no extra fetch;
2. Exact family classification across all 13 types/variants including unknown safe summary;
3. Lifecycle-terminal GenerationStatus return and wait_for_completion polling semantics;
4. GenerationState (str, Enum) base order invariant and terminal frozenset hash lookup;
5. Download client factory, trusted-host allowlist, and redirect security parity for httpx and curl_cffi;
6. Drive export operations and uncommon mind-map/table paths including ADR-0019 partial-availability.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from notebooklm import (
    Artifact,
    ArtifactInfographic,
    ArtifactMedia,
    ArtifactMediaType,
    ArtifactSlide,
    ArtifactType,
    AudioArtifactUserState,
    ExportType,
    GenerationState,
    GenerationStatus,
    ReportFormat,
    UnknownTypeWarning,
)
from notebooklm._artifact._download_client import (
    _is_trusted_download_host,
    _make_download_client,
)
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._types.artifacts import (
    _TERMINAL_GENERATION_STATES,
    _warned_artifact_types,
)
from notebooklm.exceptions import (
    ArtifactDownloadError,
    ArtifactNotFoundError,
    DecodingError,
    NetworkError,
    RPCError,
    ValidationError,
)
from notebooklm.rpc import (
    FLASHCARDS_VARIANT,
    INTERACTIVE_MIND_MAP_VARIANT,
    QUIZ_VARIANT,
    ArtifactStatus,
    ArtifactTypeCode,
    RPCMethod,
)
from tests._fixtures.fake_core import make_fake_core
from tests._fixtures.web_backend import build_web_backend
from tests._helpers.signature_inspection import signature_parameters


def _make_api(
    rpc_call: AsyncMock | None = None,
    list_mind_maps_return: list[Any] | None = None,
) -> tuple[ArtifactsAPI, Any, MagicMock]:
    """Construct an ArtifactsAPI instance with isolated mock collaborators."""
    studio_rpc_call = rpc_call or AsyncMock(return_value=[])
    mock_notebooks = MagicMock()
    mock_notebooks.get_source_ids = AsyncMock(return_value=[])
    mock_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    if list_mind_maps_return is not None:
        mock_mind_maps.list_mind_maps = AsyncMock(return_value=list_mind_maps_return)
    else:
        mock_mind_maps.list_mind_maps = AsyncMock(return_value=[])

    async def routed_rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        if method is RPCMethod.GET_NOTES_AND_MIND_MAPS:
            return [await mock_mind_maps.list_mind_maps(params[0])]
        return await studio_rpc_call(method, params, **kwargs)

    mock_core = make_fake_core(
        rpc_call=AsyncMock(side_effect=routed_rpc_call),
        get_source_ids=AsyncMock(return_value=[]),
    )
    api = ArtifactsAPI(
        drain=mock_core,
        lifecycle=mock_core,
        notebooks=mock_notebooks,
        mind_maps=mock_mind_maps,
        _backend=build_web_backend(mock_core),
    )
    return api, mock_core, mock_mind_maps


def _build_full_studio_row(
    artifact_id: str = "art-100",
    title: str = "Quantum Computing Overview",
    type_code: int = ArtifactTypeCode.AUDIO.value,
    status: int = ArtifactStatus.COMPLETED.value,
    variant: int | None = None,
) -> list[Any]:
    """Construct a full 22-element LIST_ARTIFACTS row with all known slots populated."""
    row: list[Any] = [None] * 22
    # [0]=id, [1]=title, [2]=type_code, [3]=sources, [4]=status, [5]=isPubliclyReadable
    row[0] = artifact_id
    row[1] = title
    row[2] = type_code
    row[3] = [[["src-alpha"]], [["src-beta"]]]
    row[4] = status
    row[5] = False
    # [6]=audio metadata: [None, [prompt], None, None, None, media_urls_list, duration_nanos]
    row[6] = [
        None,
        ["Explain quantum computing in plain terms"],
        None,
        None,
        None,
        [
            ["https://storage.googleapis.com/audio.mp4", 1, "audio/mp4"],
            ["https://storage.googleapis.com/audio.m3u8", 2, None],
        ],
        [872, 489_796_000],
    ]
    # [7]=report metadata: [markdown, [None*5, prompt, [report_kind]]]
    row[7] = [
        "# Markdown report",
        [
            None,
            None,
            None,
            None,
            None,
            "Explain quantum computing in plain terms",
            ["Concept Explanation"],
        ],
    ]
    # [8]=video metadata: [None, None, [None, None, "Video Prompt"]]
    row[8] = None
    # [9]=quiz/flashcard metadata: [None, [variant_code, None, "Quiz prompt"]]
    if variant is not None:
        row[9] = [None, [variant, None, "Quiz prompt"]]
    # [10]=last modified timestamp: [seconds, nanos]
    row[10] = [1_700_000_100, 250_000_000]
    # [14]=infographic metadata: [[prompt], title, [[sub_title, [url, w, h], alt, text]]]
    row[14] = [
        ["Explain quantum computing visually"],
        "Quantum Infographic",
        [
            [
                "Core Qubits",
                ["https://storage.googleapis.com/info1.png", 2752, 1536],
                "Diagram of entangled qubits",
                "Superposition and Entanglement",
            ]
        ],
    ]
    # [15]=created_at timestamp: [seconds, nanos]
    row[15] = [1_700_000_000, 0]
    # [16]=slide deck metadata: [[prompt], title, [[[url, w, h], alt, text]], pdf_url]
    row[16] = [
        ["Generate slide deck about quantum"],
        "Quantum Slides",
        [
            [
                ["https://storage.googleapis.com/slide1.png", 1376, 768],
                "Intro slide diagram",
                "SLIDE 1: Quantum Supremacy",
            ]
        ],
        "https://storage.googleapis.com/deck.pdf",
    ]
    # [17]=user state: audio resume position [[[seconds, nanos]]]
    row[17] = [[[123, 500_000_000]]]
    # [21]=etag
    row[21] = '"etag-studio-rev-42"'
    return row


# ===========================================================================
# 1. Public Signatures and Method Inventory
# ===========================================================================


def test_artifacts_api_public_signatures_are_frozen() -> None:
    """Freeze all public method signatures on ArtifactsAPI.

    P5 may split internal execution into family services, but ArtifactsAPI
    remains the stable facade with unchanged public signatures.
    """
    # Listing & Discovery
    assert list(signature_parameters(ArtifactsAPI.list)) == [
        "self",
        "notebook_id",
        "artifact_type",
    ]
    assert list(signature_parameters(ArtifactsAPI.get)) == [
        "self",
        "notebook_id",
        "artifact_id",
    ]
    assert list(signature_parameters(ArtifactsAPI.get_or_none)) == [
        "self",
        "notebook_id",
        "artifact_id",
    ]
    assert list(signature_parameters(ArtifactsAPI.get_prompt)) == [
        "self",
        "notebook_id",
        "artifact_id",
    ]

    # Family listing shortcuts
    for method_name in (
        "list_audio",
        "list_video",
        "list_reports",
        "list_quizzes",
        "list_flashcards",
        "list_infographics",
        "list_slide_decks",
        "list_data_tables",
    ):
        assert list(signature_parameters(getattr(ArtifactsAPI, method_name))) == [
            "self",
            "notebook_id",
        ]

    # Generation signatures
    gen_audio = signature_parameters(ArtifactsAPI.generate_audio)
    assert list(gen_audio) == [
        "self",
        "notebook_id",
        "source_ids",
        "language",
        "instructions",
        "audio_format",
        "audio_length",
    ]
    assert gen_audio["source_ids"].default is None
    assert gen_audio["language"].default == "en"

    gen_quiz = signature_parameters(ArtifactsAPI.generate_quiz)
    assert list(gen_quiz) == [
        "self",
        "notebook_id",
        "source_ids",
        "instructions",
        "quantity",
        "difficulty",
    ]
    assert gen_quiz["quantity"].default is None
    assert gen_quiz["difficulty"].default is None

    gen_report = signature_parameters(ArtifactsAPI.generate_report)
    assert list(gen_report) == [
        "self",
        "notebook_id",
        "report_format",
        "source_ids",
        "language",
        "custom_prompt",
        "extra_instructions",
    ]
    assert gen_report["report_format"].default is ReportFormat.BRIEFING_DOC

    # Polling & Lifecycle
    wait_sig = signature_parameters(ArtifactsAPI.wait_for_completion)
    assert list(wait_sig) == [
        "self",
        "notebook_id",
        "task_id",
        "initial_interval",
        "max_interval",
        "timeout",
        "max_not_found",
        "min_not_found_window",
        "on_status_change",
    ]
    assert wait_sig["initial_interval"].default == 2.0
    assert wait_sig["max_interval"].default == 10.0
    assert wait_sig["timeout"].default == 300.0
    assert wait_sig["max_not_found"].default == 5
    assert wait_sig["min_not_found_window"].default == 10.0
    assert wait_sig["on_status_change"].default is None

    # Mutation & Export
    rename_sig = signature_parameters(ArtifactsAPI.rename)
    assert list(rename_sig) == ["self", "notebook_id", "artifact_id", "new_title", "return_object"]
    assert rename_sig["return_object"].kind is inspect.Parameter.KEYWORD_ONLY
    assert rename_sig["return_object"].default is True

    assert list(signature_parameters(ArtifactsAPI.delete)) == [
        "self",
        "notebook_id",
        "artifact_id",
    ]
    assert list(signature_parameters(ArtifactsAPI.export_report)) == [
        "self",
        "notebook_id",
        "artifact_id",
        "title",
        "export_type",
    ]
    assert list(signature_parameters(ArtifactsAPI.export_data_table)) == [
        "self",
        "notebook_id",
        "artifact_id",
        "title",
    ]
    export_sig = signature_parameters(ArtifactsAPI.export)
    assert list(export_sig) == [
        "self",
        "notebook_id",
        "artifact_id",
        "title",
        "export_type",
        "content",
    ]
    assert export_sig["content"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_artifacts_list_preserves_runtime_string_filter_compatibility() -> None:
    audio_row = _build_full_studio_row(type_code=ArtifactTypeCode.AUDIO.value)
    video_row = _build_full_studio_row(
        artifact_id="art-video",
        type_code=ArtifactTypeCode.VIDEO.value,
    )
    rpc_call = AsyncMock(return_value=[[audio_row, video_row]])
    api, _mock_core, _mock_mind_maps = _make_api(rpc_call=rpc_call)

    artifacts = await api.list("nb-1", "audio")  # type: ignore[arg-type]

    assert [artifact.id for artifact in artifacts] == ["art-100"]


@pytest.mark.asyncio
async def test_artifacts_list_drops_empty_studio_rows() -> None:
    api, _mock_core, _mock_mind_maps = _make_api(rpc_call=AsyncMock(return_value=[[[]]]))

    assert await api.list("nb-1") == []


# ===========================================================================
# 2. Every Current Artifact Field & Nested Value Populated by list/get
# ===========================================================================


@pytest.mark.asyncio
async def test_list_and_get_populate_every_artifact_field_and_nested_value_without_extra_fetch() -> (
    None
):
    """P5 compatibility invariant: list and get populate every Artifact field in one RPC pass.

    No extra fetches are issued per-artifact; all nested media, slides, infographics,
    source IDs, timestamps, prompt, etag, and user states survive projection.
    """
    audio_row = _build_full_studio_row(
        artifact_id="art-audio",
        title="Quantum Audio",
        type_code=ArtifactTypeCode.AUDIO.value,
    )
    slide_row = _build_full_studio_row(
        artifact_id="art-slides",
        title="Quantum Slides",
        type_code=ArtifactTypeCode.SLIDE_DECK.value,
    )
    info_row = _build_full_studio_row(
        artifact_id="art-info",
        title="Quantum Infographic",
        type_code=ArtifactTypeCode.INFOGRAPHIC.value,
    )

    rpc_call = AsyncMock(return_value=[[audio_row, slide_row, info_row]])
    api, mock_core, mock_mind_maps = _make_api(rpc_call=rpc_call)

    # 1. Test api.list()
    artifacts = await api.list("nb-1")
    assert len(artifacts) == 3

    # Assert exactly 1 LIST_ARTIFACTS call and 1 GET_NOTES_AND_MIND_MAPS sub-fetch
    assert rpc_call.await_count == 1
    assert rpc_call.await_args_list[0][0][0] == RPCMethod.LIST_ARTIFACTS
    mock_mind_maps.list_mind_maps.assert_awaited_once_with("nb-1")

    # 2. Verify all Audio fields and nested values
    audio = artifacts[0]
    assert audio.id == "art-audio"
    assert audio.title == "Quantum Audio"
    assert audio.kind is ArtifactType.AUDIO
    assert audio.status == ArtifactStatus.COMPLETED.value
    assert audio.status_str == "completed"
    assert audio.is_completed is True
    assert audio.is_failed is False
    assert audio.is_processing is False
    assert audio.is_pending is False
    assert audio.created_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert audio.last_modified_at == datetime(2023, 11, 14, 22, 15, 0, 250_000, tzinfo=timezone.utc)
    assert audio.url == "https://storage.googleapis.com/audio.mp4"
    assert audio.generation_prompt == "Explain quantum computing in plain terms"
    assert audio.duration_seconds == 872.489796
    assert audio.source_ids == ("src-alpha", "src-beta")
    assert audio.etag == '"etag-studio-rev-42"'
    assert len(audio.media_urls) == 2
    assert audio.media_urls[0] == ArtifactMedia(
        url="https://storage.googleapis.com/audio.mp4",
        kind=ArtifactMediaType.PROGRESSIVE,
        type_code=1,
        mime_type="audio/mp4",
    )
    assert audio.media_urls[1] == ArtifactMedia(
        url="https://storage.googleapis.com/audio.m3u8",
        kind=ArtifactMediaType.HLS,
        type_code=2,
        mime_type=None,
    )
    assert audio.user_state == AudioArtifactUserState(playback_position_seconds=123.5)

    # 3. Verify Slide Deck nested fields
    slides_art = artifacts[1]
    assert slides_art.kind is ArtifactType.SLIDE_DECK
    assert slides_art.url == "https://storage.googleapis.com/deck.pdf"
    assert len(slides_art.slides) == 1
    assert slides_art.slides[0] == ArtifactSlide(
        image_url="https://storage.googleapis.com/slide1.png",
        width=1376,
        height=768,
        alt_text="Intro slide diagram",
        text="SLIDE 1: Quantum Supremacy",
    )

    # 4. Verify Infographic nested fields
    info_art = artifacts[2]
    assert info_art.kind is ArtifactType.INFOGRAPHIC
    assert info_art.url == "https://storage.googleapis.com/info1.png"
    assert len(info_art.infographics) == 1
    assert info_art.infographics[0] == ArtifactInfographic(
        title="Core Qubits",
        image_url="https://storage.googleapis.com/info1.png",
        width=2752,
        height=1536,
        alt_text="Diagram of entangled qubits",
        text="Superposition and Entanglement",
    )

    # 5. Test api.get() and get_or_none() populate identical fields without extra per-artifact fetch
    rpc_call.reset_mock()
    mock_mind_maps.list_mind_maps.reset_mock()
    rpc_call.return_value = [[audio_row]]

    got = await api.get("nb-1", "art-audio")
    assert got == audio
    assert rpc_call.await_count == 1
    # get() delegates to list() to discover studio + note-backed mind maps
    mock_mind_maps.list_mind_maps.assert_awaited_once_with("nb-1")

    rpc_call.reset_mock()
    mock_mind_maps.list_mind_maps.reset_mock()
    got_none = await api.get_or_none("nb-1", "art-audio")
    assert got_none == audio
    assert rpc_call.await_count == 1
    mock_mind_maps.list_mind_maps.assert_awaited_once_with("nb-1")


@pytest.mark.asyncio
async def test_note_backed_mind_map_populates_artifact_from_note_row() -> None:
    """Note-backed mind maps adapt into Artifacts with genuine type code 5."""
    mind_map_note = [
        "mm-note-1",
        [
            "mm-note-1",
            '{"nodes": []}',
            [1, "user-1", [1_700_000_000, 0]],
            None,
            "Mind Map Title",
        ],
    ]
    api, _, mock_mind_maps = _make_api(
        rpc_call=AsyncMock(return_value=[]),
        list_mind_maps_return=[mind_map_note],
    )

    artifacts = await api.list("nb-1")
    assert len(artifacts) == 1
    mm = artifacts[0]
    assert mm.id == "mm-note-1"
    assert mm.title == "Mind Map Title"
    assert mm.kind is ArtifactType.MIND_MAP
    assert mm._artifact_type == ArtifactTypeCode.MIND_MAP.value
    assert mm.is_interactive_mind_map is False
    assert mm.status == ArtifactStatus.COMPLETED.value
    assert mm.created_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert mm.url is None
    assert mm.generation_prompt is None
    assert mm.media_urls == ()


# ===========================================================================
# 3. Exact Family Classification Including Unknown Safe Summary
# ===========================================================================


@pytest.mark.parametrize(
    ("type_code", "variant", "expected_kind", "predicate_checks"),
    [
        (ArtifactTypeCode.AUDIO.value, None, ArtifactType.AUDIO, {}),
        (ArtifactTypeCode.REPORT.value, None, ArtifactType.REPORT, {"report_subtype": "report"}),
        (ArtifactTypeCode.VIDEO.value, None, ArtifactType.VIDEO, {}),
        (
            ArtifactTypeCode.QUIZ.value,
            QUIZ_VARIANT,
            ArtifactType.QUIZ,
            {"is_quiz": True, "is_flashcards": False, "is_interactive_mind_map": False},
        ),
        (
            ArtifactTypeCode.QUIZ.value,
            FLASHCARDS_VARIANT,
            ArtifactType.FLASHCARDS,
            {"is_quiz": False, "is_flashcards": True, "is_interactive_mind_map": False},
        ),
        (
            ArtifactTypeCode.QUIZ.value,
            INTERACTIVE_MIND_MAP_VARIANT,
            ArtifactType.MIND_MAP,
            {"is_interactive_mind_map": True, "is_quiz": False, "is_flashcards": False},
        ),
        (
            ArtifactTypeCode.QUIZ.value,
            None,
            ArtifactType.UNKNOWN,
            {"is_unclassified_type4": True, "is_interactive_mind_map": False},
        ),
        (
            ArtifactTypeCode.MIND_MAP.value,
            None,
            ArtifactType.MIND_MAP,
            {"is_interactive_mind_map": False},
        ),
        (ArtifactTypeCode.FANTASY_MAP.value, None, ArtifactType.FANTASY_MAP, {}),
        (ArtifactTypeCode.INFOGRAPHIC.value, None, ArtifactType.INFOGRAPHIC, {}),
        (ArtifactTypeCode.SLIDE_DECK.value, None, ArtifactType.SLIDE_DECK, {}),
        (ArtifactTypeCode.DATA_TABLE.value, None, ArtifactType.DATA_TABLE, {}),
        (ArtifactTypeCode.FILE.value, None, ArtifactType.FILE, {}),
    ],
)
def test_exact_family_classification_matrix(
    type_code: int,
    variant: int | None,
    expected_kind: ArtifactType,
    predicate_checks: dict[str, Any],
) -> None:
    """Characterize the exact classification mapping across all known families and variants."""
    _warned_artifact_types.clear()
    row = ["art-x", "Artifact Title", type_code, None, ArtifactStatus.COMPLETED.value]
    if variant is not None:
        row.extend([None] * 4)  # extend to index 9
        row.append([None, [variant]])

    artifact = Artifact.from_api_response(row)

    if expected_kind is ArtifactType.UNKNOWN:
        with pytest.warns(UnknownTypeWarning):
            assert artifact.kind is expected_kind
    else:
        assert artifact.kind is expected_kind

    assert artifact.kind == expected_kind.value  # str-enum comparison works
    for prop, expected_val in predicate_checks.items():
        assert getattr(artifact, prop) == expected_val, f"Mismatch on property {prop}"


def test_unknown_artifact_family_retains_safe_summary_without_guessing() -> None:
    """Unknown artifact type codes retain all row metadata without guessing a family."""
    _warned_artifact_types.clear()
    row: list[Any] = [None] * 22
    row[0] = "art-future-99"
    row[1] = "Future AI Creation"
    row[2] = 99  # Unmodelled future type code
    row[3] = [[["src-1"]]]
    row[4] = ArtifactStatus.COMPLETED.value
    row[15] = [1_700_000_000, 0]

    artifact = Artifact.from_api_response(row)

    with pytest.warns(UnknownTypeWarning, match=r"Unknown artifact type 99"):
        assert artifact.kind is ArtifactType.UNKNOWN

    assert artifact.kind == "unknown"
    assert artifact.id == "art-future-99"
    assert artifact.title == "Future AI Creation"
    assert artifact.status == ArtifactStatus.COMPLETED.value
    assert artifact.created_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)
    assert artifact.source_ids == ("src-1",)
    assert artifact._artifact_type == 99


# ===========================================================================
# 4. Lifecycle-Terminal GenerationStatus & wait_for_completion Behavior
# ===========================================================================


def test_generation_status_lifecycle_terminal_partition() -> None:
    """Partition the full set of GenerationState members into terminal vs non-terminal."""
    terminal_states = {
        GenerationState.COMPLETED,
        GenerationState.FAILED,
        GenerationState.REMOVED,
    }
    non_terminal_states = {
        GenerationState.PENDING,
        GenerationState.IN_PROGRESS,
        GenerationState.NOT_FOUND,
        GenerationState.UNKNOWN,
        GenerationState.SUGGESTED,
        GenerationState.PENDING_REVIEW,
    }
    assert {s for s in GenerationState if s.is_terminal} == terminal_states
    assert {s for s in GenerationState if not s.is_terminal} == non_terminal_states

    for s in terminal_states:
        assert GenerationStatus(task_id="t", status=s).is_terminal is True
    for s in non_terminal_states:
        assert GenerationStatus(task_id="t", status=s).is_terminal is False


@pytest.mark.asyncio
async def test_wait_for_completion_lifecycle_terminal_and_status_behavior() -> None:
    """wait_for_completion returns GenerationStatus and terminates only on lifecycle-terminal outcomes."""
    # 1. Immediate completion returns GenerationStatus
    api, _, _ = _make_api()
    api.poll_status = AsyncMock(  # type: ignore[method-assign]
        return_value=GenerationStatus(
            task_id="t1",
            status=GenerationState.COMPLETED,
            url="https://storage.googleapis.com/audio.mp4",
        )
    )
    res = await api.wait_for_completion("nb-1", "t1")
    assert isinstance(res, GenerationStatus)
    assert res.is_complete is True
    assert res.is_terminal is True
    assert res.url == "https://storage.googleapis.com/audio.mp4"

    # 2. Immediate failure returns GenerationStatus
    api.poll_status = AsyncMock(  # type: ignore[method-assign]
        return_value=GenerationStatus(
            task_id="t2", status=GenerationState.FAILED, error="Model generation failed"
        )
    )
    res = await api.wait_for_completion("nb-1", "t2")
    assert isinstance(res, GenerationStatus)
    assert res.is_failed is True
    assert res.is_terminal is True
    assert res.error == "Model generation failed"

    # 3. Transitions through non-terminal states keep polling until complete
    api.poll_status = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            GenerationStatus(task_id="t3", status=GenerationState.PENDING),
            GenerationStatus(task_id="t3", status=GenerationState.IN_PROGRESS),
            GenerationStatus(task_id="t3", status=GenerationState.COMPLETED),
        ]
    )
    res = await api.wait_for_completion("nb-1", "t3", initial_interval=0.001, max_interval=0.001)
    assert res.is_complete is True
    assert api.poll_status.await_count == 3

    # 4. Sustained NOT_FOUND escalates to REMOVED (is_removed=True, is_terminal=True)
    api.poll_status = AsyncMock(  # type: ignore[method-assign]
        return_value=GenerationStatus(task_id="t4", status=GenerationState.NOT_FOUND)
    )
    res = await api.wait_for_completion(
        "nb-1",
        "t4",
        initial_interval=0.001,
        max_interval=0.001,
        max_not_found=2,
        min_not_found_window=0.0,
    )
    assert res.is_removed is True
    assert res.is_terminal is True
    assert res.status is GenerationState.REMOVED


@pytest.mark.asyncio
async def test_poll_status_media_readiness_guards_premature_completion() -> None:
    """Media artifacts with wire status COMPLETED but empty URLs stay in_progress."""
    row_without_url = [
        "task-media",
        "Audio Title",
        ArtifactTypeCode.AUDIO.value,
        None,
        ArtifactStatus.COMPLETED.value,
        False,
        [None, None, None, None, None, [], [0, 0]],  # empty media urls
    ]
    api, _, _ = _make_api(rpc_call=AsyncMock(return_value=[[row_without_url]]))

    status = await api.poll_status("nb-1", "task-media")
    # Poll status remains in_progress until media URLs populate
    assert status.status is GenerationState.IN_PROGRESS
    assert status.is_complete is False


# ===========================================================================
# 5. GenerationState str/Enum Base Order and Terminal Frozenset Hashing
# ===========================================================================


def test_generation_state_mro_base_order_and_hash_invariants() -> None:
    """GenerationState must inherit str before Enum to ensure str.__hash__ is preserved.

    Reordering to class GenerationState(Enum, str) breaks _TERMINAL_GENERATION_STATES
    frozenset hash lookup by string value!
    """
    assert GenerationState.__mro__[1] is str
    assert GenerationState.__mro__[2] is Enum

    # Hash equals string value hash, NOT Enum member name hash
    assert hash(GenerationState.COMPLETED) == hash("completed")
    assert hash(GenerationState.COMPLETED) != hash("COMPLETED")

    # _TERMINAL_GENERATION_STATES frozenset contains exact terminal states
    assert (
        frozenset(
            {
                GenerationState.COMPLETED,
                GenerationState.FAILED,
                GenerationState.REMOVED,
            }
        )
        == _TERMINAL_GENERATION_STATES
    )

    # Hash membership lookup works with both enum instances and plain strings
    assert "completed" in _TERMINAL_GENERATION_STATES
    assert "failed" in _TERMINAL_GENERATION_STATES
    assert "removed" in _TERMINAL_GENERATION_STATES
    assert "pending" not in _TERMINAL_GENERATION_STATES
    assert "in_progress" not in _TERMINAL_GENERATION_STATES
    assert "not_found" not in _TERMINAL_GENERATION_STATES
    assert "unknown" not in _TERMINAL_GENERATION_STATES
    assert "suggested" not in _TERMINAL_GENERATION_STATES
    assert "pending_review" not in _TERMINAL_GENERATION_STATES

    # Raw string-constructed GenerationStatus uses the frozenset hash lookup cleanly
    assert GenerationStatus(task_id="t", status="completed").is_terminal is True
    assert GenerationStatus(task_id="t", status="failed").is_terminal is True
    assert GenerationStatus(task_id="t", status="removed").is_terminal is True
    assert GenerationStatus(task_id="t", status="pending").is_terminal is False
    assert GenerationStatus(task_id="t", status="future_state").is_terminal is False


# ===========================================================================
# 6. Download-Client Factory / Trusted-Host / Redirect Security Parity
# ===========================================================================


def test_trusted_download_host_allowlist_invariants() -> None:
    """Verify trusted-host allowlist and rejection of SSRF vectors."""
    # Valid Google hosts
    assert _is_trusted_download_host("storage.googleapis.com") is True
    assert _is_trusted_download_host("lh3.googleusercontent.com") is True
    assert _is_trusted_download_host("drive.google.com") is True
    assert _is_trusted_download_host("apis.google.com") is True

    # Untrusted hosts & attack vectors
    assert _is_trusted_download_host(None) is False
    assert _is_trusted_download_host("evil.com") is False
    assert _is_trusted_download_host("notgoogle.com") is False
    assert _is_trusted_download_host("storage.googleapis.com.evil.com") is False
    assert _is_trusted_download_host("evil%2egoogleapis.com") is False
    assert _is_trusted_download_host("storage.googleapis.com%2eevil.com") is False
    assert _is_trusted_download_host("storage.googleapis.com@evil.com") is False
    assert _is_trusted_download_host("storage.googleapis.com/path") is False
    assert _is_trusted_download_host("storage.googleapis.com\\path") is False


@pytest.mark.asyncio
async def test_download_client_factory_and_redirect_security_parity_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx download client enforces allowlist and HTTPS on initial request and redirects."""
    monkeypatch.delenv("NOTEBOOKLM_TRANSPORT", raising=False)
    client, do_get = _make_download_client(httpx.Cookies(), timeout=30.0)
    assert isinstance(client, httpx.AsyncClient)

    # 1. Initial non-HTTPS scheme is rejected by event hook
    with pytest.raises(ArtifactDownloadError, match="Untrusted redirect to non-HTTPS"):
        await do_get("http://storage.googleapis.com/audio.mp4")

    # 2. Initial untrusted domain is rejected by event hook
    with pytest.raises(ArtifactDownloadError, match="Untrusted download domain"):
        await do_get("https://evil.example.com/audio.mp4")

    await client.aclose()


@pytest.mark.asyncio
async def test_download_client_factory_and_redirect_security_parity_curl_cffi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """curl_cffi download client routes through get_guarded with trusted-host predicate."""
    pytest.importorskip("curl_cffi", reason="requires curl_cffi extra")
    from notebooklm._curl_cffi_transport import CurlCffiAsyncClient

    monkeypatch.setenv("NOTEBOOKLM_TRANSPORT", "curl_cffi")
    client, do_get = _make_download_client(httpx.Cookies(), timeout=30.0)
    assert isinstance(client, CurlCffiAsyncClient)

    # Non-HTTPS initial URL is blocked before connection
    with pytest.raises(httpx.RequestError):
        await do_get("http://storage.googleapis.com/audio.mp4")

    # Untrusted domain is blocked before connection
    with pytest.raises(httpx.RequestError):
        await do_get("https://evil.example.com/audio.mp4")

    await client.aclose()


# ===========================================================================
# 7. Drive Export & Uncommon Mind-Map / Table Paths
# ===========================================================================


@pytest.mark.asyncio
async def test_drive_export_operations_and_targets() -> None:
    """Drive export operations target Docs/Sheets with validated targets."""
    rpc_call = AsyncMock(return_value=None)
    api, _, _ = _make_api(rpc_call=rpc_call)

    # 1. export_report defaults to DOCS (code 1)
    await api.export_report("nb-1", "art-report", title="Quarterly Report")
    rpc_call.assert_awaited_once_with(
        RPCMethod.EXPORT_ARTIFACT,
        [None, "art-report", None, "Quarterly Report", 1],
        source_path="/notebook/nb-1",
        allow_null=True,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
        read_timeout=None,
        raise_on_null_status=False,
        _retry_deadline=None,
    )

    # 2. export_data_table targets SHEETS (code 2)
    rpc_call.reset_mock()
    await api.export_data_table("nb-1", "art-table", title="Quarterly Table")
    rpc_call.assert_awaited_once_with(
        RPCMethod.EXPORT_ARTIFACT,
        [None, "art-table", None, "Quarterly Table", 2],
        source_path="/notebook/nb-1",
        allow_null=True,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
        read_timeout=None,
        raise_on_null_status=False,
        _retry_deadline=None,
    )

    # 3. export() with artifact_id
    rpc_call.reset_mock()
    await api.export("nb-1", artifact_id="art-1", title="Export Title", export_type=ExportType.DOCS)
    rpc_call.assert_awaited_once_with(
        RPCMethod.EXPORT_ARTIFACT,
        [None, "art-1", None, "Export Title", 1],
        source_path="/notebook/nb-1",
        allow_null=True,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
        read_timeout=None,
        raise_on_null_status=False,
        _retry_deadline=None,
    )

    # 4. export() with raw content
    rpc_call.reset_mock()
    await api.export(
        "nb-1", content="# Markdown content", title="Custom Export", export_type=ExportType.DOCS
    )
    rpc_call.assert_awaited_once_with(
        RPCMethod.EXPORT_ARTIFACT,
        [None, None, "# Markdown content", "Custom Export", 1],
        source_path="/notebook/nb-1",
        allow_null=True,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
        read_timeout=None,
        raise_on_null_status=False,
        _retry_deadline=None,
    )

    # 5. export() validation: exactly one target required
    with pytest.raises(ValidationError, match=r"requires exactly one of artifact_id="):
        await api.export("nb-1", artifact_id="art-1", content="some content")

    with pytest.raises(ValidationError, match=r"requires exactly one of artifact_id="):
        await api.export("nb-1", artifact_id=None, content=None)


@pytest.mark.asyncio
async def test_mind_map_dual_backing_and_partial_availability() -> None:
    """Mind maps merge interactive and note-backed rows; partial outages degrade gracefully."""
    interactive_row = [
        "mm-studio",
        "Interactive Map",
        ArtifactTypeCode.QUIZ.value,
        None,
        ArtifactStatus.COMPLETED.value,
        False,
        None,
        None,
        None,
        [None, [INTERACTIVE_MIND_MAP_VARIANT, None, "Mind map prompt"]],
    ]
    note_backed_row = [
        "mm-note",
        [
            "mm-note",
            '{"nodes": []}',
            [1, "user-1", [1_700_000_000, 0]],
            None,
            "Note Backed Map",
        ],
    ]

    # 1. Successful merged listing
    api, _, _ = _make_api(
        rpc_call=AsyncMock(return_value=[[interactive_row]]),
        list_mind_maps_return=[note_backed_row],
    )
    artifacts = await api.list("nb-1", ArtifactType.MIND_MAP)
    assert len(artifacts) == 2
    assert {a.id for a in artifacts} == {"mm-studio", "mm-note"}

    # 2. ADR-0019 partial-availability: transient transport error in mind-map sub-fetch returns studio rows
    api, _, mock_mind_maps = _make_api(rpc_call=AsyncMock(return_value=[[interactive_row]]))
    mock_mind_maps.list_mind_maps = AsyncMock(side_effect=RPCError("temporary endpoint error"))
    degraded = await api.list("nb-1")
    assert len(degraded) == 1
    assert degraded[0].id == "mm-studio"

    # The legacy partial-availability boundary never swallowed the executor's
    # translated NetworkError; preserve that exact public behavior.
    mock_mind_maps.list_mind_maps = AsyncMock(side_effect=NetworkError("connection reset"))
    with pytest.raises(NetworkError, match="connection reset"):
        await api.list("nb-1")

    # 3. ADR-0019 schema drift in mind-map sub-fetch propagates DecodingError
    mock_mind_maps.list_mind_maps = AsyncMock(
        side_effect=DecodingError("corrupt mind map note shape")
    )
    with pytest.raises(DecodingError, match="corrupt mind map"):
        await api.list("nb-1")

    # 4. get_prompt returns generation prompt for studio mind maps, None for note-backed, raises on unknown
    api, _, mock_mind_maps = _make_api(
        rpc_call=AsyncMock(return_value=[[interactive_row]]),
        list_mind_maps_return=[note_backed_row],
    )
    prompt_studio = await api.get_prompt("nb-1", "mm-studio")
    assert prompt_studio == "Mind map prompt"

    prompt_note = await api.get_prompt("nb-1", "mm-note")
    assert prompt_note is None  # Note backed mind map has no prompt

    with pytest.raises(ArtifactNotFoundError):
        await api.get_prompt("nb-1", "nonexistent-id")


@pytest.mark.asyncio
async def test_data_table_listing_and_filtering() -> None:
    """Data tables (type code 9) are filtered cleanly by list_data_tables."""
    table_row = [
        "art-tbl",
        "Financial Table",
        ArtifactTypeCode.DATA_TABLE.value,
        None,
        ArtifactStatus.COMPLETED.value,
    ]
    audio_row = [
        "art-aud",
        "Financial Audio",
        ArtifactTypeCode.AUDIO.value,
        None,
        ArtifactStatus.COMPLETED.value,
    ]
    api, _, _ = _make_api(rpc_call=AsyncMock(return_value=[[table_row, audio_row]]))

    tables = await api.list_data_tables("nb-1")
    assert len(tables) == 1
    assert tables[0].id == "art-tbl"
    assert tables[0].kind is ArtifactType.DATA_TABLE
