"""Placeholder reservation, request normalizers, and the record-or-replay harness."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from tests._helpers.android_grpc_cassette import (
    AndroidGrpcCassetteError,
    AndroidGrpcCassetteMismatch,
    ProtoRedactor,
    RecordingGrpcModule,
    compose_sanitizers,
)
from tests._helpers.android_grpc_harness import android_cassette_client, bind_values
from tests._helpers.android_grpc_normalizers import REQUEST_NONCE_FIELDS, normalize_request

from notebooklm._android.chat import GENERATE_FREE_FORM_STREAMED_METHOD
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
    organization_pb2,
    read_pb2,
)
from notebooklm._android.proto.notebooklm.android.wire.v1 import organization_mutations_pb2

CASSETTES = Path(__file__).resolve().parents[2] / "cassettes" / "android"
NOTEBOOK_ID = "2f1c1b2a-9d4e-4c7b-8a1f-0123456789ab"
UUID_PLACEHOLDER_1 = "00000000-0000-4000-8000-000000000001"
UUID_PLACEHOLDER_2 = "00000000-0000-4000-8000-000000000002"


def test_reserve_assigns_placeholders_in_reservation_order_before_traffic() -> None:
    redactor = ProtoRedactor(trust_placeholders=True)

    assert redactor.reserve(NOTEBOOK_ID) == UUID_PLACEHOLDER_1
    assert redactor.reserve("What is this?") == "SCRUBBED_STRING_0001"
    assert (
        redactor.reserve("https://example.com/a")
        == "https://example.invalid/grpc-cassette/url-0001"
    )

    # Traffic seen later continues the same counters and honours the reservation.
    message = read_pb2.GetProjectRequest(project_id=NOTEBOOK_ID)
    sanitized = redactor("/svc/GetProject", "request", message)
    assert sanitized.project_id == UUID_PLACEHOLDER_1
    later = redactor(
        "/svc/GetProject", "request", read_pb2.GetProjectRequest(project_id=str(uuid4()))
    )
    assert later.project_id == UUID_PLACEHOLDER_2


def test_reserve_rejects_non_string_and_empty_values() -> None:
    redactor = ProtoRedactor()
    with pytest.raises(AndroidGrpcCassetteError):
        redactor.reserve("")
    with pytest.raises(AndroidGrpcCassetteError):
        redactor.reserve(b"bytes")  # type: ignore[arg-type]


def test_uuid_shaped_bytes_keep_uuid_shape_and_share_the_string_table() -> None:
    redactor = ProtoRedactor()
    response = organization_mutations_pb2.GetLabelsWireResponse()
    collection = response.collections.add()
    collection.id = NOTEBOOK_ID
    collection.member_ids.append(NOTEBOOK_ID.encode("ascii"))
    collection.member_ids.append(b"\x00\x01opaque")

    sanitized = redactor("/svc/GetLabels", "response", response)

    assert sanitized.collections[0].id == UUID_PLACEHOLDER_1
    assert sanitized.collections[0].member_ids[0] == UUID_PLACEHOLDER_1.encode("ascii")
    assert sanitized.collections[0].member_ids[1] == b"SCRUBBED_BYTES_0001"
    # Idempotent under the trusting redactor used for replay and the canonical guard.
    again = ProtoRedactor(trust_placeholders=True)("/svc/GetLabels", "response", sanitized)
    assert again.SerializeToString(deterministic=True) == sanitized.SerializeToString(
        deterministic=True
    )


def test_normalize_request_clears_only_registered_nonce_fields() -> None:
    request = chat_pb2.GenerateFreeFormStreamedRequest(
        user_query="q", user_message_id=str(uuid4()), project_id=NOTEBOOK_ID
    )
    normalized = normalize_request(GENERATE_FREE_FORM_STREAMED_METHOD, "request", request)
    assert normalized.user_message_id == ""
    assert normalized.user_query == "q"
    assert normalized.project_id == NOTEBOOK_ID

    untouched = chat_pb2.GenerateFreeFormStreamedRequest(user_message_id="keep")
    assert normalize_request("/svc/Other", "request", untouched).user_message_id == "keep"
    response = chat_pb2.GenerateFreeFormStreamedResponse(is_final_response=True)
    assert normalize_request(GENERATE_FREE_FORM_STREAMED_METHOD, "response", response) is response


def test_normalize_request_fails_loudly_when_a_registered_field_disappears() -> None:
    # A proto regeneration that renames the nonce field must not silently
    # start recording nonces (and never match on replay).
    wrong_type = organization_pb2.GetLabelsRequest()
    with pytest.raises(TypeError, match="has no field 'user_message_id'"):
        normalize_request(GENERATE_FREE_FORM_STREAMED_METHOD, "request", wrong_type)


def test_registered_nonce_fields_exist_on_their_request_types() -> None:
    assert REQUEST_NONCE_FIELDS == {GENERATE_FREE_FORM_STREAMED_METHOD: ("user_message_id",)}
    assert "user_message_id" in chat_pb2.GenerateFreeFormStreamedRequest.DESCRIPTOR.fields_by_name


def test_compose_sanitizers_applies_in_order_and_requires_messages() -> None:
    redactor = ProtoRedactor(trust_placeholders=True)
    composed = compose_sanitizers(normalize_request, redactor)
    request = chat_pb2.GenerateFreeFormStreamedRequest(
        user_query="hello", user_message_id=str(uuid4())
    )
    sanitized = composed(GENERATE_FREE_FORM_STREAMED_METHOD, "request", request)
    assert sanitized.user_message_id == ""
    assert sanitized.user_query == "SCRUBBED_REQUEST_0001"

    with pytest.raises(AndroidGrpcCassetteError):
        compose_sanitizers()
    broken = compose_sanitizers(lambda _method, _direction, _message: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(AndroidGrpcCassetteError, match="must return a protobuf message"):
        broken("/svc/X", "request", request)


def test_bind_values_gives_real_inputs_when_recording_and_placeholders_on_replay() -> None:
    recording = bind_values(
        ProtoRedactor(trust_placeholders=True),
        notebook_id=NOTEBOOK_ID,
        question="What is this?",
        record=True,
    )
    assert (recording.notebook_id, recording.question) == (NOTEBOOK_ID, "What is this?")

    replay_a = bind_values(
        ProtoRedactor(trust_placeholders=True),
        notebook_id=str(uuid4()),
        question="anything",
        record=False,
    )
    replay_b = bind_values(
        ProtoRedactor(trust_placeholders=True),
        notebook_id=str(uuid4()),
        question="something else",
        record=False,
    )
    assert replay_a == replay_b
    assert replay_a.notebook_id == UUID_PLACEHOLDER_1
    assert replay_a.question == "SCRUBBED_STRING_0001"
    assert replay_a.url == "https://example.invalid/grpc-cassette/url-0001"
    assert replay_a.research_query == "SCRUBBED_STRING_0002"


def test_recording_rejects_a_redactor_that_would_renumber_placeholders(tmp_path: Path) -> None:
    with pytest.raises(AndroidGrpcCassetteError, match="trust_placeholders=True"):
        RecordingGrpcModule(
            object(),
            tmp_path / "x.grpc.json",
            sanitizer=normalize_request,
            redactor=ProtoRedactor(trust_placeholders=False),
        )
    with pytest.raises(AndroidGrpcCassetteError, match="trust_placeholders=True"):
        RecordingGrpcModule(
            object(),
            tmp_path / "x.grpc.json",
            sanitizer=normalize_request,
            redactor=object(),  # type: ignore[arg-type]
        )


def test_trusted_placeholders_retire_their_index_so_reservation_stays_injective() -> None:
    redactor = ProtoRedactor(trust_placeholders=True)

    assert redactor.reserve(UUID_PLACEHOLDER_1) == UUID_PLACEHOLDER_1
    assert redactor.reserve(NOTEBOOK_ID) == UUID_PLACEHOLDER_2
    assert redactor.reserve("SCRUBBED_STRING_0007") == "SCRUBBED_STRING_0007"
    assert redactor.reserve("fresh text") == "SCRUBBED_STRING_0008"
    assert (
        redactor.reserve("https://example.invalid/grpc-cassette/url-0003")
        == "https://example.invalid/grpc-cassette/url-0003"
    )
    assert redactor.reserve("https://example.com/x") == (
        "https://example.invalid/grpc-cassette/url-0004"
    )

    response = organization_mutations_pb2.GetLabelsWireResponse()
    collection = response.collections.add()
    collection.member_ids.append(b"SCRUBBED_BYTES_0005")
    collection.member_ids.append(b"\x00opaque")
    sanitized = redactor("/svc/GetLabels", "response", response)
    assert list(sanitized.collections[0].member_ids) == [
        b"SCRUBBED_BYTES_0005",
        b"SCRUBBED_BYTES_0006",
    ]


def test_cassette_mismatch_escapes_the_session_status_mapping() -> None:
    assert not issubclass(AndroidGrpcCassetteMismatch, Exception)
    assert issubclass(AndroidGrpcCassetteMismatch, BaseException)


@pytest.mark.asyncio
async def test_replay_mismatch_reaches_the_test_verbatim_through_the_public_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cassette = CASSETTES / "get_project_rich_recorded.grpc.json"
    # The cassette holds GetProject for notebooks.get then sources.list; asking
    # for sharing status instead must fail as a cassette mismatch, not as the
    # session's sanitized ``RPCError(... UNKNOWN)``.
    with pytest.raises(AndroidGrpcCassetteMismatch, match="request mismatch at interaction 1"):
        async with android_cassette_client(cassette, monkeypatch=monkeypatch, scratch=None) as (
            client,
            values,
        ):
            await client.notebooks.get(values.notebook_id)
            await client.sharing.get_status(values.notebook_id)


def test_request_constants_are_numbered_per_request_independently_of_response_traffic() -> None:
    from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
        sources_pb2,
    )

    def make_request() -> chat_pb2.ActOnSourcesRequest:
        request = chat_pb2.ActOnSourcesRequest()
        request.mind_map_action.action = "generate"
        request.mind_map_action.language = "en"
        return request

    quiet = ProtoRedactor(trust_placeholders=True)
    busy = ProtoRedactor(trust_placeholders=True)
    # ``busy`` has seen plenty of response strings first; ``quiet`` has not.
    busy(
        "/svc/GetProject",
        "response",
        read_pb2.GetProjectResponse(project=read_pb2.Project(title="a", emoji="b")),
    )
    busy(
        "/svc/LoadSource",
        "response",
        sources_pb2.LoadSourceRequest(source_id=read_pb2.SourceId(id=str(uuid4()))),
    )

    quiet_out = quiet("/svc/ActOnSources", "request", make_request())
    busy_out = busy("/svc/ActOnSources", "request", make_request())
    assert quiet_out.SerializeToString(deterministic=True) == busy_out.SerializeToString(
        deterministic=True
    )
    assert quiet_out.mind_map_action.action == "SCRUBBED_REQUEST_0001"
    assert quiet_out.mind_map_action.language == "SCRUBBED_REQUEST_0002"
    # Request-local numbering never leaks into the global table.
    assert quiet.known_string("generate") is None


def test_ids_echoed_by_responses_keep_their_global_placeholder_in_later_requests() -> None:
    redactor = ProtoRedactor(trust_placeholders=True)
    response = redactor(
        "/svc/GetProject",
        "response",
        read_pb2.GetProjectResponse(project=read_pb2.Project(id=NOTEBOOK_ID, title="Title")),
    )
    request = redactor(
        "/svc/GetProject", "request", read_pb2.GetProjectRequest(project_id=NOTEBOOK_ID)
    )
    assert response.project.id == UUID_PLACEHOLDER_1
    assert request.project_id == UUID_PLACEHOLDER_1
    # A UUID first seen in a request is also assigned globally, so a later
    # response echoing it agrees.
    other = str(uuid4())
    first = redactor("/svc/GetProject", "request", read_pb2.GetProjectRequest(project_id=other))
    echoed = redactor(
        "/svc/GetProject",
        "response",
        read_pb2.GetProjectResponse(project=read_pb2.Project(id=other)),
    )
    assert first.project_id == echoed.project.id == UUID_PLACEHOLDER_2


def test_wrapped_source_id_bytes_keep_their_framing_and_uuid_shape() -> None:
    redactor = ProtoRedactor()
    wrapped = read_pb2.SourceId(id=NOTEBOOK_ID).SerializeToString()
    assert wrapped[:2] == b"\x0a\x24"
    response = organization_mutations_pb2.GetLabelsWireResponse()
    row = response.collections.add()
    row.member_ids.append(wrapped)
    sanitized = redactor("/svc/GetLabels", "response", response)
    member = sanitized.collections[0].member_ids[0]
    assert read_pb2.SourceId.FromString(member).id == UUID_PLACEHOLDER_1
    again = ProtoRedactor(trust_placeholders=True)("/svc/GetLabels", "response", sanitized)
    assert again.SerializeToString(deterministic=True) == sanitized.SerializeToString(
        deterministic=True
    )


def test_research_job_status_code_is_preserved_as_schema_semantics() -> None:
    from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
        research_pb2,
    )

    response = research_pb2.ListDiscoverSourcesJobResponse()
    job = response.jobs.add()
    job.info.status = 2
    job.info.results.summary = "private summary"
    sanitized = ProtoRedactor()("/svc/ListDiscoverSourcesJob", "response", response)
    assert sanitized.jobs[0].info.status == 2
    assert sanitized.jobs[0].info.results.summary == "SCRUBBED_STRING_0001"


def test_request_map_fields_and_opaque_bytes_are_scoped_per_request() -> None:
    from google.protobuf.any_pb2 import Any as AnyMessage
    from google.protobuf.struct_pb2 import Struct

    redactor = ProtoRedactor(trust_placeholders=True)
    struct = Struct()
    struct["secret-key"] = "secret-value"
    struct["https://example.com/private"] = "SCRUBBED_STRING_0007"
    sanitized = redactor("/svc/X", "request", struct)
    keys = sorted(sanitized.fields)
    assert keys == ["SCRUBBED_REQUEST_0001", "https://example.invalid/grpc-cassette/url-0001"]
    assert sanitized.fields["SCRUBBED_REQUEST_0001"].string_value == "SCRUBBED_REQUEST_0002"
    assert (
        sanitized.fields["https://example.invalid/grpc-cassette/url-0001"].string_value
        == "SCRUBBED_STRING_0007"
    )
    assert redactor.known_string("secret-key") is None

    blob = AnyMessage(type_url="https://example.com/type", value=b"\x00\x01private-bytes")
    scoped = redactor("/svc/X", "request", blob)
    assert scoped.type_url == "https://example.invalid/grpc-cassette/url-0002"
    assert scoped.value == b"SCRUBBED_REQUEST_BYTES_0001"
    assert redactor.known_bytes(b"\x00\x01private-bytes") is None
    # A forged "placeholder" URL with a non-numeric suffix is not trusted.
    forged = AnyMessage(type_url="https://example.invalid/grpc-cassette/url-real-path")
    assert redactor("/svc/X", "request", forged).type_url.endswith("url-0003")


def test_every_four_digit_placeholder_counter_is_budget_guarded() -> None:
    from tests._helpers import android_grpc_cassette as seam

    cases: list[tuple[object, str, object]] = [
        (ProtoRedactor(), "_string_count", "s"),
        (ProtoRedactor(), "_bytes_count", b"b"),
        (ProtoRedactor(), "_url_count", "https://example.com/x"),
        (seam._RequestScope(ProtoRedactor()), "_string_count", "s"),
        (seam._RequestScope(ProtoRedactor()), "_bytes_count", b"b"),
    ]
    for redactor, attribute, value in cases:
        setattr(redactor, attribute, seam._PLACEHOLDER_BUDGET)
        with pytest.raises(AndroidGrpcCassetteError, match="split the family"):
            if isinstance(value, bytes):
                redactor._sanitize_bytes(value)  # type: ignore[attr-defined]
            else:
                redactor._sanitize_string(value)  # type: ignore[attr-defined]
