"""Offline contract tests for the evidence-qualified B4 artifact adapter."""

from __future__ import annotations

import asyncio
import inspect
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import httpx
import pytest
from google.protobuf import empty_pb2
from tests._helpers.android_supervisor import SupervisedAndroidTransport

from notebooklm._android.artifacts import (
    CREATE_ARTIFACT_METHOD,
    DELETE_ARTIFACT_METHOD,
    GENERATE_REPORT_SUGGESTIONS_METHOD,
    GET_ARTIFACT_METHOD,
    LIST_ARTIFACTS_METHOD,
    UPDATE_ARTIFACT_METHOD,
    AndroidArtifactsAPI,
)
from notebooklm._android.assets import AndroidAssetDownloadService
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    artifacts_pb2,
    read_pb2,
)
from notebooklm._android.session import AndroidSession
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._client_metrics import ClientMetrics
from notebooklm._notebook_metadata import NotebookSourceIdProvider
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm._types.common import UnknownTypeWarning
from notebooklm._types.enums import (
    ArtifactTypeCode,
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
from notebooklm.exceptions import (
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    DecodingError,
    RPCError,
    UnsupportedOperationError,
    ValidationError,
)
from notebooklm.types import Artifact, ArtifactType

_PROTO = artifacts_pb2


@dataclass(frozen=True)
class _Lease:
    epoch: int = 7


class FakeSession:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.errors: dict[str, BaseException] = {}
        self.scopes: list[str] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        self.scopes.append(label)
        yield _Lease()

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.calls.append((method, request, kwargs))
        error = self.errors.get(method)
        if error is not None:
            raise error
        response = self.responses[method]
        if isinstance(response, list):
            return response.pop(0)
        return response


class FakeNotebooks:
    def __init__(self, source_ids: list[str] | None = None) -> None:
        self.source_ids = source_ids or ["source-1", "source-2"]
        self.calls: list[str] = []

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        self.calls.append(notebook_id)
        return list(self.source_ids)


class FakeMindMaps:
    def __init__(self, artifacts: list[Artifact] | None = None) -> None:
        self.artifacts = artifacts or []
        self.calls: list[str] = []
        self.error: BaseException | None = None

    async def list_mind_map_artifacts(self, notebook_id: str) -> list[Artifact]:
        self.calls.append(notebook_id)
        if self.error is not None:
            raise self.error
        return list(self.artifacts)


class FakeAssets:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.error: ArtifactDownloadError | None = None

    async def download_url(self, url: str, output_path: str) -> str:
        self.calls.append((url, output_path))
        if self.error is not None:
            raise self.error
        return output_path

    async def download_urls_batch(self, urls_and_paths: list[tuple[str, str]]) -> Any:
        raise AssertionError(f"batch transfer not expected: {urls_and_paths!r}")


def _supervisor() -> CallSupervisor:
    return CallSupervisor(
        metrics=ClientMetrics(),
        drain_tracker=TransportDrainTracker(),
        max_concurrent_rpcs=2,
    )


async def _activate(supervisor: CallSupervisor, epoch: int = 1) -> None:
    loop = __import__("asyncio").get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(epoch)
    supervisor.start_accepting(epoch)


def _artifact(
    artifact_id: str,
    *,
    title: str = "Artifact",
    type_code: int = _PROTO.ARTIFACT_TYPE_INFOGRAPHIC,
    status: int = _PROTO.ARTIFACT_STATUS_READY,
    variant: int = 0,
    etag: str = "etag-1",
    url: str | None = None,
) -> Any:
    message = _PROTO.Artifact(
        artifact_id=artifact_id,
        title=title,
        type=type_code,
        status=status,
        etag=etag,
    )
    if type_code == _PROTO.ARTIFACT_TYPE_APP:
        message.app.generation_options.app_type = variant
    if type_code == _PROTO.ARTIFACT_TYPE_INFOGRAPHIC and url is not None:
        message.infographic.infographics.add(title=title).image.url = url
    return message


def _mind_map(artifact_id: str = "note-map") -> Artifact:
    return Artifact(
        id=artifact_id,
        title="Note map",
        _artifact_type=ArtifactTypeCode.MIND_MAP.value,
        status=_PROTO.ARTIFACT_STATUS_READY,
    )


def _graph(
    studio: list[Any] | None = None,
) -> tuple[FakeSession, FakeNotebooks, FakeMindMaps, FakeAssets, AndroidArtifactsAPI]:
    studio_rows = studio or []
    get_response = _PROTO.GetArtifactResponse()
    if studio_rows:
        get_response.artifact.CopyFrom(studio_rows[-1])
    session = FakeSession(
        {
            LIST_ARTIFACTS_METHOD: _PROTO.ListArtifactsResponse(artifacts=studio_rows),
            GET_ARTIFACT_METHOD: get_response,
            DELETE_ARTIFACT_METHOD: empty_pb2.Empty(),
            GENERATE_REPORT_SUGGESTIONS_METHOD: _PROTO.GenerateReportSuggestionsResponse(),
        }
    )
    notebooks = FakeNotebooks()
    mind_maps = FakeMindMaps()
    assets = FakeAssets()
    api = AndroidArtifactsAPI(
        session=cast(AndroidSession, session),
        supervisor=_supervisor(),
        notebooks=cast(NotebookSourceIdProvider, notebooks),
        mind_maps=mind_maps,
        asset_downloads=cast(AndroidAssetDownloadService, assets),
    )
    return session, notebooks, mind_maps, assets, api


def _supervised_graph(
    transport: SupervisedAndroidTransport,
    *,
    notebooks: Any | None = None,
    mind_maps: Any | None = None,
    assets: Any | None = None,
) -> AndroidArtifactsAPI:
    return AndroidArtifactsAPI(
        session=cast(AndroidSession, transport),
        supervisor=transport.supervisor,
        notebooks=cast(NotebookSourceIdProvider, notebooks or FakeNotebooks()),
        mind_maps=cast(Any, mind_maps or FakeMindMaps()),
        asset_downloads=cast(AndroidAssetDownloadService, assets or FakeAssets()),
    )


def test_adapter_is_concrete_and_requires_the_narrow_note_lister() -> None:
    assert AndroidArtifactsAPI.__abstractmethods__ == frozenset()
    assert ArtifactsAPI.__abstractmethods__
    assert inspect.signature(AndroidArtifactsAPI).parameters["mind_maps"].default is (
        inspect.Parameter.empty
    )
    session, notebooks, _, assets, _ = _graph()
    with pytest.raises(TypeError, match="mind_maps"):
        AndroidArtifactsAPI(
            session=cast(AndroidSession, session),
            supervisor=_supervisor(),
            notebooks=cast(NotebookSourceIdProvider, notebooks),
            mind_maps=cast(Any, None),
            asset_downloads=cast(AndroidAssetDownloadService, assets),
        )


def test_adapter_retains_transport_without_deleted_session_attribute() -> None:
    session, _, _, _, api = _graph()
    assert api._transport is session
    assert not hasattr(api, "_session")


@pytest.mark.asyncio
async def test_list_merges_studio_then_notes_and_filters_suggested() -> None:
    session, _, mind_maps, _, api = _graph(
        [
            _artifact("studio-1", type_code=_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW),
            _artifact("suggested", status=_PROTO.ARTIFACT_STATUS_SUGGESTED),
            _artifact("interactive", type_code=_PROTO.ARTIFACT_TYPE_APP, variant=4),
        ]
    )
    mind_maps.artifacts = [_mind_map()]

    listed = await api.list("notebook-1")

    assert [artifact.id for artifact in listed] == ["studio-1", "interactive", "note-map"]
    assert mind_maps.calls == ["notebook-1"]
    method, request, kwargs = session.calls[0]
    assert method == LIST_ARTIFACTS_METHOD
    assert request == _PROTO.ListArtifactsRequest(project_id="notebook-1")
    assert kwargs == {
        "replay_safe": True,
        "response_type": _PROTO.ListArtifactsResponse,
        "expected_epoch": 7,
    }


@pytest.mark.asyncio
async def test_non_mind_map_filter_skips_note_io_and_mind_map_filter_merges_both() -> None:
    _, _, mind_maps, _, api = _graph(
        [
            _artifact("quiz", type_code=_PROTO.ARTIFACT_TYPE_APP, variant=2),
            _artifact("interactive", type_code=_PROTO.ARTIFACT_TYPE_APP, variant=4),
        ]
    )
    mind_maps.artifacts = [_mind_map()]

    assert [item.id for item in await api.list("notebook-1", ArtifactType.QUIZ)] == ["quiz"]
    assert mind_maps.calls == []
    assert [item.id for item in await api.list("notebook-1", ArtifactType.MIND_MAP)] == [
        "interactive",
        "note-map",
    ]
    assert mind_maps.calls == ["notebook-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(RPCError("temporary", method_id="notes"), id="rpc"),
        pytest.param(httpx.ConnectError("temporary"), id="httpx"),
    ],
)
async def test_transient_note_failure_returns_partial_studio_and_unknown_sentinel(
    error: BaseException,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, _, mind_maps, _, api = _graph([_artifact("studio")])
    mind_maps.error = error

    with caplog.at_level("WARNING"):
        aggregate, note_state = await api._list_with_note_state("notebook-1", None)

    assert [item.id for item in aggregate] == ["studio"]
    assert note_state is None
    assert type(error).__name__ in caplog.text
    assert str(error) not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(DecodingError("malformed note"), id="decoding"),
        pytest.param(ValueError("programming error"), id="other"),
    ],
)
async def test_non_transient_note_failure_propagates_identity(error: BaseException) -> None:
    _, _, mind_maps, _, api = _graph([_artifact("studio")])
    mind_maps.error = error
    with pytest.raises(type(error)) as raised:
        await api.list("notebook-1")
    assert raised.value is error


