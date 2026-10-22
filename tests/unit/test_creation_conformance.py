"""Shared behavioral creation matrix through the real Web and Android facades."""

from __future__ import annotations

import warnings
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifact import creation_normalized as n
from notebooklm._types.enums import (
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
)
from notebooklm._web.artifacts import WebArtifactsAPI
from notebooklm._web.mind_maps import NoteBackedMindMapService
from notebooklm._web.notes import NoteService
from notebooklm.exceptions import ArtifactNotReadyError, ValidationError
from notebooklm.types import GenerationStatus, MindMapKind, MindMapResult
from tests._fixtures.creation_conformance import (
    CREATE_ARTIFACT_METHOD,
    android_artifacts_graph,
    android_mind_maps_graph,
    web_mind_maps_graph,
)
from tests._fixtures.creation_conformance import (
    PROTO as _PROTO,
)
from tests._fixtures.creation_conformance import (
    artifact_proto as _artifact,
)
from tests._fixtures.creation_conformance import (
    interactive_artifact as _interactive_artifact,
)
from tests._fixtures.fake_core import make_fake_core


@pytest.fixture(params=["web", "android"])
def backend(request):
    return request.param


@pytest.fixture
def creation_api(backend, monkeypatch):
    if backend == "web":
        terminal = make_fake_core(
            rpc_call=AsyncMock(return_value=[["created", None, None, None, 1]])
        )
        notebooks = MagicMock(get_source_ids=AsyncMock(return_value=["resolved"]))
        api = WebArtifactsAPI(
            rpc=terminal,
            supervisor=terminal,
            notebooks=notebooks,
            mind_maps=MagicMock(spec=NoteBackedMindMapService),
            note_service=MagicMock(spec=NoteService),
        )
    else:
        terminal, notebooks, _, _, api = android_artifacts_graph()
        notebooks.source_ids = ["resolved"]
    observed = []
    send = api._send_create_artifact

    async def observe(value):
        observed.append(value)
        if backend == "android":
            type_code, variant = {
                n.NormalizedAudio: (1, None),
                n.NormalizedVideo: (3, None),
                n.NormalizedCinematicVideo: (3, None),
                n.NormalizedReport: (2, None),
                n.NormalizedQuiz: (4, 2),
                n.NormalizedFlashcards: (4, 1),
                n.NormalizedInfographic: (7, None),
                n.NormalizedSlideDeck: (8, None),
                n.NormalizedDataTable: (9, None),
                n.NormalizedInteractiveMindMap: (4, 4),
            }[type(value)]
            terminal.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
                artifact=_artifact(
                    "created",
                    type_code=type_code,
                    variant=variant,
                    status=_PROTO.ARTIFACT_STATUS_INITIALIZED,
                )
            )
        return await send(value)

    monkeypatch.setattr(api, "_send_create_artifact", observe)
    return api, observed, terminal, notebooks


