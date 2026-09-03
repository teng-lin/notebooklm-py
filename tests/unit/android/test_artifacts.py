"""Offline contract tests for the evidence-qualified artifact adapter."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from google.protobuf import empty_pb2
from tests._helpers.android_supervisor import SupervisedAndroidTransport

from notebooklm._android import artifact_outputs
from notebooklm._android import artifacts as android_artifacts
from notebooklm._android import notes as android_notes
from notebooklm._android.artifact_outputs import report_doc_markdown, write_text_atomic
from notebooklm._android.artifacts import (
    ACT_ON_SOURCES_METHOD,
    CREATE_ARTIFACT_METHOD,
    DELETE_ARTIFACT_METHOD,
    DERIVE_ARTIFACT_METHOD,
    EXPORT_TO_DRIVE_METHOD,
    GENERATE_ARTIFACT_METHOD,
    GENERATE_REPORT_SUGGESTIONS_METHOD,
    GET_ARTIFACT_METHOD,
    LIST_ARTIFACTS_METHOD,
    UPDATE_ARTIFACT_METHOD,
    AndroidArtifactsAPI,
)
from notebooklm._android.assets import AndroidAssetDownloadService
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    artifacts_pb2,
    chat_pb2,
    read_pb2,
)
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import artifacts_pb2 as wire_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._client_metrics import ClientMetrics
from notebooklm._notebook_metadata import NotebookSourceIdProvider
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._types.common import UnknownTypeWarning
from notebooklm._types.enums import (
    ArtifactTypeCode,
    AudioFormat,
    AudioLength,
    ExportType,
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
    ArtifactFeatureUnavailableError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    AuthError,
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
    ValidationError,
)
from notebooklm.types import Artifact, ArtifactType, MindMap, MindMapKind, Note

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
        self.mind_maps: list[MindMap] = []
        self.calls: list[str] = []
        self.error: BaseException | None = None

    async def list_mind_map_artifacts(self, notebook_id: str) -> list[Artifact]:
        self.calls.append(notebook_id)
        if self.error is not None:
            raise self.error
        return list(self.artifacts)

    async def list_note_backed_mind_maps(self, notebook_id: str) -> list[MindMap]:
        self.calls.append(notebook_id)
        if self.error is not None:
            raise self.error
        return list(self.mind_maps)


class FakeAssets:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.representation_calls: list[tuple[str, str, str]] = []
        self.error: BaseException | None = None

    async def download_url(self, url: str, output_path: str) -> str:
        self.calls.append((url, output_path))
        if self.error is not None:
            raise self.error
        return output_path

    async def download_urls_batch(self, urls_and_paths: list[tuple[str, str]]) -> Any:
        raise AssertionError(f"batch transfer not expected: {urls_and_paths!r}")

    async def download_representation(
        self,
        url: str,
        output_path: str,
        *,
        representation: str,
    ) -> str:
        self.representation_calls.append((url, output_path, representation))
        if self.error is not None:
            raise self.error
        return output_path


def _supervisor() -> CallSupervisor:
    return CallSupervisor(
        metrics=ClientMetrics(),
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
    source_ids: list[str] | None = None,
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
    for source_id in source_ids or []:
        message.sources.add().source_id.id = source_id
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
            DERIVE_ARTIFACT_METHOD: _PROTO.DeriveArtifactResponse(),
            GENERATE_ARTIFACT_METHOD: _PROTO.GenerateArtifactResponse(),
            EXPORT_TO_DRIVE_METHOD: _PROTO.ExportToDriveResponse(),
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
    parameters = inspect.signature(AndroidArtifactsAPI).parameters
    assert parameters["mind_maps"].default is inspect.Parameter.empty
    assert "note_backed_generator" not in parameters
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
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        GET_ARTIFACT_METHOD,
    ]
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
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        GET_ARTIFACT_METHOD,
    ]
    assert mind_maps.calls == []


@pytest.mark.asyncio
async def test_poll_rejects_wrong_get_artifact_identity() -> None:
    session, _, mind_maps, _, api = _graph([_artifact("target")])
    session.responses[GET_ARTIFACT_METHOD] = _PROTO.GetArtifactResponse(
        artifact=_artifact("other", type_code=_PROTO.ARTIFACT_TYPE_APP, variant=2)
    )
    await _activate(api._supervisor)

    with pytest.raises(DecodingError, match="different artifact id") as raised:
        await api.poll_status("notebook-1", "target")

    assert raised.value.method_id == GET_ARTIFACT_METHOD
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        GET_ARTIFACT_METHOD,
    ]
    assert mind_maps.calls == []


@pytest.mark.asyncio
async def test_private_get_artifact_helper_uses_exact_request_and_epoch() -> None:
    session, _, _, _, api = _graph([_artifact("target")])

    result = await api._get_studio_artifact("notebook-1", "target", expected_epoch=42)

    assert result is not None and result.id == "target"
    assert session.calls[0][0] == LIST_ARTIFACTS_METHOD
    method, request, kwargs = session.calls[1]
    assert method == GET_ARTIFACT_METHOD
    assert request == _PROTO.GetArtifactRequest(artifact_id="target")
    assert kwargs == {
        "replay_safe": True,
        "response_type": _PROTO.GetArtifactResponse,
        "expected_epoch": 42,
    }


@pytest.mark.asyncio
async def test_get_artifact_missing_payload_is_bounded_decode_error() -> None:
    session, _, _, _, api = _graph([_artifact("target")])
    session.responses[GET_ARTIFACT_METHOD] = _PROTO.GetArtifactResponse()

    with pytest.raises(DecodingError, match="omitted its artifact") as raised:
        await api._get_studio_artifact("notebook-1", "target", expected_epoch=7)

    assert raised.value.method_id == GET_ARTIFACT_METHOD
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_get_artifact_identity_failure_drops_capability_response_from_frames() -> None:
    secret = "https://lh3.googleusercontent.com/image.png?cap=get-secret"
    raw_response = _PROTO.GetArtifactResponse(artifact=_artifact("other", url=secret))
    session, _, _, _, api = _graph([_artifact("target")])
    session.responses[GET_ARTIFACT_METHOD] = raw_response

    with pytest.raises(DecodingError) as raised:
        await api._get_studio_artifact("notebook-1", "target", expected_epoch=7)

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
    await _activate(api._supervisor)

    status = await api.poll_status("notebook-1", "missing")

    assert status.is_not_found
    assert [call[0] for call in session.calls] == [LIST_ARTIFACTS_METHOD]
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
async def test_generate_quiz_uses_public_standard_medium_defaults() -> None:
    session, _, _, _, api = _graph([_artifact("failed-1")])
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact(
            "quiz-defaults",
            type_code=_PROTO.ARTIFACT_TYPE_APP,
            variant=_PROTO.APP_TYPE_QUIZ,
        )
    )

    await api.generate_quiz("notebook-1", source_ids=["source-1"])

    options = session.calls[0][1].artifact.app.generation_options.quiz_generation_options
    assert options.question_quantity == QuizQuantity.STANDARD.value
    assert options.quiz_difficulty == QuizDifficulty.MEDIUM.value


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
async def test_generate_quiz_rejects_mismatched_nonempty_response_sources() -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact(
            "wrong-sources",
            type_code=_PROTO.ARTIFACT_TYPE_APP,
            variant=_PROTO.APP_TYPE_QUIZ,
            source_ids=["source-2"],
        )
    )

    with pytest.raises(DecodingError, match="different source ids"):
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
async def test_generate_audio_formats_use_live_wire_overlay(
    audio_format: AudioFormat,
) -> None:
    session, notebooks, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("audio-format", type_code=_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW)
    )

    status = await api.generate_audio("notebook-1", audio_format=audio_format)

    assert status.task_id == "audio-format"
    options = session.calls[0][1].artifact.audio_overview.generation_options
    projection = wire_pb2.WireAudioOverviewGenerationOptionsProjection()
    projection.ParseFromString(options.SerializeToString())
    assert projection.format == audio_format.value
    assert notebooks.calls == ["notebook-1"]


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
    session, _, _, _, api = _graph([_artifact("failed-1")])
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("wrong", type_code=_PROTO.ARTIFACT_TYPE_APP, variant=2)
    )

    with pytest.raises(DecodingError, match="different artifact family"):
        await api.generate_audio("notebook-1", source_ids=["source-1"])

    assert [call[0] for call in session.calls] == [CREATE_ARTIFACT_METHOD]
    assert session.calls[0][2]["replay_safe"] is False


@pytest.mark.asyncio
async def test_generate_audio_rejects_mismatched_nonempty_response_sources() -> None:
    session, _, _, _, api = _graph([_artifact("failed-1")])
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact(
            "wrong-sources",
            type_code=_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW,
            source_ids=["source-2"],
        )
    )

    with pytest.raises(DecodingError, match="different source ids") as raised:
        await api.generate_audio("notebook-1", source_ids=["source-1"])
    assert getattr(raised.value, "unconfirmed", False) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            NetworkError("connection lost", method_id=CREATE_ARTIFACT_METHOD),
            id="network",
        ),
        pytest.param(
            RateLimitError(
                "response lost",
                method_id=CREATE_ARTIFACT_METHOD,
                rpc_code=8,
            ),
            id="rate-limit",
        ),
        pytest.param(
            ServerError(
                "response lost",
                method_id=CREATE_ARTIFACT_METHOD,
                rpc_code=14,
            ),
            id="server",
        ),
    ],
)
@pytest.mark.parametrize("family", ["audio", "quiz"])
async def test_create_artifact_lost_response_is_unconfirmed_and_never_replayed(
    error: BaseException,
    family: str,
) -> None:
    session, _, _, _, api = _graph()
    session.errors[CREATE_ARTIFACT_METHOD] = error

    with pytest.raises(RPCError, match="list artifacts.*manually") as caught:
        if family == "audio":
            await api.generate_audio("notebook-1", source_ids=["source-1"])
        else:
            await api.generate_quiz("notebook-1", source_ids=["source-1"])

    assert getattr(caught.value, "unconfirmed", False) is True
    assert caught.value.method_id == CREATE_ARTIFACT_METHOD
    assert caught.value.rpc_code == getattr(error, "rpc_code", None)
    assert caught.value.__cause__ is None
    assert [call[0] for call in session.calls] == [CREATE_ARTIFACT_METHOD]
    assert session.calls[0][2]["replay_safe"] is False


@pytest.mark.asyncio
async def test_generate_video_families_use_exact_mobile_options() -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact(
            "video-1",
            type_code=_PROTO.ARTIFACT_TYPE_EXPLAINER_VIDEO,
            status=_PROTO.ARTIFACT_STATUS_PROCESSING,
        )
    )

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
    assert "expected_epoch" not in kwargs
    assert request.artifact.type == _PROTO.ARTIFACT_TYPE_EXPLAINER_VIDEO
    assert [row.source_id.id for row in request.artifact.sources] == ["source-1"]
    options = request.artifact.explainer_video.generation_options
    assert [source.id for source in options.source_ids] == ["source-1"]
    assert options.language_code == "fr"
    assert options.video_focus == "Use the evidence"
    assert options.template_format == _PROTO.TEMPLATE_FORMAT_BRIEF
    assert options.video_overview_style == _PROTO.VIDEO_OVERVIEW_STYLE_WATERCOLOR


@pytest.mark.asyncio
@pytest.mark.parametrize("through_video_format", [False, True])
async def test_cinematic_video_uses_mobile_template_code_three(
    through_video_format: bool,
) -> None:
    session, notebooks, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("cinematic", type_code=_PROTO.ARTIFACT_TYPE_EXPLAINER_VIDEO)
    )

    if through_video_format:
        status = await api.generate_video(
            "notebook-1",
            video_format=VideoFormat.CINEMATIC,
        )
    else:
        status = await api.generate_cinematic_video("notebook-1")

    assert status.task_id == "cinematic"
    options = session.calls[0][1].artifact.explainer_video.generation_options
    assert options.template_format == _PROTO.TEMPLATE_FORMAT_BREAKDOWN
    assert options.video_overview_style == _PROTO.VIDEO_OVERVIEW_STYLE_UNSPECIFIED
    assert notebooks.calls == ["notebook-1"]


@pytest.mark.asyncio
async def test_cinematic_video_rejects_style_prompt_before_io() -> None:
    session, notebooks, _, _, api = _graph()

    with pytest.raises(ValidationError, match="cinematic"):
        await api.generate_video(
            "notebook-1",
            video_format=VideoFormat.CINEMATIC,
            video_style=VideoStyle.CUSTOM,
            style_prompt="Use hand-drawn diagrams",
        )

    assert session.calls == []
    assert notebooks.calls == []


@pytest.mark.asyncio
async def test_video_style_prompt_requires_string_before_io() -> None:
    session, notebooks, _, _, api = _graph()

    with pytest.raises(ValidationError) as raised:
        await api.generate_video(
            "notebook-1",
            video_style=VideoStyle.CUSTOM,
            style_prompt=cast(Any, 7),
        )

    assert str(raised.value) == "style_prompt must be a string or None"
    assert session.scopes == []
    assert session.calls == []
    assert notebooks.calls == []


@pytest.mark.asyncio
async def test_video_style_validation_precedes_closed_runtime_admission() -> None:
    transport = SupervisedAndroidTransport()
    await transport.supervisor.stop_accepting(1)
    api = _supervised_graph(transport)

    with pytest.raises(ValidationError, match="cinematic"):
        await api.generate_video(
            "notebook-1",
            video_format=VideoFormat.CINEMATIC,
            video_style=VideoStyle.CUSTOM,
            style_prompt="Use hand-drawn diagrams",
        )

    assert transport.calls == []


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
async def test_generate_concept_explanation_uses_live_flexible_report_contract() -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("concept", type_code=_PROTO.ARTIFACT_TYPE_TAILORED_REPORT)
    )

    await api.generate_report(
        "notebook-1",
        ReportFormat.CONCEPT_EXPLANATION,
        source_ids=["source-1"],
    )

    options = session.calls[0][1].artifact.tailored_report.generation_options
    assert options.type == "Concept Explanation"
    assert options.description == "Clear explanations of key concepts"
    assert "common misconceptions" in options.document_directive


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
async def test_infographic_detail_level_uses_live_wire_overlay() -> None:
    session, notebooks, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("detailed", type_code=_PROTO.ARTIFACT_TYPE_INFOGRAPHIC)
    )

    await api.generate_infographic(
        "notebook-1",
        detail_level=InfographicDetail.DETAILED,
    )

    options = session.calls[0][1].artifact.infographic.generation_options
    projection = wire_pb2.WireInfographicGenerationOptionsProjection()
    projection.ParseFromString(options.SerializeToString())
    assert projection.detail_level == InfographicDetail.DETAILED.value
    assert notebooks.calls == ["notebook-1"]


@pytest.mark.asyncio
async def test_infographic_uses_public_standard_detail_default() -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("standard", type_code=_PROTO.ARTIFACT_TYPE_INFOGRAPHIC)
    )

    await api.generate_infographic("notebook-1", source_ids=["source-1"])

    options = session.calls[0][1].artifact.infographic.generation_options
    projection = wire_pb2.WireInfographicGenerationOptionsProjection()
    projection.ParseFromString(options.SerializeToString())
    assert projection.detail_level == InfographicDetail.STANDARD.value


@pytest.mark.asyncio
async def test_generate_data_table_uses_live_local_wire_overlay() -> None:
    session, _, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact("table", type_code=_PROTO.ARTIFACT_TYPE_TABLE)
    )

    status = await api.generate_data_table(
        "notebook-1",
        source_ids=["source-1"],
        language="fr",
        instructions="Compare evidence",
    )

    assert status.task_id == "table"
    request = session.calls[0][1]
    projection = wire_pb2.WireArtifactTableProjection()
    projection.ParseFromString(request.artifact.SerializeToString())
    assert projection.table.generation_options.user_steering_prompt == "Compare evidence"
    assert projection.table.generation_options.language_code == "fr"


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
async def test_retry_failed_uses_web_derived_mobile_generate_artifact_shape() -> None:
    session, _, _, _, api = _graph([_artifact("failed-1")])
    session.responses[GENERATE_ARTIFACT_METHOD] = _PROTO.GenerateArtifactResponse(
        artifact=_artifact(
            "failed-1",
            type_code=_PROTO.ARTIFACT_TYPE_SLIDES,
            status=_PROTO.ARTIFACT_STATUS_INITIALIZED,
        )
    )

    status = await api.retry_failed("notebook-1", "failed-1")

    assert status.task_id == "failed-1"
    assert status.status == "pending"
    method, request, kwargs = session.calls[1]
    assert method == GENERATE_ARTIFACT_METHOD
    assert request.artifact_id == "failed-1"
    assert request.request_context.client_type != 0
    assert kwargs == {
        "replay_safe": False,
        "response_type": _PROTO.GenerateArtifactResponse,
        "expected_epoch": 7,
    }


@pytest.mark.asyncio
async def test_retry_failed_rejects_changed_artifact_identity() -> None:
    session, _, _, _, api = _graph([_artifact("failed-1")])
    session.responses[GENERATE_ARTIFACT_METHOD] = _PROTO.GenerateArtifactResponse(
        artifact=_artifact("different", type_code=_PROTO.ARTIFACT_TYPE_SLIDES)
    )

    with pytest.raises(DecodingError, match="different artifact id"):
        await api.retry_failed("notebook-1", "failed-1")


@pytest.mark.asyncio
async def test_retry_failed_empty_result_is_feature_unavailable() -> None:
    session, _, _, _, api = _graph([_artifact("failed-1")])

    with pytest.raises(ArtifactFeatureUnavailableError) as raised:
        await api.retry_failed("notebook-1", "failed-1")

    assert raised.value.method_id == GENERATE_ARTIFACT_METHOD
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        GENERATE_ARTIFACT_METHOD,
    ]
    assert getattr(raised.value, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_retry_failed_rejects_artifact_outside_notebook_before_mutation() -> None:
    session, _, _, _, api = _graph([_artifact("owned-by-notebook")])

    with pytest.raises(ArtifactNotFoundError):
        await api.retry_failed("notebook-1", "foreign-artifact")

    assert [call[0] for call in session.calls] == [LIST_ARTIFACTS_METHOD]


@pytest.mark.asyncio
async def test_revise_slide_uses_apk_exact_derive_request_without_replay() -> None:
    session, _, _, _, api = _graph([_artifact("original-slides")])
    session.responses[DERIVE_ARTIFACT_METHOD] = _PROTO.DeriveArtifactResponse(
        artifact=_artifact(
            "derived-slides",
            type_code=_PROTO.ARTIFACT_TYPE_SLIDES,
            status=_PROTO.ARTIFACT_STATUS_INITIALIZED,
        )
    )

    result = await api.revise_slide("notebook-1", "original-slides", 3, "Simplify it")

    assert result.task_id == "derived-slides"
    assert result.status == "pending"
    assert session.scopes == ["artifacts.revise_slide"]
    assert session.calls[0][0] == LIST_ARTIFACTS_METHOD
    method, request, kwargs = session.calls[1]
    assert method == DERIVE_ARTIFACT_METHOD
    assert request.original_artifact_id == "original-slides"
    assert request.request_context.client_type != 0
    assert request.slides_derivation_options.slide_edit_instructions == [
        _PROTO.SlideEditInstruction(slide_index=3, edit_instruction="Simplify it")
    ]
    assert kwargs == {
        "replay_safe": False,
        "response_type": _PROTO.DeriveArtifactResponse,
        "expected_epoch": 7,
    }


@pytest.mark.asyncio
async def test_revise_slide_negative_index_rejects_before_io() -> None:
    session, _, _, _, api = _graph()

    with pytest.raises(ValidationError, match="slide_index must be >= 0"):
        await api.revise_slide("notebook-1", "slides", -1, "prompt")

    assert session.calls == []


@pytest.mark.asyncio
async def test_revise_slide_requires_a_slides_artifact_response() -> None:
    session, _, _, _, api = _graph([_artifact("slides")])
    session.responses[DERIVE_ARTIFACT_METHOD] = _PROTO.DeriveArtifactResponse(
        artifact=_artifact("wrong-family", type_code=_PROTO.ARTIFACT_TYPE_INFOGRAPHIC)
    )

    with pytest.raises(DecodingError, match="different artifact family") as raised:
        await api.revise_slide("notebook-1", "slides", 0, "prompt")

    assert raised.value.method_id == DERIVE_ARTIFACT_METHOD
    assert getattr(raised.value, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_revise_slide_requires_artifact_payload() -> None:
    session, _, _, _, api = _graph([_artifact("slides")])

    with pytest.raises(DecodingError, match="omitted its artifact") as raised:
        await api.revise_slide("notebook-1", "slides", 0, "prompt")

    assert raised.value.method_id == DERIVE_ARTIFACT_METHOD
    assert getattr(raised.value, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_revise_slide_requires_a_new_artifact_identity() -> None:
    session, _, _, _, api = _graph([_artifact("slides")])
    session.responses[DERIVE_ARTIFACT_METHOD] = _PROTO.DeriveArtifactResponse(
        artifact=_artifact("slides", type_code=_PROTO.ARTIFACT_TYPE_SLIDES)
    )

    with pytest.raises(DecodingError, match="reused the original artifact id") as raised:
        await api.revise_slide("notebook-1", "slides", 0, "prompt")
    assert getattr(raised.value, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_revise_slide_lost_response_is_unconfirmed_and_never_replayed() -> None:
    session, _, _, _, api = _graph([_artifact("slides")])
    error = NetworkError("derive response lost", method_id=DERIVE_ARTIFACT_METHOD)
    session.errors[DERIVE_ARTIFACT_METHOD] = error

    with pytest.raises(NetworkError) as raised:
        await api.revise_slide("notebook-1", "slides", 0, "prompt")

    assert raised.value is error
    assert getattr(raised.value, "unconfirmed", False) is True
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        DERIVE_ARTIFACT_METHOD,
    ]

    assert raised.value.method_id == DERIVE_ARTIFACT_METHOD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("type_code", "representation", "method_name"),
    [
        (_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW, "audio", "download_audio"),
        (_PROTO.ARTIFACT_TYPE_EXPLAINER_VIDEO, "video", "download_video"),
    ],
)
async def test_media_download_prefers_exact_download_representation(
    type_code: int,
    representation: str,
    method_name: str,
) -> None:
    raw = _artifact("media", type_code=type_code)
    media_urls = (
        raw.audio_overview.media_urls
        if type_code == _PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW
        else raw.explainer_video.media_urls
    )
    media_urls.add(url="https://lh3.googleusercontent.com/adaptive", type=2)
    media_urls.add(url="https://lh3.googleusercontent.com/download", type=4)
    session, _, _, assets, api = _graph([raw])

    result = await getattr(api, method_name)("notebook-1", "media.mp4")

    assert result == "media.mp4"
    assert [call[0] for call in session.calls] == [LIST_ARTIFACTS_METHOD]
    assert assets.representation_calls == [
        ("https://lh3.googleusercontent.com/download", "media.mp4", representation)
    ]


@pytest.mark.asyncio
async def test_media_download_rejects_adaptive_playlist_before_asset_io() -> None:
    raw = _artifact("audio", type_code=_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW)
    raw.audio_overview.media_urls.add(
        url="https://lh3.googleusercontent.com/adaptive",
        type=_PROTO.MEDIA_STREAMING_TYPE_ADAPTIVE_STREAMING_HLS,
    )
    _, _, _, assets, api = _graph([raw])

    with pytest.raises(ArtifactParseError, match="downloadable media URL"):
        await api.download_audio("notebook-1", "audio.mp4")

    assert assets.representation_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_format", "expected_url", "representation"),
    [
        ("pdf", "https://lh3.googleusercontent.com/slides.pdf", "slide_pdf"),
        ("pptx", "https://lh3.googleusercontent.com/slides.pptx", "slide_pptx"),
    ],
)
async def test_slide_download_reads_exact_get_artifact_representation(
    output_format: str,
    expected_url: str,
    representation: str,
) -> None:
    raw = _artifact("slides", type_code=_PROTO.ARTIFACT_TYPE_SLIDES)
    raw.slides.pdf_download_url = "https://lh3.googleusercontent.com/slides.pdf"
    raw.slides.pptx_download_url = "https://lh3.googleusercontent.com/slides.pptx"
    session, _, _, assets, api = _graph([raw])

    result = await api.download_slide_deck(
        "notebook-1",
        f"slides.{output_format}",
        "slides",
        output_format,
    )

    assert result == f"slides.{output_format}"
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        LIST_ARTIFACTS_METHOD,
        GET_ARTIFACT_METHOD,
    ]
    assert assets.representation_calls == [
        (expected_url, f"slides.{output_format}", representation)
    ]


@pytest.mark.asyncio
async def test_download_data_table_decodes_tailwind_doc_as_bom_csv(tmp_path) -> None:
    raw = _artifact("table", type_code=_PROTO.ARTIFACT_TYPE_TABLE)
    projection = wire_pb2.WireArtifactTableProjection()
    table = projection.table.document.body.content.add().table
    for values in (("Name", "Evidence"), ("Alpha", "quoted, value"), ("Beta", "line\nbreak")):
        row = table.table_rows.add()
        for value in values:
            row.table_cells.add().content.add().paragraph.elements.add().text_run.content = value
    raw.MergeFromString(projection.SerializeToString())
    session, _, _, _, api = _graph([raw])
    output = tmp_path / "table.csv"

    result = await api.download_data_table("notebook-1", str(output), "table")

    assert result == str(output)
    assert output.read_bytes() == (
        b'\xef\xbb\xbfName,Evidence\r\nAlpha,"quoted, value"\r\nBeta,"line\nbreak"\r\n'
    )
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        LIST_ARTIFACTS_METHOD,
        GET_ARTIFACT_METHOD,
    ]


@pytest.mark.asyncio
async def test_download_data_table_preserves_code_blocks(tmp_path) -> None:
    raw = _artifact("table", type_code=_PROTO.ARTIFACT_TYPE_TABLE)
    projection = wire_pb2.WireArtifactTableProjection()
    table = projection.table.document.body.content.add().table
    header = table.table_rows.add()
    header.table_cells.add().content.add().paragraph.elements.add().text_run.content = "Snippet"
    data = table.table_rows.add()
    data.table_cells.add().content.add().code_block.content = "x = 1\ny = 2"
    raw.MergeFromString(projection.SerializeToString())
    _, _, _, _, api = _graph([raw])
    output = tmp_path / "table.csv"

    await api.download_data_table("notebook-1", str(output), "table")

    assert output.read_bytes() == b'\xef\xbb\xbfSnippet\r\n"x = 1\ny = 2"\r\n'


@pytest.mark.asyncio
async def test_download_data_table_rejects_multiple_tables_without_output(tmp_path) -> None:
    raw = _artifact("table", type_code=_PROTO.ARTIFACT_TYPE_TABLE)
    projection = wire_pb2.WireArtifactTableProjection()
    for _ in range(2):
        table = projection.table.document.body.content.add().table
        table.table_rows.add().table_cells.add().content.add().paragraph.elements.add().text_run.content = "Header"
    raw.MergeFromString(projection.SerializeToString())
    _, _, _, _, api = _graph([raw])
    output = tmp_path / "table.csv"

    with pytest.raises(ArtifactParseError, match="multiple top-level tables"):
        await api.download_data_table("notebook-1", str(output), "table")

    assert not output.exists()


@pytest.mark.asyncio
async def test_download_data_table_rejects_mixed_cell_variants_without_output(tmp_path) -> None:
    raw = _artifact("table", type_code=_PROTO.ARTIFACT_TYPE_TABLE)
    projection = wire_pb2.WireArtifactTableProjection()
    table = projection.table.document.body.content.add().table
    cell_block = table.table_rows.add().table_cells.add().content.add()
    cell_block.paragraph.elements.add().text_run.content = "Header"
    cell_block.image.url = "https://example.invalid/must-not-be-dropped"
    raw.MergeFromString(projection.SerializeToString())
    _, _, _, _, api = _graph([raw])
    output = tmp_path / "table.csv"

    with pytest.raises(ArtifactParseError, match="unsupported cell structure"):
        await api.download_data_table("notebook-1", str(output), "table")

    assert not output.exists()


@pytest.mark.asyncio
async def test_download_data_table_rejects_missing_document_without_output(tmp_path) -> None:
    raw = _artifact("table", type_code=_PROTO.ARTIFACT_TYPE_TABLE)
    _, _, _, _, api = _graph([raw])
    output = tmp_path / "table.csv"

    with pytest.raises(ArtifactParseError, match="omitted its table document"):
        await api.download_data_table("notebook-1", str(output), "table")

    assert not output.exists()


@pytest.mark.asyncio
async def test_export_to_drive_supports_artifact_and_literal_content_targets() -> None:
    session, _, _, _, api = _graph([_artifact("report-1")])
    session.responses[EXPORT_TO_DRIVE_METHOD] = [
        _PROTO.ExportToDriveResponse(url="https://docs.google.com/document/d/one"),
        _PROTO.ExportToDriveResponse(url="https://docs.google.com/spreadsheets/d/two"),
    ]

    report_url = await api.export_report(
        "notebook-1",
        "report-1",
        "Report title",
        ExportType.DOCS,
    )
    content_url = await api.export(
        "notebook-1",
        title="Table title",
        export_type=ExportType.SHEETS,
        content="A,B\n1,2\n",
    )

    assert report_url.endswith("/one")
    assert content_url.endswith("/two")
    first = session.calls[1]
    assert first[0] == EXPORT_TO_DRIVE_METHOD
    assert first[1].WhichOneof("target") == "artifact_id"
    assert first[1].artifact_id == "report-1"
    assert first[1].title == "Report title"
    assert first[1].destination == ExportType.DOCS.value
    second = session.calls[2]
    assert second[1].WhichOneof("target") == "content"
    assert second[1].content == "A,B\n1,2\n"
    assert second[1].destination == ExportType.SHEETS.value
    assert session.calls[0][2]["replay_safe"] is True
    assert all(call[2]["replay_safe"] is False for call in session.calls[1:])
    assert session.scopes == ["artifacts.export", "artifacts.export"]


@pytest.mark.asyncio
async def test_export_data_table_forces_sheets_and_validates_target_before_io() -> None:
    session, _, _, _, api = _graph([_artifact("table-1")])
    session.responses[EXPORT_TO_DRIVE_METHOD] = _PROTO.ExportToDriveResponse(
        url="https://docs.google.com/spreadsheets/d/sheet"
    )

    assert (await api.export_data_table("notebook-1", "table-1")).endswith("/sheet")
    assert session.calls[1][1].destination == ExportType.SHEETS.value
    session.calls.clear()
    session.scopes.clear()

    with pytest.raises(ValidationError, match="exactly one"):
        await api.export("notebook-1")
    with pytest.raises(ValidationError, match="exactly one"):
        await api.export("notebook-1", "artifact-1", content="literal")
    with pytest.raises(ValidationError, match="title must be a string"):
        await api.export("notebook-1", "artifact-1", cast(Any, 42))
    with pytest.raises(ValidationError, match="export_type must be an ExportType"):
        await api.export("notebook-1", "artifact-1", export_type=cast(Any, 1))
    assert session.calls == []
    assert session.scopes == []


@pytest.mark.asyncio
async def test_export_to_drive_rejects_missing_or_non_https_response_url() -> None:
    session, _, _, _, api = _graph([_artifact("report-1")])
    session.responses[EXPORT_TO_DRIVE_METHOD] = _PROTO.ExportToDriveResponse(url="javascript:x")

    with pytest.raises(DecodingError, match="valid HTTPS URL") as raised:
        await api.export_report("notebook-1", "report-1")
    assert getattr(raised.value, "unconfirmed", False) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        NetworkError("export response lost"),
        RateLimitError("export throttled"),
        ServerError("export unavailable"),
    ],
)
async def test_export_to_drive_transport_loss_is_unconfirmed_and_sent_once(
    error: RPCError,
) -> None:
    session, _, _, _, api = _graph([_artifact("report-1")])
    session.errors[EXPORT_TO_DRIVE_METHOD] = error

    with pytest.raises(type(error)) as raised:
        await api.export_report("notebook-1", "report-1")

    assert raised.value is error
    assert getattr(raised.value, "unconfirmed", False) is True
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        EXPORT_TO_DRIVE_METHOD,
    ]


@pytest.mark.asyncio
async def test_export_to_drive_auth_rejection_is_not_marked_unconfirmed() -> None:
    session, _, _, _, api = _graph([_artifact("report-1")])
    error = AuthError("auth rejected", rpc_code=16)
    session.errors[EXPORT_TO_DRIVE_METHOD] = error

    with pytest.raises(AuthError) as raised:
        await api.export_report("notebook-1", "report-1")

    assert raised.value is error
    assert getattr(raised.value, "unconfirmed", False) is False


@pytest.mark.asyncio
async def test_download_report_decodes_live_apk_report_doc_and_writes_atomically(tmp_path) -> None:
    raw = _artifact("report", type_code=_PROTO.ARTIFACT_TYPE_TAILORED_REPORT)
    structural = raw.tailored_report.report_doc.body.content.add(start_index=0, end_index=12)
    run = structural.paragraph.elements.add(start_index=0, end_index=12)
    run.text_run.content = "Hello report"
    structural.paragraph.paragraph_style.named_style_type = 2
    bullet = raw.tailored_report.report_doc.body.content.add()
    bullet.paragraph.bullet_info.list_type = 1
    bullet.paragraph.bullet_info.nesting_level = 1
    bullet.paragraph.elements.add().text_run.content = "Key point"
    rule = raw.tailored_report.report_doc.body.content.add()
    rule.horizontal_rule.SetInParent()
    table = raw.tailored_report.report_doc.body.content.add().table
    for left, right in (("A", "B"), ("1", "2")):
        row = table.table_rows.add()
        row.table_cells.add().content.add().paragraph.elements.add().text_run.content = left
        row.table_cells.add().content.add().paragraph.elements.add().text_run.content = right
    session, _, _, _, api = _graph([raw])
    output = tmp_path / "report.md"

    result = await api.download_report("notebook-1", str(output), "report")

    assert result == str(output)
    assert output.read_text(encoding="utf-8") == (
        "# Hello report\n\n  - Key point\n\n---\n\n| A | B |\n| --- | --- |\n| 1 | 2 |"
    )
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        LIST_ARTIFACTS_METHOD,
        GET_ARTIFACT_METHOD,
    ]


@pytest.mark.asyncio
async def test_exact_download_rejects_foreign_prefetched_artifact_before_global_read(
    tmp_path,
) -> None:
    owned = _artifact("owned", type_code=_PROTO.ARTIFACT_TYPE_TAILORED_REPORT)
    foreign = _artifact("foreign", type_code=_PROTO.ARTIFACT_TYPE_TAILORED_REPORT)
    session, _, _, _, api = _graph([owned])

    with pytest.raises(ArtifactNotFoundError):
        await api.download_report(
            "notebook-1",
            str(tmp_path / "foreign.md"),
            "foreign",
            artifacts_data=[foreign],
        )

    assert [call[0] for call in session.calls] == [LIST_ARTIFACTS_METHOD]


@pytest.mark.asyncio
async def test_atomic_text_publication_settles_worker_without_publishing_after_cancellation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_started = threading.Event()
    write_release = threading.Event()

    def _blocking_fsync(_descriptor: int) -> None:
        write_started.set()
        assert write_release.wait(timeout=5)

    monkeypatch.setattr(artifact_outputs.os, "fsync", _blocking_fsync)
    output = tmp_path / "cancelled.md"
    task = asyncio.create_task(
        write_text_atomic(
            str(output),
            "secret payload",
            artifact_type="report",
            artifact_id="report-1",
        )
    )
    assert await asyncio.to_thread(write_started.wait, 5)

    task.cancel()
    await asyncio.sleep(0)
    try:
        assert not task.done()
        assert not output.exists()
    finally:
        write_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not output.exists()
    assert list(tmp_path.iterdir()) == []


def test_report_renderer_preserves_exact_non_sample_variants_and_citations() -> None:
    document = chat_pb2.TailwindDoc()
    paragraph = document.body.content.add().paragraph
    styled = paragraph.elements.add().text_run
    styled.content = "x+y"
    styled.text_style.underline = True
    styled.text_style.math = 1
    paragraph.elements.add().resource.id = "resource-1"
    document.body.content.add().a2ui_block.json = '{"type":"card"}'
    citation = document.objects.add(object_id={"id": "citation-1"}).citation
    citation.fragment.elements.add().paragraph.elements.add().text_run.content = "Quoted fact"
    citation.source_attribution.ingested_source.source.id = "source-1"

    rendered = report_doc_markdown(document)

    assert "$<u>x+y</u>$[resource: resource-1]" in rendered
    assert '```json\n{"type":"card"}\n```' in rendered
    assert "> **Citation citation-1 (source-1):** Quoted fact" in rendered


@pytest.mark.asyncio
async def test_download_quiz_formats_exact_apk_app_html(tmp_path) -> None:
    raw = _artifact(
        "quiz",
        title="Quiz title",
        type_code=_PROTO.ARTIFACT_TYPE_APP,
        variant=_PROTO.APP_TYPE_QUIZ,
    )
    raw.app.app_html = (
        '<main data-app-data="{&quot;quiz&quot;:[{&quot;question&quot;:&quot;Q?&quot;,'
        "&quot;answerOptions&quot;:[{&quot;text&quot;:&quot;A&quot;,"
        '&quot;isCorrect&quot;:true}]}]}"></main>'
    )
    session, _, _, _, api = _graph([raw])
    output = tmp_path / "quiz.json"

    result = await api.download_quiz("notebook-1", str(output), "quiz")

    assert result == str(output)
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "title": "Quiz title",
        "questions": [
            {
                "question": "Q?",
                "answerOptions": [{"text": "A", "isCorrect": True}],
            }
        ],
    }
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        LIST_ARTIFACTS_METHOD,
        GET_ARTIFACT_METHOD,
    ]


@pytest.mark.asyncio
async def test_download_quiz_html_does_not_require_embedded_app_data(tmp_path) -> None:
    raw = _artifact(
        "quiz-html",
        title="Quiz title",
        type_code=_PROTO.ARTIFACT_TYPE_APP,
        variant=_PROTO.APP_TYPE_QUIZ,
    )
    raw.app.app_html = "<main>standalone exact app HTML</main>"
    _, _, _, _, api = _graph([raw])
    output = tmp_path / "quiz.html"

    result = await api.download_quiz(
        "notebook-1",
        str(output),
        "quiz-html",
        output_format="html",
    )

    assert result == str(output)
    assert output.read_text(encoding="utf-8") == raw.app.app_html


@pytest.mark.asyncio
async def test_download_flashcards_formats_exact_templatized_app_data(tmp_path) -> None:
    raw = _artifact(
        "cards",
        title="Cards",
        type_code=_PROTO.ARTIFACT_TYPE_APP,
        variant=_PROTO.APP_TYPE_FLASHCARDS,
    )
    raw.app.templatized_app.app_data = json.dumps({"flashcards": [{"f": "front", "b": "back"}]})
    _, _, _, _, api = _graph([raw])
    output = tmp_path / "cards.md"

    await api.download_flashcards(
        "notebook-1",
        str(output),
        "cards",
        output_format="markdown",
    )

    assert "**Q:** front" in output.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_media_download_accepts_owned_android_protobuf_prefetch() -> None:
    raw = _artifact("audio", type_code=_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW)
    raw.audio_overview.media_urls.add(
        url="https://lh3.googleusercontent.com/download",
        type=_PROTO.MEDIA_STREAMING_TYPE_DOWNLOAD,
    )
    session, _, _, assets, api = _graph([raw])

    await api.download_audio(
        "notebook-1",
        "audio.mp4",
        "audio",
        artifacts_data=[raw],
    )

    assert [call[0] for call in session.calls] == [LIST_ARTIFACTS_METHOD]
    assert assets.representation_calls == [
        ("https://lh3.googleusercontent.com/download", "audio.mp4", "audio")
    ]


@pytest.mark.asyncio
async def test_interactive_mind_map_generation_uses_live_exact_app_variant() -> None:
    session, notebooks, _, _, api = _graph()
    session.responses[CREATE_ARTIFACT_METHOD] = _PROTO.CreateArtifactResponse(
        artifact=_artifact(
            "interactive",
            type_code=_PROTO.ARTIFACT_TYPE_APP,
            status=_PROTO.ARTIFACT_STATUS_INITIALIZED,
            variant=_PROTO.APP_TYPE_MINDMAP,
        )
    )

    result = await api._generate_interactive_mind_map(
        "notebook-1",
        None,
        language="en",
        instructions="focus on causality",
    )

    assert result.task_id == "interactive"
    assert notebooks.calls == ["notebook-1"]
    method, request, kwargs = session.calls[0]
    assert method == CREATE_ARTIFACT_METHOD
    assert request.artifact.type == _PROTO.ARTIFACT_TYPE_APP
    assert request.artifact.app.generation_options.app_type == _PROTO.APP_TYPE_MINDMAP
    assert request.artifact.app.generation_options.language_code == "en"
    assert request.artifact.app.generation_options.free_text_steering_prompt == (
        "focus on causality"
    )
    assert kwargs["replay_safe"] is False


@pytest.mark.asyncio
async def test_interactive_mind_map_tree_decodes_live_direct_json_field() -> None:
    raw = _artifact(
        "interactive",
        type_code=_PROTO.ARTIFACT_TYPE_APP,
        variant=_PROTO.APP_TYPE_MINDMAP,
    )
    raw.app.mind_map_json = json.dumps({"name": "Root", "children": [{"name": "Leaf"}]})
    session, _, _, _, api = _graph([raw])

    tree = await api._get_interactive_mind_map_tree("notebook-1", "interactive")

    assert tree == {"name": "Root", "children": [{"name": "Leaf"}]}
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        GET_ARTIFACT_METHOD,
    ]


@pytest.mark.asyncio
async def test_exact_interactive_tree_rejects_artifact_outside_notebook() -> None:
    session, _, _, _, api = _graph([_artifact("owned-by-notebook")])

    with pytest.raises(ArtifactNotFoundError):
        await api._get_interactive_mind_map_tree("notebook-1", "foreign-artifact")

    assert [call[0] for call in session.calls] == [LIST_ARTIFACTS_METHOD]


@pytest.mark.asyncio
async def test_download_interactive_mind_map_writes_validated_json(tmp_path) -> None:
    raw = _artifact(
        "interactive",
        type_code=_PROTO.ARTIFACT_TYPE_APP,
        variant=_PROTO.APP_TYPE_MINDMAP,
    )
    raw.app.mind_map_json = json.dumps({"name": "Root", "children": [{"name": "Leaf"}]})
    session, _, _, _, api = _graph([raw])
    output = tmp_path / "mind-map.json"

    result = await api.download_mind_map("notebook-1", str(output), "interactive")

    assert result == str(output)
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "name": "Root",
        "children": [{"name": "Leaf"}],
    }
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        LIST_ARTIFACTS_METHOD,
        GET_ARTIFACT_METHOD,
    ]


@pytest.mark.asyncio
async def test_download_note_backed_mind_map_uses_typed_prefetch_without_rpc(tmp_path) -> None:
    session, _, _, _, api = _graph()
    output = tmp_path / "note-map.json"
    note_map = MindMap(
        id="note-map",
        notebook_id="notebook-1",
        title="Note map",
        kind=MindMapKind.NOTE_BACKED,
        tree={"name": "Root", "children": []},
    )

    result = await api.download_mind_map(
        "notebook-1",
        str(output),
        "note-map",
        mind_maps=[note_map],
        artifacts_data=[],
    )

    assert result == str(output)
    assert json.loads(output.read_text(encoding="utf-8")) == note_map.tree
    assert session.calls == []


@pytest.mark.asyncio
async def test_download_note_backed_mind_map_self_fetches_without_prefetch(tmp_path) -> None:
    session, _, mind_maps, _, api = _graph()
    output = tmp_path / "note-map.json"
    note_map = MindMap(
        id="note-map",
        notebook_id="notebook-1",
        title="Note map",
        kind=MindMapKind.NOTE_BACKED,
        tree={"name": "Root", "children": []},
    )
    mind_maps.mind_maps = [note_map]

    result = await api.download_mind_map("notebook-1", str(output), "note-map")

    assert result == str(output)
    assert json.loads(output.read_text(encoding="utf-8")) == note_map.tree
    assert mind_maps.calls == ["notebook-1"]
    assert session.calls == []


@pytest.mark.asyncio
async def test_download_note_backed_mind_map_holds_outer_scope_through_publication(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = SupervisedAndroidTransport()
    write_started = asyncio.Event()
    write_release = asyncio.Event()
    output = tmp_path / "note-map.json"
    note_map = MindMap(
        id="note-map",
        notebook_id="notebook-1",
        title="Note map",
        kind=MindMapKind.NOTE_BACKED,
        tree={"name": "Root", "children": []},
    )

    async def _blocked_write(output_path: str, *_args: Any, **_kwargs: Any) -> str:
        write_started.set()
        await write_release.wait()
        return output_path

    monkeypatch.setattr(android_artifacts, "write_text_atomic", _blocked_write)
    task = asyncio.create_task(
        _supervised_graph(transport).download_mind_map(
            "notebook-1",
            str(output),
            "note-map",
            mind_maps=[note_map],
            artifacts_data=[],
        )
    )
    await write_started.wait()

    await transport.supervisor.stop_accepting(1)
    idle = asyncio.create_task(transport.supervisor.wait_for_idle(1, 1.0))
    await asyncio.sleep(0)
    assert not idle.done()
    write_release.set()

    assert await task == str(output)
    await idle


@pytest.mark.asyncio
async def test_interactive_mind_map_rejects_malformed_node_tree() -> None:
    raw = _artifact(
        "interactive",
        type_code=_PROTO.ARTIFACT_TYPE_APP,
        variant=_PROTO.APP_TYPE_MINDMAP,
    )
    raw.app.mind_map_json = '{"name":"Root","children":[{"name":7}]}'
    _, _, _, _, api = _graph([raw])

    with pytest.raises(ArtifactParseError, match="invalid node"):
        await api._get_interactive_mind_map_tree("notebook-1", "interactive")


@pytest.mark.asyncio
async def test_generate_note_backed_mind_map_uses_native_action_and_note_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, notebooks, _, _, api = _graph()
    tree_json = '{"name":"Topics","children":[]}'
    session.responses[ACT_ON_SOURCES_METHOD] = chat_pb2.ActOnSourcesResponse(
        response=chat_pb2.AnswerResponse(response=tree_json)
    )
    create = AsyncMock(
        return_value=Note(
            id="note-1",
            notebook_id="notebook-1",
            title="Topics",
            content=tree_json,
        )
    )
    monkeypatch.setattr(android_notes, "create_note", create)

    result = await api.generate_mind_map(
        "notebook-1",
        ["source-1"],
        "fr",
        "Group by theme",
    )

    assert result.mind_map == {"name": "Topics", "children": []}
    assert result.note_id == "note-1"
    create.assert_awaited_once_with(
        session,
        "notebook-1",
        title="Topics",
        content=tree_json,
        expected_epoch=7,
    )
    assert len(session.calls) == 1
    method, request, kwargs = session.calls[0]
    assert method == ACT_ON_SOURCES_METHOD
    assert [source.source_id.id for source in request.sources] == ["source-1"]
    assert request.mind_map_action.action == "interactive_mindmap"
    assert request.mind_map_action.language == "fr"
    assert [(item.key, item.value) for item in request.mind_map_action.context] == [
        ("[CONTEXT]", "Group by theme")
    ]
    assert request.request_context.client_type == 3
    assert kwargs["replay_safe"] is False
    assert kwargs["response_type"] is chat_pb2.ActOnSourcesResponse
    assert kwargs["expected_epoch"] == 7
    assert notebooks.calls == []


@pytest.mark.asyncio
async def test_generate_note_backed_mind_map_resolves_default_sources_natively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, notebooks, _, _, api = _graph()
    session.responses[ACT_ON_SOURCES_METHOD] = chat_pb2.ActOnSourcesResponse(
        response=chat_pb2.AnswerResponse(response='{"name":"Root","children":[]}')
    )
    create = AsyncMock(
        return_value=Note(
            id="note-1",
            notebook_id="notebook-1",
            title="Root",
            content='{"name":"Root","children":[]}',
        )
    )
    monkeypatch.setattr(android_notes, "create_note", create)

    await api.generate_mind_map("notebook-1")

    assert notebooks.calls == ["notebook-1"]
    request = session.calls[0][1]
    assert [source.source_id.id for source in request.sources] == ["source-1", "source-2"]


@pytest.mark.asyncio
async def test_generate_note_backed_mind_map_validates_instructions_before_io() -> None:
    session, notebooks, _, _, api = _graph()

    with pytest.raises(ValidationError, match="instructions must be a string or None"):
        await api.generate_mind_map("notebook-1", instructions=cast(Any, 7))

    assert notebooks.calls == []
    assert session.scopes == []
    assert session.calls == []


@pytest.mark.asyncio
async def test_generate_note_backed_mind_map_empty_response_does_not_create_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, _, _, api = _graph()
    session.responses[ACT_ON_SOURCES_METHOD] = chat_pb2.ActOnSourcesResponse()
    create = AsyncMock()
    monkeypatch.setattr(android_notes, "create_note", create)

    result = await api.generate_mind_map("notebook-1", ["source-1"])

    assert result.mind_map is None
    assert result.note_id is None
    assert result.created_at is None
    create.assert_not_awaited()
    assert [method for method, _request, _kwargs in session.calls] == [ACT_ON_SOURCES_METHOD]


@pytest.mark.asyncio
async def test_generate_note_backed_mind_map_preserves_non_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, _, _, api = _graph()
    session.responses[ACT_ON_SOURCES_METHOD] = chat_pb2.ActOnSourcesResponse(
        response=chat_pb2.AnswerResponse(response="unstructured tree")
    )
    create = AsyncMock(
        return_value=Note(
            id="note-1",
            notebook_id="notebook-1",
            title="Mind Map",
            content="unstructured tree",
        )
    )
    monkeypatch.setattr(android_notes, "create_note", create)

    result = await api.generate_mind_map("notebook-1", ["source-1"])

    assert result.mind_map == "unstructured tree"
    assert result.note_id == "note-1"
    create.assert_awaited_once_with(
        session,
        "notebook-1",
        title="Mind Map",
        content="unstructured tree",
        expected_epoch=7,
    )


@pytest.mark.asyncio
async def test_generate_note_backed_mind_map_generation_failure_never_writes_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, _, _, api = _graph()
    original = RPCError("generation unavailable", method_id=ACT_ON_SOURCES_METHOD, rpc_code=13)
    session.errors[ACT_ON_SOURCES_METHOD] = original
    create = AsyncMock()
    monkeypatch.setattr(android_notes, "create_note", create)

    with pytest.raises(RPCError) as raised:
        await api.generate_mind_map("notebook-1", ["source-1"])

    assert raised.value is original
    create.assert_not_awaited()
    assert [method for method, _request, _kwargs in session.calls] == [ACT_ON_SOURCES_METHOD]


@pytest.mark.asyncio
async def test_generate_note_backed_mind_map_note_failure_does_not_repeat_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, _, _, api = _graph()
    session.responses[ACT_ON_SOURCES_METHOD] = chat_pb2.ActOnSourcesResponse(
        response=chat_pb2.AnswerResponse(response='{"children":[]}')
    )
    original = RPCError("note unavailable", method_id="CreateNote", rpc_code=13)
    create = AsyncMock(side_effect=original)
    monkeypatch.setattr(android_notes, "create_note", create)

    with pytest.raises(RPCError) as raised:
        await api.generate_mind_map("notebook-1", ["source-1"])

    assert raised.value is original
    create.assert_awaited_once()
    assert [method for method, _request, _kwargs in session.calls] == [ACT_ON_SOURCES_METHOD]


@pytest.mark.asyncio
async def test_infographic_prefetch_requires_notebook_ownership_proof() -> None:
    raw = _artifact("image", url="https://lh3.googleusercontent.com/image?cap=1")
    session, _, _, assets, api = _graph([raw])

    result = await api.download_infographic(
        "notebook-1",
        "out.png",
        artifact_id="image",
        artifacts_data=[raw],
    )

    assert result == "out.png"
    assert [call[0] for call in session.calls] == [LIST_ARTIFACTS_METHOD]
    assert assets.calls == [(raw.infographic.infographics[0].image.url, "out.png")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "type_code"),
    [
        ("download_audio", _PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW),
        ("download_video", _PROTO.ARTIFACT_TYPE_EXPLAINER_VIDEO),
        ("download_infographic", _PROTO.ARTIFACT_TYPE_INFOGRAPHIC),
    ],
)
async def test_media_prefetch_rejects_artifact_outside_notebook_before_asset_io(
    method_name: str,
    type_code: int,
) -> None:
    owned = _artifact("owned", type_code=type_code)
    foreign = _artifact(
        "foreign",
        type_code=type_code,
        url=(
            "https://lh3.googleusercontent.com/foreign?cap=1"
            if type_code == _PROTO.ARTIFACT_TYPE_INFOGRAPHIC
            else None
        ),
    )
    if type_code == _PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW:
        foreign.audio_overview.media_urls.add(
            url="https://lh3.googleusercontent.com/foreign-audio",
            type=_PROTO.MEDIA_STREAMING_TYPE_DOWNLOAD,
        )
    elif type_code == _PROTO.ARTIFACT_TYPE_EXPLAINER_VIDEO:
        foreign.explainer_video.media_urls.add(
            url="https://lh3.googleusercontent.com/foreign-video",
            type=_PROTO.MEDIA_STREAMING_TYPE_DOWNLOAD,
        )
    session, _, _, assets, api = _graph([owned])

    with pytest.raises(ArtifactNotFoundError):
        await getattr(api, method_name)(
            "notebook-1",
            "out.bin",
            "foreign",
            artifacts_data=[foreign],
        )

    assert [call[0] for call in session.calls] == [LIST_ARTIFACTS_METHOD]
    assert assets.calls == []
    assert assets.representation_calls == []


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
async def test_infographic_preserves_auth_error_type_without_cause() -> None:
    secret_url = "https://lh3.googleusercontent.com/object?secret=capability"
    _, _, _, assets, api = _graph([_artifact("image-1", url=secret_url)])
    assets.error = AuthError("authentication expired")

    with pytest.raises(AuthError) as raised:
        await api.download_infographic("notebook-1", "out.png")

    assert raised.value is assets.error
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_media_representation_preserves_auth_error_type_without_cause() -> None:
    raw = _artifact("audio", type_code=_PROTO.ARTIFACT_TYPE_AUDIO_OVERVIEW)
    raw.audio_overview.media_urls.add(
        url="https://lh3.googleusercontent.com/audio?secret=capability",
        type=_PROTO.MEDIA_STREAMING_TYPE_DOWNLOAD,
    )
    _, _, _, assets, api = _graph([raw])
    assets.error = AuthError("authentication expired")

    with pytest.raises(AuthError) as raised:
        await api.download_audio("notebook-1", "audio.mp4")

    assert raised.value is assets.error
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_delete_preflights_ownership_and_is_idempotent_after_that_proof() -> None:
    session, _, _, _, api = _graph(
        [
            _artifact("artifact-1"),
            _artifact("missing"),
            _artifact("denied"),
        ]
    )
    await api.delete("notebook-1", "artifact-1")
    assert [call[0] for call in session.calls] == [
        LIST_ARTIFACTS_METHOD,
        DELETE_ARTIFACT_METHOD,
    ]
    list_method, list_request, list_kwargs = session.calls[0]
    assert list_method == LIST_ARTIFACTS_METHOD
    assert list_request == _PROTO.ListArtifactsRequest(project_id="notebook-1")
    assert list_kwargs == {
        "replay_safe": True,
        "response_type": _PROTO.ListArtifactsResponse,
        "expected_epoch": 7,
    }
    method, request, kwargs = session.calls[1]
    assert method == DELETE_ARTIFACT_METHOD
    assert request == _PROTO.DeleteArtifactRequest(artifact_id="artifact-1")
    assert kwargs == {
        "replay_safe": False,
        "response_type": empty_pb2.Empty,
        "expected_epoch": 7,
    }
    assert session.scopes == ["artifacts.delete"]

    session.errors[DELETE_ARTIFACT_METHOD] = RPCError("missing", rpc_code=5)
    await api.delete("notebook-1", "missing")
    original = RPCError("denied", rpc_code=7)
    session.errors[DELETE_ARTIFACT_METHOD] = original
    with pytest.raises(RPCError) as raised:
        await api.delete("notebook-1", "denied")
    assert raised.value is original


@pytest.mark.asyncio
async def test_delete_ignores_artifact_outside_requested_notebook_without_mutation() -> None:
    session, _, _, _, api = _graph([_artifact("artifact-in-notebook-1")])

    result = await api.delete("notebook-1", "artifact-from-another-notebook")

    assert result is None
    assert [call[0] for call in session.calls] == [LIST_ARTIFACTS_METHOD]
    assert session.calls[0][1] == _PROTO.ListArtifactsRequest(project_id="notebook-1")
    assert session.scopes == ["artifacts.delete"]


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


class _PausedSupervisedNotebookSources:
    def __init__(self, transport: SupervisedAndroidTransport) -> None:
        self._transport = transport
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        self.started.set()
        await self.release.wait()
        return await self._transport.unary(
            "notebooks.get_source_ids",
            notebook_id,
            replay_safe=True,
            response_type=list,
        )


@pytest.mark.asyncio
async def test_quiz_nested_source_read_rejects_a_retired_workflow_epoch() -> None:
    transport = SupervisedAndroidTransport()
    notebooks = _PausedSupervisedNotebookSources(transport)
    transport.handlers["notebooks.get_source_ids"] = ["source-1"]
    api = _supervised_graph(transport, notebooks=notebooks)
    task = asyncio.create_task(api.generate_quiz("notebook-1"))
    await notebooks.started.wait()

    old_generation = await transport.force_close_and_reopen()
    notebooks.release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await task
    assert transport.calls == []
    assert old_generation.in_flight == 0


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
    assert "expected_epoch" not in transport.calls[1][2]


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
    assert "expected_epoch" not in transport.calls[1][2]
    await transport.supervisor.wait_for_idle(1, 0.1)


@pytest.mark.asyncio
async def test_note_mind_map_source_resolution_and_mutations_finish_during_graceful_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = SupervisedAndroidTransport()
    sources_started = asyncio.Event()
    sources_release = asyncio.Event()

    async def _sources(_request: Any, _kwargs: dict[str, Any]) -> Any:
        sources_started.set()
        await sources_release.wait()
        return ["source-1"]

    transport.handlers["notebooks.get_source_ids"] = _sources
    transport.handlers[ACT_ON_SOURCES_METHOD] = chat_pb2.ActOnSourcesResponse(
        response=chat_pb2.AnswerResponse(response='{"name":"Root","children":[]}')
    )
    create = AsyncMock(
        return_value=Note(
            id="note-1",
            notebook_id="notebook-1",
            title="Root",
            content='{"name":"Root","children":[]}',
        )
    )
    monkeypatch.setattr(android_notes, "create_note", create)
    api = _supervised_graph(transport, notebooks=_SupervisedNotebookSources(transport))
    task = asyncio.create_task(api.generate_mind_map("notebook-1"))
    await sources_started.wait()

    await transport.supervisor.stop_accepting(1)
    sources_release.set()

    assert (await task).note_id == "note-1"
    assert [method for method, _request, _kwargs in transport.calls] == [
        "notebooks.get_source_ids",
        ACT_ON_SOURCES_METHOD,
    ]
    assert transport.calls[1][2]["expected_epoch"] == 1
    create.assert_awaited_once_with(
        transport,
        "notebook-1",
        title="Root",
        content='{"name":"Root","children":[]}',
        expected_epoch=1,
    )
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


# ===========================================================================
# Input admission before any RPC is dispatched
# ===========================================================================


@pytest.mark.asyncio
async def test_audio_generation_requires_at_least_one_source() -> None:
    session, _nb, _mm, _assets, api = _graph()

    with pytest.raises(ValidationError, match="at least one source id"):
        await api.generate_audio("notebook-1", source_ids=[])

    assert session.calls == []


@pytest.mark.asyncio
async def test_audio_instructions_must_be_text() -> None:
    session, _nb, _mm, _assets, api = _graph()

    with pytest.raises(ValidationError, match="instructions must be a string or None"):
        await api.generate_audio("notebook-1", source_ids=["s1"], instructions=7)

    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "label"),
    [
        pytest.param("generate_video", "Video", id="video"),
        pytest.param("generate_report", "Report", id="report"),
    ],
)
async def test_a_supported_family_requires_at_least_one_source(method: str, label: str) -> None:
    """An explicitly empty list is a caller error; ``None`` means "all sources"."""
    session, _nb, _mm, _assets, api = _graph()

    with pytest.raises(ValidationError, match=f"{label} generation requires at least one"):
        await getattr(api, method)("notebook-1", source_ids=[])

    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output_format",
    [pytest.param("docx", id="unsupported"), pytest.param("PDF", id="wrong-case")],
)
async def test_a_slide_deck_download_rejects_an_unknown_format(output_format: str) -> None:
    session, _nb, _mm, _assets, api = _graph()

    with pytest.raises(ValidationError, match="Must be 'pdf' or 'pptx'"):
        await api.download_slide_deck("notebook-1", "deck.out", "slides", output_format)

    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        pytest.param("download_quiz", id="quiz"),
        pytest.param("download_flashcards", id="flashcards"),
    ],
)
async def test_an_interactive_app_download_rejects_an_unknown_output_format(
    method: str,
) -> None:
    session, _nb, _mm, _assets, api = _graph()

    with pytest.raises(ValidationError, match="Use one of: json, markdown, html"):
        await getattr(api, method)("notebook-1", "out.txt", "app-1", "pdf")

    assert session.calls == []