@pytest.mark.asyncio
async def test_public_get_stays_aggregate_while_poll_uses_exact_get_artifact() -> None:
    session, _, mind_maps, _, api = _graph(
        [_artifact("quiz", type_code=_PROTO.ARTIFACT_TYPE_APP, variant=2)]
    )
    await _activate(api._supervisor)

    assert (await api.get("notebook-1", "quiz")).id == "quiz"
    assert await api.get_prompt("notebook-1", "quiz") is None
    status = await api.poll_status("notebook-1", "quiz")

    assert status.task_id == "quiz"
    assert status.is_complete
    assert mind_maps.calls == ["notebook-1", "notebook-1"]
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        LIST_ARTIFACTS_METHOD,
        GET_ARTIFACT_METHOD,
    ]


@pytest.mark.asyncio
async def test_wait_ready_tick_is_one_get_artifact_and_zero_note_reads() -> None:
    session, _, mind_maps, _, api = _graph(
        [_artifact("ready", url="https://lh3.googleusercontent.com/ready.png")]
    )
    await _activate(api._supervisor)

    result = await api.wait_for_completion(
        "notebook-1",
        "ready",
        initial_interval=0,
        max_interval=0,
        timeout=1,
    )

    assert result.is_complete
    assert [call[0] for call in session.calls] == [GET_ARTIFACT_METHOD]
    assert mind_maps.calls == []


@pytest.mark.asyncio
async def test_poll_get_artifact_does_not_decode_unrelated_list_rows() -> None:
    malformed = _artifact("", title="unrelated malformed")
    target = _artifact(
        "target",
        url="https://lh3.googleusercontent.com/target.png",
    )
    session, _, mind_maps, _, api = _graph([malformed, target])
    await _activate(api._supervisor)

    result = await api.poll_status("notebook-1", "target")

    assert result.is_complete
    assert [call[0] for call in session.calls] == [GET_ARTIFACT_METHOD]
    assert mind_maps.calls == []