CASES = [
    (
        "audio",
        {},
        n.NormalizedAudio,
        {"format_code": AudioFormat.DEEP_DIVE.value, "length_code": AudioLength.DEFAULT.value},
    ),
    (
        "video",
        {},
        n.NormalizedVideo,
        {"format_code": VideoFormat.EXPLAINER.value, "style_code": VideoStyle.AUTO_SELECT.value},
    ),
    ("cinematic_video", {}, n.NormalizedCinematicVideo, {"language": "en"}),
    ("report", {}, n.NormalizedReport, {"title": "Briefing Doc"}),
    (
        "quiz",
        {},
        n.NormalizedQuiz,
        {
            "quantity_code": QuizQuantity.STANDARD.value,
            "difficulty_code": QuizDifficulty.MEDIUM.value,
        },
    ),
    (
        "flashcards",
        {},
        n.NormalizedFlashcards,
        {
            "quantity_code": QuizQuantity.STANDARD.value,
            "difficulty_code": QuizDifficulty.MEDIUM.value,
        },
    ),
    (
        "infographic",
        {},
        n.NormalizedInfographic,
        {
            "orientation_code": InfographicOrientation.LANDSCAPE.value,
            "detail_code": InfographicDetail.STANDARD.value,
            "style_code": InfographicStyle.AUTO_SELECT.value,
        },
    ),
    (
        "slide_deck",
        {},
        n.NormalizedSlideDeck,
        {
            "format_code": SlideDeckFormat.DETAILED_DECK.value,
            "length_code": SlideDeckLength.DEFAULT.value,
        },
    ),
    ("data_table", {}, n.NormalizedDataTable, {"language": "en"}),
    (
        "audio",
        {"audio_format": AudioFormat.DEBATE, "audio_length": AudioLength.SHORT},
        n.NormalizedAudio,
        {"format_code": AudioFormat.DEBATE.value, "length_code": AudioLength.SHORT.value},
    ),
    (
        "video",
        {"video_style": VideoStyle.CUSTOM, "style_prompt": "  paper cutout  "},
        n.NormalizedVideo,
        {"style_prompt": "paper cutout", "style_code": VideoStyle.CUSTOM.value},
    ),
    (
        "report",
        {
            "report_format": ReportFormat.CUSTOM,
            "custom_prompt": "My report",
            "extra_instructions": "ignored",
        },
        n.NormalizedReport,
        {"directive": "My report"},
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("family,options,expected_type,expected", CASES)
async def test_normalized_values_reach_both_real_protocol_hooks(
    creation_api, family, options, expected_type, expected
):
    api, observed, terminal, _ = creation_api
    result = await getattr(api, f"generate_{family}")("nb", source_ids=["s"], **options)
    assert result.task_id == "created" and result.status == "pending"
    [value] = observed
    assert type(value) is expected_type
    assert value.notebook_id == "nb" and value.source_ids == ("s",)
    for field, wanted in expected.items():
        assert getattr(value, field) == wanted
    with pytest.raises(FrozenInstanceError):
        value.source_ids = ()
    # The actual encoder and terminal were exercised after the hook observation.
    assert terminal.calls if hasattr(terminal, "calls") else terminal.rpc_call.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("family", [row[0] for row in CASES[:9]])
@pytest.mark.parametrize("sources", [None, []])
async def test_source_omission_and_empty_are_backend_policy(backend, creation_api, family, sources):
    api, observed, terminal, notebooks = creation_api
    method = getattr(api, f"generate_{family}")
    if backend == "android" and sources == []:
        with pytest.raises(ValidationError, match="at least one source id"):
            await method("nb", source_ids=sources)
        assert observed == [] and terminal.calls == [] and notebooks.calls == []
    else:
        await method("nb", source_ids=sources)
        assert observed[0].source_ids == (("resolved",) if sources is None else ())
        if backend == "android":
            assert notebooks.calls == ["nb"]
        elif sources is None:
            notebooks.get_source_ids.assert_awaited_once_with("nb")
        else:
            notebooks.get_source_ids.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "family,options",
    [
        ("audio", {"language": ""}),
        ("data_table", {"instructions": 123}),
        ("audio", {"audio_format": AudioLength.SHORT}),
    ],
)
async def test_legacy_web_accepted_inputs_do_not_acquire_android_rejections(
    backend, creation_api, family, options
):
    api, observed, terminal, _ = creation_api
    if backend == "android":
        with pytest.raises(ValidationError):
            await getattr(api, f"generate_{family}")("nb", source_ids=["s"], **options)
        assert observed == [] and terminal.calls == []
    else:
        await getattr(api, f"generate_{family}")("nb", source_ids=["s"], **options)
        assert len(observed) == 1 and terminal.rpc_call.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"video_format": VideoFormat.SHORT, "video_style": VideoStyle.WHITEBOARD},
        {"video_style": VideoStyle.CUSTOM},
        {"style_prompt": "requires custom"},
    ],
)
async def test_shared_unsupported_video_combinations_reject_before_terminal(creation_api, options):
    api, observed, _, _ = creation_api
    with pytest.raises(ValidationError):
        await api.generate_video("nb", ["s"], **options)
    assert observed == []


