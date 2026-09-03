"""Tests for the test-only Android gRPC cassette channel seam."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from google.protobuf.any_pb2 import Any as AnyMessage
from google.protobuf.empty_pb2 import Empty
from google.protobuf.timestamp_pb2 import Timestamp
from tests._helpers.android_grpc_cassette import (
    AndroidGrpcCassette,
    AndroidGrpcCassetteError,
    AndroidGrpcCassetteMismatch,
    ProtoRedactor,
    RecordingGrpcModule,
    ReplayBearer,
    ReplayGrpcModule,
)
from tests.cassette_patterns import _CREDENTIAL_DETECTORS

from notebooklm._android.auth import BearerCredential
from notebooklm._android.phenotype import CLIENT_TYPE_HEADER, EXPERIMENT_TOKEN_HEADER
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    account_pb2,
    artifacts_pb2,
    chat_pb2,
    notes_pb2,
    organization_pb2,
    read_pb2,
    research_pb2,
    sources_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    notebooks_pb2 as orchestration_notebooks_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.sharing import sharing_pb2
from notebooklm._android.proto.notebooklm.android.wire.v1 import (
    organization_mutations_pb2,
)
from notebooklm._android.proto.notebooklm.android.wire.v1 import (
    sharing_pb2 as wire_sharing_pb2,
)
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import (
    notebooks_pb2,
    source_content_pb2,
)
from notebooklm._android.session import ANDROID_GRPC_TARGET, AndroidSession
from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor

METHOD = (
    "/google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/GetProject"
)
RAW_PROJECT_ID = "6a34c56e-ef9f-48db-ad21-d6ce449c108d"
RAW_SOURCE_ID = "63bb19ec-3f16-445e-aa91-6cc070f0ff85"
SAFE_PROJECT_ID = "00000000-0000-4000-8000-000000000001"
SENSITIVE_TITLE = "private-project-title-must-not-persist"
SENSITIVE_URL = "https://private.example.test/account/document"
SENSITIVE_BEARER = "bearer-sensitive-value-must-not-persist"
ANDROID_CASSETTES = Path(__file__).resolve().parents[2] / "cassettes" / "android"
# Every protobuf type a committed cassette may carry. Registering a type here
# is a review step: the canonicality test decodes each payload through it and
# proves the mandatory redactor is already a no-op, so nothing escapes as
# opaque base64.
_CASSETTE_MESSAGE_TYPES = (
    # 1. settings via unary GetOrCreateAccount
    account_pb2.GetOrCreateAccountRequest,
    account_pb2.GetOrCreateAccountResponse,
    # 2. GetProject (notebooks.get decodes the wire projection; sources.list the raw response)
    read_pb2.GetProjectRequest,
    read_pb2.GetProjectResponse,
    notebooks_pb2.WireGetProjectResponse,
    # 3. LoadSource structured content
    sources_pb2.LoadSourceRequest,
    source_content_pb2.WireLoadSourceResponse,
    # 4. ListArtifacts plus GetNotes
    artifacts_pb2.ListArtifactsRequest,
    artifacts_pb2.ListArtifactsResponse,
    notes_pb2.GetNotesRequest,
    notes_pb2.GetNotesResponse,
    # 5. GetLabels (notebook labels and account collections)
    organization_pb2.GetLabelsRequest,
    organization_mutations_pb2.GetLabelsWireResponse,
    # 6. ListDiscoverSourcesJob research state
    research_pb2.ListDiscoverSourcesJobRequest,
    research_pb2.ListDiscoverSourcesJobResponse,
    # 7. sharing GetProjectDetails
    sharing_pb2.GetProjectDetailsRequest,
    wire_sharing_pb2.GetProjectDetailsResponse,
    # 8. ListChatSessions plus ListChatTurns
    chat_pb2.ListChatSessionsRequest,
    chat_pb2.ListChatSessionsResponse,
    chat_pb2.GetChatSessionStatusRequest,
    chat_pb2.GetChatSessionStatusResponse,
    chat_pb2.CancelGenerationRequest,
    chat_pb2.CancelGenerationResponse,
    chat_pb2.ListChatTurnsRequest,
    chat_pb2.ListChatTurnsResponse,
    # 9. server-streaming GenerateFreeFormStreamed
    chat_pb2.GenerateFreeFormStreamedRequest,
    chat_pb2.GenerateFreeFormStreamedResponse,
    # --- mutation families ---
    Empty,
    # notebooks: CreateProject, MutateProject, ListRecentlyViewedProjects, CopyProject,
    # DeleteProjects, GenerateNotebookGuide
    orchestration_notebooks_pb2.CreateProjectRequest,
    orchestration_notebooks_pb2.CopyProjectRequest,
    orchestration_notebooks_pb2.DeleteProjectsRequest,
    orchestration_notebooks_pb2.GenerateNotebookGuideRequest,
    notebooks_pb2.WireMutateProjectRequest,
    notebooks_pb2.WireGenerateNotebookGuideResponse,
    read_pb2.Project,
    read_pb2.ListRecentlyViewedProjectsRequest,
    read_pb2.ListRecentlyViewedProjectsResponse,
    # sources: AddTentativeSources, AddSources, MutateSource, GenerateDocumentGuides,
    # CheckSourceFreshness, DeleteSources
    sources_pb2.AddTentativeSourcesRequest,
    sources_pb2.AddTentativeSourcesResponse,
    sources_pb2.AddSourcesRequest,
    sources_pb2.AddSourcesResponse,
    sources_pb2.ListExpertIntelligenceContentRequest,
    sources_pb2.ListExpertIntelligenceContentResponse,
    sources_pb2.MutateSourceRequest,
    sources_pb2.MutateSourceResponse,
    sources_pb2.GenerateDocumentGuidesRequest,
    sources_pb2.GenerateDocumentGuidesResponse,
    sources_pb2.CheckSourceFreshnessRequest,
    sources_pb2.CheckSourceFreshnessResponse,
    sources_pb2.DeleteSourcesRequest,
    sources_pb2.RetrieveRelevantChunksRequest,
    sources_pb2.RetrieveRelevantChunksResponse,
    # sources (#2283): AddSourcesAsync, AppendSource, CopySourcesAsync
    sources_pb2.AddSourcesAsyncResponse,
    sources_pb2.AppendSourceRequest,
    sources_pb2.CopySourcesAsyncRequest,
    sources_pb2.CopySourcesAsyncResponse,
    # notes: CreateNote, MutateNote, DeleteNotes
    notes_pb2.CreateNoteRequest,
    notes_pb2.CreateNoteResponse,
    notes_pb2.MutateNoteRequest,
    notes_pb2.MutateNoteResponse,
    notes_pb2.DeleteNotesRequest,
    notes_pb2.DeleteNotesResponse,
    # labels and collections: CreateLabel, MutateLabel, DeleteLabels
    organization_pb2.CreateLabelRequest,
    organization_pb2.CreateLabelResponse,
    organization_pb2.MutateLabelRequest,
    organization_pb2.MutateLabelResponse,
    organization_pb2.DeleteLabelsRequest,
    organization_pb2.DeleteLabelsResponse,
    # sharing: ShareProject
    sharing_pb2.ShareProjectRequest,
    sharing_pb2.ShareProjectResponse,
    # chat: DeleteChatTurns, ActOnSources (mind map)
    chat_pb2.DeleteChatTurnsRequest,
    chat_pb2.ActOnSourcesRequest,
    chat_pb2.ActOnSourcesResponse,
    # settings: MutateAccount
    account_pb2.MutateAccountRequest,
    account_pb2.Account,
    # artifacts: CreateArtifact, GetArtifact, UpdateArtifact, DeleteArtifact,
    # GenerateReportSuggestions
    artifacts_pb2.Artifact,
    artifacts_pb2.CreateArtifactRequest,
    artifacts_pb2.CreateArtifactResponse,
    artifacts_pb2.GetArtifactRequest,
    artifacts_pb2.GetArtifactResponse,
    artifacts_pb2.UpdateArtifactRequest,
    artifacts_pb2.DeleteArtifactRequest,
    artifacts_pb2.GenerateReportSuggestionsRequest,
    artifacts_pb2.GenerateReportSuggestionsResponse,
    # artifacts (#2283): CopyArtifactsAsync, GetArtifactCustomizationChoices
    artifacts_pb2.CopyArtifactsAsyncRequest,
    artifacts_pb2.CopyArtifactsAsyncResponse,
    artifacts_pb2.GetArtifactCustomizationChoicesRequest,
    artifacts_pb2.GetArtifactCustomizationChoicesResponse,
    # notebooks (#2283): NextStepSuggestions
    chat_pb2.NextStepSuggestionsRequest,
    orchestration_notebooks_pb2.NextStepSuggestions,
    # research: DiscoverSources, DiscoverSourcesManifold, CancelDiscoverSourcesJob,
    # FinishDiscoverSourcesRun
    research_pb2.DiscoverSourcesRequest,
    research_pb2.DiscoverSourcesResponse,
    research_pb2.DiscoverSourcesManifoldRequest,
    research_pb2.DiscoverSourcesManifoldResponse,
    research_pb2.CancelDiscoverSourcesJobRequest,
    research_pb2.FinishDiscoverSourcesRunRequest,
    research_pb2.FinishDiscoverSourcesRunResponse,
)
KNOWN_CASSETTE_PAYLOAD_TYPES = {
    message_type.DESCRIPTOR.full_name: message_type for message_type in _CASSETTE_MESSAGE_TYPES
}


@dataclass(frozen=True)
class _LeaseBearer:
    gets: list[int]

    async def activate_for_epoch(self, epoch: int) -> None:
        del epoch

    async def get(self, expected_epoch: int) -> BearerCredential:
        self.gets.append(expected_epoch)
        return BearerCredential(SENSITIVE_BEARER, 1)

    def invalidate(self, generation: int) -> None:
        del generation

    async def prepare_close(self) -> None:
        return None


class _LiveStream:
    def __init__(self, messages: list[Any]) -> None:
        self._messages = iter(messages)
        self.cancelled = False

    def __aiter__(self) -> _LiveStream:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._messages)
        except StopIteration:
            raise StopAsyncIteration from None

    def cancel(self) -> None:
        self.cancelled = True


class _LiveChannel:
    def __init__(self, response: read_pb2.GetProjectResponse) -> None:
        self.response = response
        self.invocations: list[tuple[str, bytes, Any]] = []
        self.closed = False

    def unary_unary(
        self, method: str, *, request_serializer: Any, response_deserializer: Any
    ) -> Any:
        async def invoke(request: Any, *, metadata: Any, timeout: float | None) -> Any:
            del timeout
            self.invocations.append((method, request_serializer(request), metadata))
            return response_deserializer(self.response.SerializeToString())

        return invoke

    def unary_stream(
        self, method: str, *, request_serializer: Any, response_deserializer: Any
    ) -> Any:
        def invoke(request: Any, *, metadata: Any, timeout: float | None) -> _LiveStream:
            del timeout
            self.invocations.append((method, request_serializer(request), metadata))
            return _LiveStream(
                [
                    response_deserializer(self.response.SerializeToString()),
                    response_deserializer(self.response.SerializeToString()),
                ]
            )

        return invoke

    async def close(self) -> None:
        self.closed = True


class _LiveGrpc:
    def __init__(self, channel: _LiveChannel) -> None:
        self.channel = channel
        self.secure_channel_calls = 0
        self.aio = SimpleNamespace(secure_channel=self.secure_channel)

    def ssl_channel_credentials(self) -> object:
        return object()

    def secure_channel(self, target: str, credentials: Any, *, options: Any = None) -> _LiveChannel:
        del target, credentials, options
        self.secure_channel_calls += 1
        return self.channel


def _response(project_id: str = RAW_PROJECT_ID) -> read_pb2.GetProjectResponse:
    return read_pb2.GetProjectResponse(
        project=read_pb2.Project(
            id=project_id,
            title=SENSITIVE_TITLE,
            sources=[
                read_pb2.Source(
                    source_id=read_pb2.SourceId(id=RAW_SOURCE_ID),
                    title="private-source-title",
                    metadata=read_pb2.SourceMetadata(
                        original_source_content_type=read_pb2.SOURCE_CONTENT_TYPE_URL,
                        webpage_metadata=read_pb2.WebpageMetadata(url=SENSITIVE_URL),
                    ),
                )
            ],
        )
    )


def _supervisor() -> CallSupervisor:
    return CallSupervisor(
        metrics=ClientMetrics(),
        max_concurrent_rpcs=None,
    )


def test_proto_redactor_preserves_numeric_wire_presence_and_is_idempotent() -> None:
    message = Timestamp(seconds=123, nanos=456)

    clean = ProtoRedactor()(METHOD, "response", message)
    clean_again = ProtoRedactor(trust_placeholders=True)(METHOD, "response", clean)

    assert clean == Timestamp(seconds=1, nanos=1)
    assert clean_again == clean
    assert clean.SerializeToString(deterministic=True)


async def _open_session(grpc_module: Any, bearer: Any) -> tuple[AndroidSession, CallSupervisor]:
    supervisor = _supervisor()
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    session = AndroidSession(
        bearer,
        supervisor,
        grpc_loader=lambda: grpc_module,
        protobuf_loader=lambda: object(),
    )
    session.set_bound_loop(loop)
    session.reset_after_open()
    await session.open(loop, 1)
    return session, supervisor


async def _close_session(session: AndroidSession, supervisor: CallSupervisor) -> None:
    await supervisor.begin_closing(1)
    await session.prepare_close()
    await session.close_resources()
    supervisor.mark_closed(1)


@pytest.mark.asyncio
async def test_recording_serializes_redacted_deterministic_unary_protobuf(tmp_path: Path) -> None:
    cassette_path = tmp_path / "get_project.grpc.json"
    live_channel = _LiveChannel(_response())
    live_grpc = _LiveGrpc(live_channel)
    recorder = RecordingGrpcModule(live_grpc, cassette_path, sanitizer=ProtoRedactor())
    bearer = _LeaseBearer([])
    session, supervisor = await _open_session(recorder, bearer)

    request = read_pb2.GetProjectRequest(
        project_id=RAW_PROJECT_ID,
        include_audio_overview_ids=True,
    )
    response = await session.unary(
        METHOD,
        request,
        replay_safe=True,
        response_type=read_pb2.GetProjectResponse,
    )
    await _close_session(session, supervisor)

    # Recording never mutates the live request or the response returned to the API.
    assert request.project_id == RAW_PROJECT_ID
    assert response.project.title == SENSITIVE_TITLE
    assert live_channel.invocations[0][2] == (("authorization", f"Bearer {SENSITIVE_BEARER}"),)

    raw_cassette = cassette_path.read_text(encoding="utf-8")
    assert RAW_PROJECT_ID not in raw_cassette
    assert RAW_SOURCE_ID not in raw_cassette
    assert SENSITIVE_TITLE not in raw_cassette
    assert SENSITIVE_URL not in raw_cassette
    assert SENSITIVE_BEARER not in raw_cassette
    assert "authorization" not in raw_cassette.casefold()

    cassette = AndroidGrpcCassette.load(cassette_path)
    [interaction] = cassette.interactions
    assert interaction.method == METHOD
    assert interaction.shape == "unary_unary"
    assert interaction.request.type_name.endswith(".GetProjectRequest")
    assert interaction.responses[0].type_name.endswith(".GetProjectResponse")
    clean_request = read_pb2.GetProjectRequest.FromString(interaction.request.wire_bytes)
    clean_response = read_pb2.GetProjectResponse.FromString(interaction.responses[0].wire_bytes)
    assert clean_request.project_id == SAFE_PROJECT_ID
    assert clean_response.project.id == SAFE_PROJECT_ID
    assert clean_request.include_audio_overview_ids is True
    assert clean_response.project.title == "SCRUBBED_STRING_0001"
    assert clean_response.project.sources[0].metadata.original_source_content_type == (
        read_pb2.SOURCE_CONTENT_TYPE_URL
    )
    # dump/load is canonical and byte-stable.
    before = cassette_path.read_bytes()
    cassette.dump(cassette_path)
    assert cassette_path.read_bytes() == before
    assert json.loads(raw_cassette)["format"] == "notebooklm.android.grpc-cassette"

    mismatch = ReplayGrpcModule(cassette_path)
    invoke = mismatch.secure_channel(ANDROID_GRPC_TARGET, object()).unary_unary(
        METHOD,
        request_serializer=lambda message: message.SerializeToString(),
        response_deserializer=read_pb2.GetProjectResponse.FromString,
    )
    with pytest.raises(AndroidGrpcCassetteMismatch, match="application metadata mismatch"):
        await invoke(
            read_pb2.GetProjectRequest(
                project_id=SAFE_PROJECT_ID,
                include_audio_overview_ids=True,
            ),
            metadata=((CLIENT_TYPE_HEADER, b"unexpected-propagation"),),
            timeout=None,
        )


@pytest.mark.asyncio
async def test_recording_pins_allowlisted_application_metadata_keys_without_values(
    tmp_path: Path,
) -> None:
    cassette_path = tmp_path / "application-metadata.grpc.json"
    recorder = RecordingGrpcModule(
        _LiveGrpc(_LiveChannel(_response())),
        cassette_path,
        sanitizer=ProtoRedactor(),
    )
    session, supervisor = await _open_session(recorder, _LeaseBearer([]))

    async def application_metadata(_bearer: str) -> tuple[tuple[str, bytes], ...]:
        return (
            (CLIENT_TYPE_HEADER, b"private-client-marker"),
            (EXPERIMENT_TOKEN_HEADER, b"private-experiment-token"),
        )

    await session.unary(
        METHOD,
        read_pb2.GetProjectRequest(project_id=RAW_PROJECT_ID),
        replay_safe=True,
        response_type=read_pb2.GetProjectResponse,
        metadata_augmentor=application_metadata,
    )
    await _close_session(session, supervisor)

    raw = cassette_path.read_text(encoding="utf-8")
    [interaction] = AndroidGrpcCassette.load(cassette_path).interactions
    assert interaction.application_metadata_keys == (
        CLIENT_TYPE_HEADER,
        EXPERIMENT_TOKEN_HEADER,
    )
    assert "private-client-marker" not in raw
    assert "private-experiment-token" not in raw
    assert "authorization" not in raw.casefold()

    mismatch = ReplayGrpcModule(cassette_path)
    invoke = mismatch.secure_channel(ANDROID_GRPC_TARGET, object()).unary_unary(
        METHOD,
        request_serializer=lambda message: message.SerializeToString(),
        response_deserializer=read_pb2.GetProjectResponse.FromString,
    )
    with pytest.raises(AndroidGrpcCassetteMismatch, match="application metadata mismatch"):
        await invoke(
            read_pb2.GetProjectRequest(project_id=SAFE_PROJECT_ID),
            metadata=(("authorization", "Bearer ignored"),),
            timeout=None,
        )

    replay = ReplayGrpcModule(cassette_path)
    replay_session, replay_supervisor = await _open_session(replay, ReplayBearer())
    await replay_session.unary(
        METHOD,
        read_pb2.GetProjectRequest(project_id=SAFE_PROJECT_ID),
        replay_safe=True,
        response_type=read_pb2.GetProjectResponse,
        metadata_augmentor=application_metadata,
    )
    replay.assert_consumed()
    await _close_session(replay_session, replay_supervisor)


def test_cassette_loader_rejects_explicit_null_application_metadata(tmp_path: Path) -> None:
    source = ANDROID_CASSETTES / "chat_session_control_recorded.grpc.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    data["interactions"][0]["application_metadata_keys"] = None
    cassette = tmp_path / "null-metadata.grpc.json"
    cassette.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(AndroidGrpcCassetteError, match="must be a list of text keys"):
        AndroidGrpcCassette.load(cassette)


@pytest.mark.asyncio
async def test_recording_always_redacts_after_an_identity_custom_sanitizer(
    tmp_path: Path,
) -> None:
    cassette_path = tmp_path / "identity-sanitizer.grpc.json"
    private_bytes = b"private-binary-payload-must-not-persist"
    private_message = AnyMessage(type_url=SENSITIVE_URL, value=private_bytes)
    recorder = RecordingGrpcModule(
        _LiveGrpc(_LiveChannel(cast(Any, private_message))),
        cassette_path,
        sanitizer=lambda _method, _direction, message: message,
    )
    session, supervisor = await _open_session(recorder, _LeaseBearer([]))

    response = await session.unary(
        METHOD,
        private_message,
        replay_safe=True,
        response_type=AnyMessage,
    )
    await _close_session(session, supervisor)

    assert response == private_message
    cassette = AndroidGrpcCassette.load(cassette_path)
    [interaction] = cassette.interactions
    clean_request = AnyMessage.FromString(interaction.request.wire_bytes)
    clean_response = AnyMessage.FromString(interaction.responses[0].wire_bytes)
    assert clean_request.type_url.startswith("https://example.invalid/")
    # Opaque request bytes are numbered per request; response bytes globally.
    assert clean_request.value.startswith(b"SCRUBBED_REQUEST_BYTES_")
    assert clean_response.type_url == clean_request.type_url
    assert clean_response.value.startswith(b"SCRUBBED_BYTES_")
    assert private_bytes not in interaction.request.wire_bytes
    assert SENSITIVE_URL.encode() not in interaction.request.wire_bytes


@pytest.mark.asyncio
async def test_replay_never_uses_live_channel_or_real_bearer_and_pins_request_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cassette_path = tmp_path / "replay.grpc.json"
    live_channel = _LiveChannel(_response())
    recorder = RecordingGrpcModule(
        _LiveGrpc(live_channel),
        cassette_path,
        sanitizer=ProtoRedactor(),
    )
    record_session, record_supervisor = await _open_session(recorder, _LeaseBearer([]))
    await record_session.unary(
        METHOD,
        read_pb2.GetProjectRequest(
            project_id=RAW_PROJECT_ID,
            include_audio_overview_ids=True,
        ),
        replay_safe=True,
        response_type=read_pb2.GetProjectResponse,
    )
    await _close_session(record_session, record_supervisor)

    # Even if grpc's real constructor is importable, replay must not touch it.
    import grpc

    monkeypatch.setattr(
        grpc.aio,
        "secure_channel",
        lambda *_args, **_kwargs: pytest.fail("replay opened a live gRPC channel"),
    )
    mismatch_replay = ReplayGrpcModule(cassette_path)
    mismatch_channel = mismatch_replay.secure_channel(ANDROID_GRPC_TARGET, object())
    invoke = mismatch_channel.unary_unary(
        METHOD,
        request_serializer=lambda message: message.SerializeToString(),
        response_deserializer=read_pb2.GetProjectResponse.FromString,
    )
    with pytest.raises(AndroidGrpcCassetteMismatch, match="request mismatch"):
        await invoke(
            read_pb2.GetProjectRequest(project_id=SAFE_PROJECT_ID),
            metadata=(),
            timeout=None,
        )

    replay = ReplayGrpcModule(cassette_path)
    bearer = ReplayBearer()
    session, supervisor = await _open_session(replay, bearer)
    response = await session.unary(
        METHOD,
        read_pb2.GetProjectRequest(
            project_id=SAFE_PROJECT_ID,
            include_audio_overview_ids=True,
        ),
        replay_safe=True,
        response_type=read_pb2.GetProjectResponse,
    )

    assert response.project.id == SAFE_PROJECT_ID
    assert bearer.activations == [1]
    assert bearer.gets == [1]
    assert replay.secure_channel_calls == 1
    replay.assert_consumed()

    assert replay.channel is not None
    consumed_invoke = replay.channel.unary_unary(
        METHOD,
        request_serializer=lambda message: message.SerializeToString(),
        response_deserializer=read_pb2.GetProjectResponse.FromString,
    )
    with pytest.raises(AndroidGrpcCassetteMismatch, match="exhausted"):
        await consumed_invoke(
            read_pb2.GetProjectRequest(project_id=SAFE_PROJECT_ID),
            metadata=(),
            timeout=None,
        )
    await _close_session(session, supervisor)


@pytest.mark.asyncio
async def test_server_stream_record_and_replay_pins_shape_and_frame_type(tmp_path: Path) -> None:
    cassette_path = tmp_path / "stream.grpc.json"
    recorder = RecordingGrpcModule(
        _LiveGrpc(_LiveChannel(_response())),
        cassette_path,
        sanitizer=ProtoRedactor(),
    )
    record_session, record_supervisor = await _open_session(recorder, _LeaseBearer([]))
    recorded = [
        item
        async for item in record_session.stream(
            METHOD,
            read_pb2.GetProjectRequest(project_id=RAW_PROJECT_ID),
            replay_safe=True,
            response_type=read_pb2.GetProjectResponse,
        )
    ]
    await _close_session(record_session, record_supervisor)
    assert len(recorded) == 2

    cassette = AndroidGrpcCassette.load(cassette_path)
    [interaction] = cassette.interactions
    assert interaction.shape == "unary_stream"
    assert len(interaction.responses) == 2
    assert {response.type_name for response in interaction.responses} == {
        "google.internal.labs.tailwind.orchestration.v1.GetProjectResponse"
    }

    replay = ReplayGrpcModule(cassette_path)
    replay_session, replay_supervisor = await _open_session(replay, ReplayBearer())
    replayed = [
        item
        async for item in replay_session.stream(
            METHOD,
            read_pb2.GetProjectRequest(project_id=SAFE_PROJECT_ID),
            replay_safe=True,
            response_type=read_pb2.GetProjectResponse,
        )
    ]
    replay.assert_consumed()
    await _close_session(replay_session, replay_supervisor)
    assert [item.project.id for item in replayed] == [SAFE_PROJECT_ID, SAFE_PROJECT_ID]


@pytest.mark.parametrize("cassette_path", sorted(ANDROID_CASSETTES.glob("*.grpc.json")))
def test_committed_android_grpc_cassettes_are_canonical_and_credential_free(
    cassette_path: Path,
) -> None:
    raw = cassette_path.read_text(encoding="utf-8")
    cassette = AndroidGrpcCassette.load(cassette_path)

    assert raw == json.dumps(cassette.to_json(), indent=2, sort_keys=True) + "\n"
    assert "authorization" not in raw.casefold()
    assert "bearer" not in raw.casefold()

    payload_bytes = [
        payload.wire_bytes
        for interaction in cassette.interactions
        for payload in (interaction.request, *interaction.responses)
    ]
    decoded_payloads = "\n".join(payload.decode("latin1") for payload in payload_bytes)
    for name, pattern in _CREDENTIAL_DETECTORS:
        assert pattern.search(raw) is None, f"{cassette_path.name} contains {name}"
        assert pattern.search(decoded_payloads) is None, (
            f"{cassette_path.name} protobuf contains {name}"
        )

    # A credential/high-entropy scan cannot prove ordinary scalar fields were
    # redacted. Decode every committed payload through its known FQN and demand
    # that the mandatory scalar redactor is already an idempotent no-op. New
    # cassette message types must be registered here rather than escaping as
    # opaque base64.
    for interaction in cassette.interactions:
        directed_payloads = (
            ("request", interaction.request),
            *(("response", response) for response in interaction.responses),
        )
        for direction, payload in directed_payloads:
            assert payload.type_name in KNOWN_CASSETTE_PAYLOAD_TYPES, (
                f"{cassette_path.name} uses unregistered protobuf FQN {payload.type_name}"
            )
            message_type = KNOWN_CASSETTE_PAYLOAD_TYPES[payload.type_name]
            decoded = message_type.FromString(payload.wire_bytes)
            sanitized = ProtoRedactor(trust_placeholders=True)(
                interaction.method,
                direction,
                decoded,
            )
            assert sanitized.SerializeToString(deterministic=True) == payload.wire_bytes, (
                f"{cassette_path.name} {direction} {payload.type_name} is not fully redacted"
            )