@pytest.mark.asyncio
async def test_poll_rejects_wrong_get_artifact_identity() -> None:
    session, _, mind_maps, _, api = _graph()
    session.responses[GET_ARTIFACT_METHOD] = _PROTO.GetArtifactResponse(
        artifact=_artifact("other", type_code=_PROTO.ARTIFACT_TYPE_APP, variant=2)
    )
    await _activate(api._supervisor)

    with pytest.raises(DecodingError, match="different artifact id") as raised:
        await api.poll_status("notebook-1", "target")

    assert raised.value.method_id == GET_ARTIFACT_METHOD
    assert [call[0] for call in session.calls] == [GET_ARTIFACT_METHOD]
    assert mind_maps.calls == []


@pytest.mark.asyncio
async def test_private_get_artifact_helper_uses_exact_request_and_epoch() -> None:
    session, _, _, _, api = _graph([_artifact("target")])

    result = await api._get_studio_artifact("target", expected_epoch=42)

    assert result is not None and result.id == "target"
    method, request, kwargs = session.calls[0]
    assert method == GET_ARTIFACT_METHOD
    assert request == _PROTO.GetArtifactRequest(artifact_id="target")
    assert kwargs == {
        "replay_safe": True,
        "response_type": _PROTO.GetArtifactResponse,
        "expected_epoch": 42,
    }


@pytest.mark.asyncio
async def test_get_artifact_missing_payload_is_bounded_decode_error() -> None:
    session, _, _, _, api = _graph()
    session.responses[GET_ARTIFACT_METHOD] = _PROTO.GetArtifactResponse()

    with pytest.raises(DecodingError, match="omitted its artifact") as raised:
        await api._get_studio_artifact("target")

    assert raised.value.method_id == GET_ARTIFACT_METHOD
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_get_artifact_identity_failure_drops_capability_response_from_frames() -> None:
    secret = "https://lh3.googleusercontent.com/image.png?cap=get-secret"
    raw_response = _PROTO.GetArtifactResponse(artifact=_artifact("other", url=secret))
    session, _, _, _, api = _graph()
    session.responses[GET_ARTIFACT_METHOD] = raw_response

    with pytest.raises(DecodingError) as raised:
        await api._get_studio_artifact("target")

    error = raised.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in str(error)
    for frame, _line in traceback.walk_tb(error.__traceback__):
        if "/src/notebooklm/" not in frame.f_code.co_filename:
            continue
        assert secret not in repr(frame.f_locals)
        assert raw_response not in frame.f_locals.values()


@pytest.mark.asyncio
async def test_poll_maps_get_artifact_not_found_to_not_found_status() -> None:
    session, _, mind_maps, _, api = _graph()
    session.errors[GET_ARTIFACT_METHOD] = RPCError("missing", rpc_code=5)
    await _activate(api._supervisor)

    status = await api.poll_status("notebook-1", "missing")

    assert status.is_not_found
    assert [call[0] for call in session.calls] == [GET_ARTIFACT_METHOD]
    assert mind_maps.calls == []


@pytest.mark.asyncio
async def test_get_prompt_missing_raises_bounded_artifact_error() -> None:
    _, _, _, _, api = _graph()
    with pytest.raises(ArtifactNotFoundError) as raised:
        await api.get_prompt("notebook-1", "missing")
    assert raised.value.artifact_id == "missing"
    assert raised.value.method_id == LIST_ARTIFACTS_METHOD