@pytest.mark.asyncio
async def test_android_concept_report_support_is_not_fabricated_on_web(backend, creation_api):
    api, observed, _, _ = creation_api
    if backend == "web":
        with pytest.raises(ValueError, match="Unsupported report format"):
            await api.generate_report("nb", ReportFormat.CONCEPT_EXPLANATION, ["s"])
        assert observed == []
    else:
        await api.generate_report("nb", ReportFormat.CONCEPT_EXPLANATION, ["s"])
        assert observed[0].title == "Concept Explanation"


def test_backend_capabilities_describe_interactive_implementation(backend, creation_api):
    api, _, _, _ = creation_api
    capabilities = api.creation_capabilities
    assert len({cap.family for cap in capabilities}) == len(capabilities) == 10
    interactive = next(cap for cap in capabilities if cap.family == "interactive_mind_map")
    assert interactive.supported_options == (
        ("instructions",) if backend == "web" else ("language", "instructions")
    )
    assert bool(interactive.limitations) == (backend == "web")
    report = next(cap for cap in capabilities if cap.family == "report")
    assert bool(report.limitations) == (backend == "web")
    with pytest.raises(FrozenInstanceError):
        interactive.family = "invented"


@pytest.fixture
def mind_maps(backend):
    if backend == "android":
        api, artifacts, _ = android_mind_maps_graph(artifacts=[_interactive_artifact("created")])
        artifacts._generate_interactive_mind_map.return_value = GenerationStatus(
            task_id="created", status="pending"
        )
        tree = artifacts._get_interactive_mind_map_tree
    else:
        api, rpc, _, artifacts, _ = web_mind_maps_graph(
            interactive=[_interactive_artifact("created")]
        )
        rpc.configure_mock(rpc_call=AsyncMock(side_effect=[[["created", "T", 4]], None]))
        tree = rpc.rpc_call
    return api, artifacts, tree


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "failed", "removed", "completed"])
@pytest.mark.parametrize("policy", ["legacy", "raise"])
async def test_waited_interactive_outcome_matrix(backend, mind_maps, state, policy):
    api, artifacts, tree = mind_maps
    artifacts.wait_for_completion.return_value = GenerationStatus(task_id="created", status=state)
    if state == "pending":
        # A real waiter times out if pending never progresses; it must propagate intact.
        artifacts.wait_for_completion.side_effect = TimeoutError("still pending")
    rejected = state in {"failed", "removed"} and (backend == "android" or policy == "raise")
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        if state == "pending" or rejected:
            with pytest.raises(TimeoutError if state == "pending" else ArtifactNotReadyError):
                await api.generate("nb", ["s"], kind=MindMapKind.INTERACTIVE, failure_policy=policy)
            assert tree.await_count == (1 if backend == "web" else 0)
        else:
            result = await api.generate(
                "nb", ["s"], kind=MindMapKind.INTERACTIVE, failure_policy=policy
            )
            assert result.id == "created"
            assert tree.await_count == (2 if backend == "web" else 1)
    expected_warning = backend == "web" and policy == "legacy" and state in {"failed", "removed"}
    assert len(emitted) == int(expected_warning)
    if emitted:
        assert emitted[0].category is DeprecationWarning
        assert "failure_policy='raise'" in str(emitted[0].message)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [MindMapKind.INTERACTIVE, MindMapKind.NOTE_BACKED])
@pytest.mark.parametrize("policy", ["legacy", "raise"])
async def test_nonwaited_and_synchronous_mind_maps_keep_their_contract(
    backend, mind_maps, kind, policy
):
    api, artifacts, tree = mind_maps
    artifacts.generate_mind_map.return_value = MindMapResult(
        mind_map={"name": "Root"}, note_id="note"
    )
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        result = await api.generate("nb", ["s"], kind=kind, wait=False, failure_policy=policy)
    assert not emitted
    artifacts.wait_for_completion.assert_not_awaited()
    if kind == MindMapKind.NOTE_BACKED:
        assert result.id == "note" and result.tree == {"name": "Root"}
        assert tree.await_count == 0
    else:
        assert result.id == "created" and result.tree is None
        assert tree.await_count == (1 if backend == "web" else 0)