@pytest.mark.asyncio
async def test_generate_quiz_uses_exact_request_and_never_replays_mutation() -> None:
    session, notebooks, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact(
            "quiz-1",
            type_code=_PROTO.ARTIFACT_TYPE_APP,
            status=_PROTO.ARTIFACT_STATUS_PROCESSING,
            variant=2,
        )
    )

    status = await api.generate_quiz(
        "notebook-1",
        instructions="Focus on evidence",
        quantity=QuizQuantity.MORE,
        difficulty=QuizDifficulty.HARD,
    )

    assert status.task_id == "quiz-1"
    assert status.is_in_progress
    assert notebooks.calls == ["notebook-1"]
    method, request, kwargs = session.calls[0]
    assert method == CREATE_ARTIFACT_METHOD
    assert kwargs == {
        "replay_safe": False,
        "response_type": _PROTO.CreateArtifactResponse,
        "expected_epoch": 7,
    }
    assert request.project_id == "notebook-1"
    assert request.artifact.type == _PROTO.ARTIFACT_TYPE_APP
    assert [source.source_id.id for source in request.artifact.sources] == [
        "source-1",
        "source-2",
    ]
    options = request.artifact.app.generation_options
    assert options.app_type == _PROTO.APP_TYPE_QUIZ
    assert options.free_text_steering_prompt == "Focus on evidence"
    assert options.quiz_generation_options.question_quantity == 3
    assert options.quiz_generation_options.quiz_difficulty == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source_ids": []}, "at least one source"),
        ({"source_ids": ["s"], "quantity": 2}, "QuizQuantity"),
        ({"source_ids": ["s"], "difficulty": "hard"}, "QuizDifficulty"),
        ({"source_ids": ["s"], "instructions": object()}, "instructions"),
    ],
)
async def test_generate_quiz_validation_is_pre_transport(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    session, _, _, _, api = _graph()
    with pytest.raises(ValidationError, match=message):
        await api.generate_quiz("notebook-1", **kwargs)
    assert session.calls == []


@pytest.mark.asyncio
async def test_generate_quiz_rejects_mismatched_response_family() -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("wrong", type_code=_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW)
    )
    with pytest.raises(DecodingError, match="different artifact family"):
        await api.generate_quiz("notebook-1", source_ids=["source-1"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audio_length", "expected_length"),
    [
        (None, _PROTO.EPISODE_LENGTH_MEDIUM),
        (AudioLength.SHORT, _PROTO.EPISODE_LENGTH_SHORT),
        (AudioLength.LONG, _PROTO.EPISODE_LENGTH_LONG),
    ],
)
async def test_generate_audio_uses_exact_duplicated_source_wire(
    audio_length: AudioLength | None,
    expected_length: int,
) -> None:
    session, notebooks, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact(
            "audio-1",
            type_code=_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW,
            status=_PROTO.ARTIFACT_STATUS_PROCESSING,
        )
    )

    status = await api.generate_audio(
        "notebook-1",
        source_ids=["source-1", "source-2"],
        language="fr",
        instructions="Focus on the evidence",
        audio_format=AudioFormat.DEEP_DIVE,
        audio_length=audio_length,
    )

    assert status.task_id == "audio-1"
    assert status.is_in_progress
    assert notebooks.calls == []
    method, request, kwargs = session.calls[0]
    assert method == CREATE_ARTIFACT_METHOD
    assert kwargs == {
        "replay_safe": False,
        "response_type": _PROTO.CreateArtifactResponse,
        "expected_epoch": 7,
    }
    assert request.project_id == "notebook-1"
    assert request.artifact.type == _PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW
    assert [source.source_id.id for source in request.artifact.sources] == [
        "source-1",
        "source-2",
    ]
    options = request.artifact.audio_overview.generation_options
    assert [source.id for source in options.source_ids] == ["source-1", "source-2"]
    assert options.episode_focus == "Focus on the evidence"
    assert options.episode_length == expected_length
    assert options.language_code == "fr"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "audio_format",
    [AudioFormat.BRIEF, AudioFormat.CRITIQUE, AudioFormat.DEBATE],
)
async def test_generate_audio_rejects_unevidenced_formats_before_io(
    audio_format: AudioFormat,
) -> None:
    session, notebooks, _, _, api = _graph()

    with pytest.raises(UnsupportedOperationError, match="audio_format"):
        await api.generate_audio("notebook-1", audio_format=audio_format)

    assert session.calls == []
    assert notebooks.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source_ids": []}, "at least one source"),
        ({"source_ids": ["source-1"], "language": ""}, "non-empty"),
        ({"source_ids": ["source-1"], "language": "  "}, "non-empty"),
        ({"source_ids": ["source-1"], "language": object()}, "non-empty"),
        ({"source_ids": ["source-1"], "instructions": object()}, "instructions"),
        ({"source_ids": ["source-1"], "audio_format": 1}, "AudioFormat"),
        ({"source_ids": ["source-1"], "audio_length": 2}, "AudioLength"),
    ],
)
async def test_generate_audio_validation_is_pre_transport(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    session, notebooks, _, _, api = _graph()

    with pytest.raises(ValidationError, match=message):
        await api.generate_audio("notebook-1", **kwargs)

    assert session.calls == []
    assert notebooks.calls == []


@pytest.mark.asyncio
async def test_generate_audio_rejects_empty_resolved_sources_without_mutation() -> None:
    session, notebooks, _, _, api = _graph()
    notebooks.source_ids = []

    with pytest.raises(ValidationError, match="at least one source"):
        await api.generate_audio("notebook-1")

    assert notebooks.calls == ["notebook-1"]
    assert session.calls == []


@pytest.mark.asyncio
async def test_generate_audio_rejects_mismatched_response_family() -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("wrong", type_code=_PROTO.ARTIFACT_TYPE_APP, variant=2)
    )

    with pytest.raises(DecodingError, match="different artifact family"):
        await api.generate_audio("notebook-1", source_ids=["source-1"])

    assert [call[0] for call in session.calls] == [CREATE_ARTIFACT_METHOD]
    assert session.calls[0][2]["replay_safe"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cinematic", "expected_format"),
    [(False, _PROTO.TEMPLATE_FORMAT_BRIEF), (True, _PROTO.TEMPLATE_FORMAT_BREAKDOWN)],
)
async def test_generate_video_families_use_exact_mobile_options(
    cinematic: bool,
    expected_format: int,
) -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact(
            "video-1",
            type_code=_PROTO.ARTIFACT_TYPE_EXPLAINER_VIDEO,
            status=_PROTO.ARTIFACT_STATUS_PROCESSING,
        )
    )

    if cinematic:
        status = await api.generate_cinematic_video(
            "notebook-1",
            source_ids=["source-1"],
            language="fr",
            instructions="Use the evidence",
        )
    else:
        status = await api.generate_video(
            "notebook-1",
            source_ids=["source-1"],
            language="fr",
            instructions="Use the evidence",
            video_format=VideoFormat.BRIEF,
            video_style=VideoStyle.WATERCOLOR,
        )

    assert status.task_id == "video-1"
    method, request, kwargs = session.calls[0]
    assert method == CREATE_ARTIFACT_METHOD
    assert kwargs["replay_safe"] is False
    assert kwargs["expected_epoch"] == 7
    assert request.artifact.type == _PROTO.ARTIFACT_TYPE_EXPLAINER_VIDEO
    assert [row.source_id.id for row in request.artifact.sources] == ["source-1"]
    options = request.artifact.explainer_video.generation_options
    assert [source.id for source in options.source_ids] == ["source-1"]
    assert options.language_code == "fr"
    assert options.video_focus == "Use the evidence"
    assert options.template_format == expected_format
    assert options.video_overview_style == (
        _PROTO.VIDEO_OVERVIEW_STYLE_UNSPECIFIED
        if cinematic
        else _PROTO.VIDEO_OVERVIEW_STYLE_WATERCOLOR
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("study_guide", "expected_title", "directive_fragment"),
    [
        (False, "Custom Report", "A custom directive"),
        (True, "Study Guide", "Extra emphasis"),
    ],
)
async def test_generate_report_families_use_exact_mobile_options(
    study_guide: bool,
    expected_title: str,
    directive_fragment: str,
) -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("report-1", type_code=_PROTO.ARTIFACT_TYPE_TAILORED_REPORT)
    )

    if study_guide:
        status = await api.generate_study_guide(
            "notebook-1",
            source_ids=["source-1"],
            language="de",
            extra_instructions="Extra emphasis",
        )
    else:
        status = await api.generate_report(
            "notebook-1",
            ReportFormat.CUSTOM,
            source_ids=["source-1"],
            language="de",
            custom_prompt="A custom directive",
        )

    assert status.task_id == "report-1"
    request = session.calls[0][1]
    assert request.artifact.type == _PROTO.ARTIFACT_TYPE_TAILORED_REPORT
    options = request.artifact.tailored_report.generation_options
    assert options.type == expected_title
    assert options.description
    assert [source.id for source in options.source_ids] == ["source-1"]
    assert options.language_code == "de"
    assert directive_fragment in options.document_directive


@pytest.mark.asyncio
async def test_generate_flashcards_uses_exact_nested_variant_options() -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact(
            "cards-1",
            type_code=_PROTO.ARTIFACT_TYPE_APP,
            variant=_PROTO.APP_TYPE_FLASHCARDS,
        )
    )

    status = await api.generate_flashcards(
        "notebook-1",
        source_ids=["source-1"],
        instructions="Key dates",
        quantity=QuizQuantity.MORE,
        difficulty=QuizDifficulty.HARD,
    )

    assert status.task_id == "cards-1"
    options = session.calls[0][1].artifact.app.generation_options
    assert options.app_type == _PROTO.APP_TYPE_FLASHCARDS
    assert options.free_text_steering_prompt == "Key dates"
    assert options.flashcards_generation_options.card_quantity == 3
    assert options.flashcards_generation_options.flashcards_difficulty == 3


@pytest.mark.asyncio
async def test_generate_flashcards_rejects_a_quiz_variant_response() -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact(
            "wrong-app",
            type_code=_PROTO.ARTIFACT_TYPE_APP,
            variant=_PROTO.APP_TYPE_QUIZ,
        )
    )

    with pytest.raises(DecodingError, match="different artifact family"):
        await api.generate_flashcards("notebook-1", source_ids=["source-1"])

    assert [call[0] for call in session.calls] == [CREATE_ARTIFACT_METHOD]
    assert session.calls[0][2]["replay_safe"] is False


@pytest.mark.asyncio
async def test_generate_infographic_uses_exact_mobile_options() -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("image-1", type_code=_PROTO.ARTIFACT_TYPE_INFOGRAPHIC)
    )

    await api.generate_infographic(
        "notebook-1",
        source_ids=["source-1"],
        language="es",
        instructions="Visual summary",
        orientation=InfographicOrientation.PORTRAIT,
        style=InfographicStyle.SCIENTIFIC,
    )

    request = session.calls[0][1]
    options = request.artifact.infographic.generation_options
    assert options.user_steering_prompt == "Visual summary"
    assert options.language_code == "es"
    assert options.aspect_ratio == _PROTO.InfographicGenerationOptions.ASPECT_RATIO_PORTRAIT
    assert options.style == _PROTO.InfographicGenerationOptions.STYLE_SCIENTIFIC


@pytest.mark.asyncio
async def test_generate_slide_deck_uses_exact_mobile_options() -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("slides-1", type_code=_PROTO.ARTIFACT_TYPE_SLIDES)
    )

    await api.generate_slide_deck(
        "notebook-1",
        source_ids=["source-1"],
        language="ja",
        instructions="Speaker notes",
        slide_format=SlideDeckFormat.PRESENTER_SLIDES,
        slide_length=SlideDeckLength.SHORT,
    )

    options = session.calls[0][1].artifact.slides.generation_options
    assert options.user_steering_prompt == "Speaker notes"
    assert options.language_code == "ja"
    assert options.deck_type == _PROTO.DECK_TYPE_PRESENTATION
    assert options.length == _PROTO.SLIDE_DECK_LENGTH_SHORT


@pytest.mark.asyncio
async def test_infographic_detail_level_remains_a_pre_io_evidence_gate() -> None:
    session, notebooks, _, _, api = _graph()

    with pytest.raises(UnsupportedOperationError, match="detail_level"):
        await api.generate_infographic(
            "notebook-1",
            detail_level=InfographicDetail.DETAILED,
        )

    assert session.calls == []
    assert notebooks.calls == []


@pytest.mark.asyncio
async def test_missing_artifact_identity_is_a_bounded_decode_error() -> None:
    raw_title = "raw title must not become a decoder diagnostic"
    session, _, _, _, api = _graph([_artifact("", title=raw_title)])
    with pytest.raises(DecodingError, match="required artifact id") as raised:
        await api.list("notebook-1")
    assert raised.value.method_id == LIST_ARTIFACTS_METHOD
    assert raw_title not in str(raised.value)


@pytest.mark.asyncio
async def test_unknown_artifact_enums_are_retained_without_inventing_a_family() -> None:
    session, _, _, _, api = _graph([_artifact("future", type_code=99, status=99)])
    with pytest.warns(UnknownTypeWarning, match="Unknown artifact type 99"):
        listed = await api.list("notebook-1", ArtifactType.UNKNOWN)
    assert len(listed) == 1
    assert listed[0]._artifact_type == 99
    assert listed[0].status == 99
    assert listed[0].kind is ArtifactType.UNKNOWN
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_failed_quiz_mutation_is_not_replayed() -> None:
    session, _, _, _, api = _graph()
    original = RPCError("sanitized unavailable", rpc_code=14)
    session.errors[CREATE_ARTIFACT_METHOD] = original
    with pytest.raises(RPCError) as raised:
        await api.generate_quiz("notebook-1", source_ids=["source-1"])
    assert raised.value is original
    assert [call[0] for call in session.calls] == [CREATE_ARTIFACT_METHOD]
    assert session.calls[0][2]["replay_safe"] is False


@pytest.mark.asyncio
async def test_all_unsupported_public_paths_reject_before_collaborator_io() -> None:
    session, notebooks, mind_maps, assets, api = _graph()
    invocations: list[Callable[[], Awaitable[Any]]] = [
        lambda: api.generate_data_table("n"),
        lambda: api.revise_slide("n", "a", 0, "p"),
        lambda: api.retry_failed("n", "a"),
        lambda: api.generate_mind_map("n"),
        lambda: api.download_audio("n", "out"),
        lambda: api.download_video("n", "out"),
        lambda: api.download_slide_deck("n", "out"),
        lambda: api.download_report("n", "out"),
        lambda: api.download_mind_map("n", "out"),
        lambda: api.download_data_table("n", "out"),
        lambda: api.download_quiz("n", "out"),
        lambda: api.download_flashcards("n", "out"),
        lambda: api.export_report("n", "a"),
        lambda: api.export_data_table("n", "a"),
        lambda: api.export("n", "a"),
    ]

    for invoke in invocations:
        with pytest.raises(UnsupportedOperationError):
            await invoke()

    assert session.calls == []
    assert notebooks.calls == []
    assert mind_maps.calls == []
    assert assets.calls == []