@pytest.mark.asyncio
async def test_first_party_waited_generation_raises_on_actual_failed_backend(mind_maps):
    from notebooklm._app.generate import execute_generation
    from notebooklm._app.generation_requests import MindMapGenerationRequest

    api, artifacts, tree = mind_maps
    artifacts.wait_for_completion.return_value = GenerationStatus(
        task_id="created", status="failed"
    )
    client = MagicMock(mind_maps=api, artifacts=artifacts)
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always")
        with pytest.raises(ArtifactNotReadyError):
            await execute_generation(
                MindMapGenerationRequest(notebook_id="nb", map_kind=MindMapKind.INTERACTIVE),
                client,
                notebook_resolver=AsyncMock(return_value="nb"),
                source_resolver=AsyncMock(return_value=["s"]),
            )
    assert emitted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("instructions", ["   ", "  preserve prompt  "])
async def test_interactive_domain_normalization_and_language_encoding(
    backend, creation_api, monkeypatch, instructions
):
    from notebooklm._web.mind_maps import WebMindMapsAPI
    from notebooklm._web.params import creation as web_encoder

    api, observed, terminal, notebooks = creation_api
    if backend == "android":
        await api._generate_interactive_mind_map(
            "nb", ["s"], language="fr", instructions=instructions
        )
        [request] = observed
        assert isinstance(request, n.NormalizedInteractiveMindMap)
        assert request.language == "fr" and request.instructions == instructions
        proto = terminal.calls[0][1].artifact.app.generation_options
        assert proto.language_code == "fr" and proto.free_text_steering_prompt == instructions
    else:
        encoded = []
        encode = web_encoder.encode_creation

        def observe(request):
            encoded.append(request)
            return encode(request)

        monkeypatch.setattr(web_encoder, "encode_creation", observe)
        mind_maps = WebMindMapsAPI(
            rpc=terminal,
            supervisor=terminal,
            artifacts=MagicMock(list=AsyncMock(return_value=[])),
            notebooks=notebooks,
            notes=MagicMock(),
            mind_maps=MagicMock(),
        )
        await mind_maps.generate(
            "nb",
            ["s"],
            language="fr",
            instructions=instructions,
            kind=MindMapKind.INTERACTIVE,
            wait=False,
        )
        [request] = encoded
        assert isinstance(request, n.NormalizedInteractiveMindMap)
        assert request.instructions == (instructions if instructions.strip() else None)
        params = terminal.rpc_call.call_args.args[1]
        assert "fr" not in repr(params)
        assert params[2][9][1] == ([4, None, instructions] if instructions.strip() else [4])


@pytest.mark.asyncio
@pytest.mark.parametrize("later", [False, True])
async def test_audio_deferred_generation_wire(backend, creation_api, later):
    """Both backends preserve the field-7 scheduling message and ordinary defaults."""
    api, observed, terminal, _ = creation_api
    await api.generate_audio("nb", source_ids=["source"], later=later)
    assert observed[0].later is later
    if backend == "web":
        params = terminal.rpc_call.await_args.args[1]
        assert len(params) == (7 if later else 3)
        if later:
            assert params[3:] == [None, None, None, [True]]
    else:
        from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import artifacts_pb2

        projection = artifacts_pb2.WireCreateArtifactDeferredProjection.FromString(
            terminal.calls[0][1].SerializeToString()
        )
        assert projection.HasField("deferred_generation") is later
        assert projection.deferred_generation.enabled is later
        # This independently observed suffix is shared with Web field 7.
        if later:
            assert b"\x3a\x02\x08\x01" in terminal.calls[0][1].SerializeToString()


@pytest.mark.asyncio
@pytest.mark.parametrize("later", [1, "true", None])
async def test_audio_later_requires_bool_before_source_lookup(creation_api, later):
    api, observed, _, notebooks = creation_api
    with pytest.raises(ValidationError, match="later must be a bool"):
        await api.generate_audio("nb", later=later)
    assert observed == []
    if hasattr(notebooks, "calls") and isinstance(notebooks.calls, list):
        assert notebooks.calls == []
    else:
        notebooks.get_source_ids.assert_not_awaited()