@pytest.mark.asyncio
async def test_non_none_raw_artifact_rows_reject_before_infographic_io() -> None:
    session, _, _, assets, api = _graph([_artifact("image")])
    with pytest.raises(UnsupportedOperationError):
        await api.download_infographic("notebook-1", "out.png", artifacts_data=[])
    assert session.calls == []
    assert assets.calls == []


@pytest.mark.asyncio
async def test_infographic_selects_requested_or_latest_ready_representation() -> None:
    older = _artifact("old", url="https://lh3.googleusercontent.com/old?cap=1")
    older.last_modified_timestamp.seconds = 1
    latest = _artifact("latest", url="https://lh3.googleusercontent.com/new?cap=2")
    latest.last_modified_timestamp.seconds = 2
    _, _, _, assets, api = _graph([older, latest])

    assert await api.download_infographic("notebook-1", "latest.png") == "latest.png"
    assert assets.calls[-1] == (
        "https://lh3.googleusercontent.com/new?cap=2",
        "latest.png",
    )
    assert await api.download_infographic("notebook-1", "old.png", artifact_id="old") == "old.png"
    assert assets.calls[-1][0].endswith("/old?cap=1")


@pytest.mark.asyncio
async def test_infographic_missing_and_missing_representation_match_web_errors() -> None:
    _, _, _, _, missing_api = _graph()
    with pytest.raises(ArtifactNotReadyError) as not_ready:
        await missing_api.download_infographic("notebook-1", "out.png", artifact_id="missing")
    assert not_ready.value.artifact_id == "missing"

    _, _, _, _, malformed_api = _graph([_artifact("image-without-url")])
    with pytest.raises(ArtifactParseError) as malformed:
        await malformed_api.download_infographic(
            "notebook-1", "out.png", artifact_id="image-without-url"
        )
    assert malformed.value.artifact_id == "image-without-url"
    assert malformed.value.details == "Could not find metadata"


@pytest.mark.asyncio
async def test_infographic_wraps_transfer_error_without_capability_or_cause() -> None:
    secret_url = "https://lh3.googleusercontent.com/object?secret=capability"
    _, _, _, assets, api = _graph([_artifact("image-1", url=secret_url)])
    assets.error = ArtifactDownloadError(
        "infographic",
        details="Android transfer failed (code=http_403, host=lh3.googleusercontent.com, hop=0).",
        status_code=403,
    )

    with pytest.raises(ArtifactDownloadError) as raised:
        await api.download_infographic("notebook-1", "out.png")

    assert raised.value.artifact_id == "image-1"
    assert raised.value.status_code == 403
    assert raised.value.cause is None
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert secret_url not in str(raised.value)
    for frame, _line in traceback.walk_tb(raised.value.__traceback__):
        if "/src/notebooklm/" not in frame.f_code.co_filename:
            continue
        assert secret_url not in repr(frame.f_locals)
        assert api not in frame.f_locals.values()


@pytest.mark.asyncio
async def test_delete_is_idempotent_only_for_sanitized_not_found() -> None:
    session, _, _, _, api = _graph()
    await api.delete("notebook-1", "artifact-1")
    method, request, kwargs = session.calls[-1]
    assert method == DELETE_ARTIFACT_METHOD
    assert request == _PROTO.DeleteArtifactRequest(artifact_id="artifact-1")
    assert kwargs == {"replay_safe": False, "response_type": empty_pb2.Empty}

    session.errors[DELETE_ARTIFACT_METHOD] = RPCError("missing", rpc_code=5)
    await api.delete("notebook-1", "missing")
    original = RPCError("denied", rpc_code=7)
    session.errors[DELETE_ARTIFACT_METHOD] = original
    with pytest.raises(RPCError) as raised:
        await api.delete("notebook-1", "denied")
    assert raised.value is original


@pytest.mark.asyncio
async def test_rename_preflights_etag_updates_once_and_reads_back() -> None:
    before = _artifact("artifact-1", title="Before", etag="etag-before")
    after = _artifact("artifact-1", title="After", etag="etag-after")
    session, _, mind_maps, _, api = _graph()
    session.responses[LIST_ARTIFACTS_METHOD] = [
        _PROTO.ListArtifactsResponse(artifacts=[before]),
        _PROTO.ListArtifactsResponse(artifacts=[after]),
    ]
    session.responses[UPDATE_ARTIFACT_METHOD] = _artifact("artifact-1", title="After")

    renamed = await api.rename("notebook-1", "artifact-1", "After")

    assert renamed is not None and renamed.title == "After"
    assert mind_maps.calls == []
    method, request, kwargs = session.calls[1]
    assert method == UPDATE_ARTIFACT_METHOD
    assert request.artifact.artifact_id == "artifact-1"
    assert request.artifact.title == "After"
    assert list(request.update_mask.paths) == ["title"]
    assert request.etag == "etag-before"
    assert kwargs == {
        "replay_safe": False,
        "response_type": _PROTO.Artifact,
        "expected_epoch": 7,
    }


@pytest.mark.asyncio
async def test_rename_missing_or_missing_etag_fails_before_mutation() -> None:
    session, _, _, _, api = _graph()
    with pytest.raises(ArtifactNotFoundError):
        await api.rename("notebook-1", "missing", "After", return_object=False)
    assert [call[0] for call in session.calls] == [LIST_ARTIFACTS_METHOD]

    session.calls.clear()
    session.responses[LIST_ARTIFACTS_METHOD] = _PROTO.ListArtifactsResponse(
        artifacts=[_artifact("artifact-1", etag="")]
    )
    with pytest.raises(DecodingError, match="etag"):
        await api.rename("notebook-1", "artifact-1", "After")
    assert [call[0] for call in session.calls] == [LIST_ARTIFACTS_METHOD]


@pytest.mark.asyncio
async def test_rename_rejects_wrong_bare_update_identity_without_replay() -> None:
    session, _, _, _, api = _graph()
    session.responses[LIST_ARTIFACTS_METHOD] = _PROTO.ListArtifactsResponse(
        artifacts=[_artifact("artifact-1")]
    )
    session.responses[UPDATE_ARTIFACT_METHOD] = _artifact("other-artifact")

    with pytest.raises(DecodingError, match="different artifact id"):
        await api.rename("notebook-1", "artifact-1", "After")

    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        UPDATE_ARTIFACT_METHOD,
    ]
    assert session.calls[1][2] == {
        "replay_safe": False,
        "response_type": _PROTO.Artifact,
        "expected_epoch": 7,
    }


@pytest.mark.asyncio
async def test_rename_readback_finishes_during_graceful_drain_in_one_epoch() -> None:
    transport = SupervisedAndroidTransport()
    mutation_started = asyncio.Event()
    mutation_release = asyncio.Event()

    def _list(_request: Any, _kwargs: dict[str, Any]) -> Any:
        lists = [call for call in transport.calls if call[0] == LIST_ARTIFACTS_METHOD]
        artifact = (
            _artifact("artifact-1", title="Before", etag="etag-before")
            if len(lists) == 1
            else _artifact("artifact-1", title="After", etag="etag-after")
        )
        return _PROTO.ListArtifactsResponse(artifacts=[artifact])

    async def _update(_request: Any, _kwargs: dict[str, Any]) -> Any:
        mutation_started.set()
        await mutation_release.wait()
        return _artifact("artifact-1", title="After")

    transport.handlers[LIST_ARTIFACTS_METHOD] = _list
    transport.handlers[UPDATE_ARTIFACT_METHOD] = _update
    task = asyncio.create_task(
        _supervised_graph(transport).rename("notebook-1", "artifact-1", "After")
    )
    await mutation_started.wait()

    await transport.supervisor.stop_accepting(1)
    mutation_release.set()

    result = await task
    assert result is not None and result.title == "After"
    assert [kwargs["expected_epoch"] for _method, _request, kwargs in transport.calls] == [
        1,
        1,
        1,
    ]
    await transport.supervisor.wait_for_idle(1, 0.1)


@pytest.mark.asyncio
async def test_rename_readback_cannot_cross_forced_close_and_reopen() -> None:
    transport = SupervisedAndroidTransport()
    mutation_started = asyncio.Event()
    mutation_release = asyncio.Event()
    transport.handlers[LIST_ARTIFACTS_METHOD] = _PROTO.ListArtifactsResponse(
        artifacts=[_artifact("artifact-1")]
    )

    async def _update(_request: Any, _kwargs: dict[str, Any]) -> Any:
        mutation_started.set()
        await mutation_release.wait()
        return _artifact("artifact-1", title="After")

    transport.handlers[UPDATE_ARTIFACT_METHOD] = _update
    task = asyncio.create_task(
        _supervised_graph(transport).rename("notebook-1", "artifact-1", "After")
    )
    await mutation_started.wait()

    old_generation = await transport.force_close_and_reopen()
    mutation_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in transport.calls] == [
        LIST_ARTIFACTS_METHOD,
        UPDATE_ARTIFACT_METHOD,
    ]
    assert old_generation.in_flight == 0


class _SupervisedMindMapLister:
    def __init__(self, transport: SupervisedAndroidTransport) -> None:
        self._transport = transport

    async def list_mind_map_artifacts(self, notebook_id: str) -> list[Artifact]:
        return await self._transport.unary(
            "notes.list_mind_map_artifacts",
            notebook_id,
            replay_safe=True,
            response_type=list,
        )


@pytest.mark.asyncio
async def test_aggregate_list_finishes_during_graceful_drain() -> None:
    transport = SupervisedAndroidTransport()
    studio_started = asyncio.Event()
    studio_release = asyncio.Event()

    async def _studio(_request: Any, _kwargs: dict[str, Any]) -> Any:
        studio_started.set()
        await studio_release.wait()
        return _PROTO.ListArtifactsResponse(artifacts=[_artifact("studio")])

    transport.handlers[LIST_ARTIFACTS_METHOD] = _studio
    transport.handlers["notes.list_mind_map_artifacts"] = [_mind_map()]
    api = _supervised_graph(transport, mind_maps=_SupervisedMindMapLister(transport))
    task = asyncio.create_task(api.list("notebook-1"))
    await studio_started.wait()

    await transport.supervisor.stop_accepting(1)
    studio_release.set()

    assert [item.id for item in await task] == ["studio", "note-map"]
    assert [method for method, _request, _kwargs in transport.calls] == [
        LIST_ARTIFACTS_METHOD,
        "notes.list_mind_map_artifacts",
    ]
    await transport.supervisor.wait_for_idle(1, 0.1)


@pytest.mark.asyncio
async def test_aggregate_list_cannot_cross_forced_close_and_reopen() -> None:
    transport = SupervisedAndroidTransport()
    studio_started = asyncio.Event()
    studio_release = asyncio.Event()

    async def _studio(_request: Any, _kwargs: dict[str, Any]) -> Any:
        studio_started.set()
        await studio_release.wait()
        return _PROTO.ListArtifactsResponse(artifacts=[_artifact("studio")])

    transport.handlers[LIST_ARTIFACTS_METHOD] = _studio
    transport.handlers["notes.list_mind_map_artifacts"] = [_mind_map()]
    api = _supervised_graph(transport, mind_maps=_SupervisedMindMapLister(transport))
    task = asyncio.create_task(api.list("notebook-1"))
    await studio_started.wait()

    old_generation = await transport.force_close_and_reopen()
    studio_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in transport.calls] == [LIST_ARTIFACTS_METHOD]
    assert old_generation.in_flight == 0


class _SupervisedNotebookSources:
    def __init__(self, transport: SupervisedAndroidTransport) -> None:
        self._transport = transport

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        return await self._transport.unary(
            "notebooks.get_source_ids",
            notebook_id,
            replay_safe=True,
            response_type=list,
        )


@pytest.mark.asyncio
async def test_quiz_source_resolution_and_mutation_finish_during_graceful_drain() -> None:
    transport = SupervisedAndroidTransport()
    sources_started = asyncio.Event()
    sources_release = asyncio.Event()

    async def _sources(_request: Any, _kwargs: dict[str, Any]) -> Any:
        sources_started.set()
        await sources_release.wait()
        return ["source-1"]

    transport.handlers["notebooks.get_source_ids"] = _sources
    transport.handlers[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("quiz", type_code=_PROTO.ARTIFACT_TYPE_APP, variant=2)
    )
    api = _supervised_graph(transport, notebooks=_SupervisedNotebookSources(transport))
    task = asyncio.create_task(api.generate_quiz("notebook-1"))
    await sources_started.wait()

    await transport.supervisor.stop_accepting(1)
    sources_release.set()

    assert (await task).task_id == "quiz"
    assert [method for method, _request, _kwargs in transport.calls] == [
        "notebooks.get_source_ids",
        CREATE_ARTIFACT_METHOD,
    ]
    assert transport.calls[1][2]["expected_epoch"] == 1


@pytest.mark.asyncio
async def test_audio_source_resolution_and_mutation_finish_during_graceful_drain() -> None:
    transport = SupervisedAndroidTransport()
    sources_started = asyncio.Event()
    sources_release = asyncio.Event()

    async def _sources(_request: Any, _kwargs: dict[str, Any]) -> Any:
        sources_started.set()
        await sources_release.wait()
        return ["source-1"]

    transport.handlers["notebooks.get_source_ids"] = _sources
    transport.handlers[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("audio", type_code=_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW)
    )
    api = _supervised_graph(transport, notebooks=_SupervisedNotebookSources(transport))
    task = asyncio.create_task(api.generate_audio("notebook-1"))
    await sources_started.wait()

    await transport.supervisor.stop_accepting(1)
    sources_release.set()

    assert (await task).task_id == "audio"
    assert [method for method, _request, _kwargs in transport.calls] == [
        "notebooks.get_source_ids",
        CREATE_ARTIFACT_METHOD,
    ]
    assert transport.calls[1][2]["expected_epoch"] == 1
    await transport.supervisor.wait_for_idle(1, 0.1)


@pytest.mark.asyncio
async def test_audio_mutation_cannot_cross_forced_close_and_reopen() -> None:
    transport = SupervisedAndroidTransport()
    sources_started = asyncio.Event()
    sources_release = asyncio.Event()

    async def _sources(_request: Any, _kwargs: dict[str, Any]) -> Any:
        sources_started.set()
        await sources_release.wait()
        return ["source-1"]

    transport.handlers["notebooks.get_source_ids"] = _sources
    transport.handlers[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("audio", type_code=_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW)
    )
    api = _supervised_graph(transport, notebooks=_SupervisedNotebookSources(transport))
    task = asyncio.create_task(api.generate_audio("notebook-1"))
    await sources_started.wait()

    old_generation = await transport.force_close_and_reopen()
    sources_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in transport.calls] == ["notebooks.get_source_ids"]
    assert old_generation.in_flight == 0


class _SupervisedAssets:
    def __init__(self, transport: SupervisedAndroidTransport) -> None:
        self._transport = transport

    async def download_url(self, url: str, output_path: str) -> str:
        return await self._transport.unary(
            "assets.download_url",
            (url, output_path),
            replay_safe=False,
            response_type=str,
        )


@pytest.mark.asyncio
async def test_infographic_transfer_cannot_cross_forced_close_and_reopen() -> None:
    transport = SupervisedAndroidTransport()
    studio_started = asyncio.Event()
    studio_release = asyncio.Event()

    async def _studio(_request: Any, _kwargs: dict[str, Any]) -> Any:
        studio_started.set()
        await studio_release.wait()
        return _PROTO.ListArtifactsResponse(
            artifacts=[_artifact("image", url="https://lh3.googleusercontent.com/image.png")]
        )

    transport.handlers[LIST_ARTIFACTS_METHOD] = _studio
    transport.handlers["assets.download_url"] = "out.png"
    api = _supervised_graph(transport, assets=_SupervisedAssets(transport))
    task = asyncio.create_task(api.download_infographic("notebook-1", "out.png"))
    await studio_started.wait()

    old_generation = await transport.force_close_and_reopen()
    studio_release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert [method for method, _request, _kwargs in transport.calls] == [LIST_ARTIFACTS_METHOD]
    assert old_generation.in_flight == 0


@pytest.mark.asyncio
async def test_report_suggestions_use_generated_signature_and_preserve_defaulted_rows() -> None:
    session, _, _, _, api = _graph()
    session.responses[GENERATE_REPORT_SUGGESTIONS_METHOD] = (
        _PROTO.GenerateReportSuggestionsResponse(
            suggestions=[
                _PROTO.ReportSuggestion(
                    title="Brief",
                    description="A focused report",
                    prompt="Write the report",
                    audience_level=1,
                ),
                _PROTO.ReportSuggestion(title="missing prompt"),
            ]
        )
    )

    suggestions = await api.suggest_reports("notebook-1")

    assert [(item.title, item.prompt, item.audience_level) for item in suggestions] == [
        ("Brief", "Write the report", 1),
        ("missing prompt", "", 2),
    ]
    method, request, kwargs = session.calls[0]
    assert method == GENERATE_REPORT_SUGGESTIONS_METHOD
    assert request.project_id == "notebook-1"
    assert list(request.source_ids) == []
    assert request.HasField("request_context")
    assert request.request_context.client_type != 0
    assert kwargs == {
        "replay_safe": True,
        "response_type": _PROTO.GenerateReportSuggestionsResponse,
    }


def test_artifact_source_uses_the_imported_exact_package_source_id() -> None:
    request = _PROTO.CreateArtifactRequest(
        project_id="notebook-1",
        artifact=_PROTO.Artifact(
            sources=[_PROTO.ArtifactSource(source_id=read_pb2.SourceId(id="source-1"))]
        ),
    )
    assert request.artifact.sources[0].source_id.id == "source-1"
