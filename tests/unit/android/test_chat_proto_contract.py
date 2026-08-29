"""Descriptor and checked-in wire gates for the B5 Android chat overlay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google.protobuf import descriptor_pb2
from google.protobuf.descriptor import FieldDescriptor

from notebooklm._android.codecs.chat import decode_document, decode_references
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    b3_sources_pb2,
    b5_chat_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.common.protos import chat_history_pb2

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "android"
ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"
COMMON_PACKAGE = "labs.language.tailwind.common.protos"


def _field_shapes(message: Any) -> dict[str, tuple[int, bool, int, str | None]]:
    shapes: dict[str, tuple[int, bool, int, str | None]] = {}
    for field in message.DESCRIPTOR.fields:
        target = None
        if field.message_type is not None:
            target = field.message_type.full_name
        elif field.enum_type is not None:
            target = field.enum_type.full_name
        shapes[field.name] = (field.number, field.is_repeated, field.type, target)
    return shapes


def _without_implicit_json_names(
    descriptor: descriptor_pb2.FileDescriptorProto,
) -> descriptor_pb2.FileDescriptorProto:
    normalized = descriptor_pb2.FileDescriptorProto()
    normalized.CopyFrom(descriptor)
    pending = list(normalized.message_type)
    while pending:
        message = pending.pop()
        pending.extend(message.nested_type)
        for field in message.field:
            field.ClearField("json_name")
    return normalized


def test_b5_packages_imports_and_service_free_overlay_are_exact() -> None:
    assert b5_chat_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert chat_history_pb2.DESCRIPTOR.package == COMMON_PACKAGE
    assert [dependency.name for dependency in b5_chat_pb2.DESCRIPTOR.dependencies] == [
        "google/internal/labs/tailwind/orchestration/v1/b1_read.proto",
        "google/internal/labs/tailwind/orchestration/v1/b3_sources.proto",
        "google/protobuf/timestamp.proto",
        "labs/language/tailwind/common/protos/chat_history.proto",
    ]
    assert b5_chat_pb2.DESCRIPTOR.services_by_name == {}
    assert chat_history_pb2.DESCRIPTOR.services_by_name == {}
    assert _field_shapes(chat_history_pb2.ChatSession) == {
        "chat_session_id": (1, False, FieldDescriptor.TYPE_STRING, None)
    }


def test_b5_request_response_fields_are_exhaustive() -> None:
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    boolean = FieldDescriptor.TYPE_BOOL
    int32 = FieldDescriptor.TYPE_INT32
    message = FieldDescriptor.TYPE_MESSAGE
    enum = FieldDescriptor.TYPE_ENUM
    o = ORCHESTRATION_PACKAGE

    expected = {
        b3_sources_pb2.InputSource: {
            "source_id": (1, singular, message, f"{o}.SourceId"),
        },
        b5_chat_pb2.ConversationEvent: {
            "text": (1, singular, string, None),
            "type": (3, singular, enum, f"{o}.ConversationEvent.ConversationEventType"),
        },
        b5_chat_pb2.ConversationTurnKey: {
            "session_id": (1, singular, string, None),
            "conversation_id": (2, singular, string, None),
            "observed_field_3": (3, singular, int32, None),
        },
        b5_chat_pb2.ObjectId: {"id": (1, singular, string, None)},
        b5_chat_pb2.Range: {
            "start_index": (2, singular, int32, None),
            "end_index": (3, singular, int32, None),
        },
        b5_chat_pb2.AnnotationMapEntry: {
            "object_id": (1, singular, message, f"{o}.ObjectId"),
            "content_range": (2, singular, message, f"{o}.Range"),
        },
        b5_chat_pb2.TextRun: {"content": (1, singular, string, None)},
        b5_chat_pb2.ParagraphElement: {
            "start_index": (1, singular, int32, None),
            "end_index": (2, singular, int32, None),
            "text_run": (3, singular, message, f"{o}.TextRun"),
        },
        b5_chat_pb2.Paragraph: {
            "elements": (1, repeated, message, f"{o}.ParagraphElement"),
        },
        b5_chat_pb2.StructuralElement: {
            "start_index": (1, singular, int32, None),
            "end_index": (2, singular, int32, None),
            "paragraph": (3, singular, message, f"{o}.Paragraph"),
        },
        b5_chat_pb2.Body: {
            "content": (1, repeated, message, f"{o}.StructuralElement"),
            "inline_object_locations": (
                2,
                repeated,
                message,
                f"{o}.AnnotationMapEntry",
            ),
        },
        b5_chat_pb2.TailwindDocFragment: {
            "elements": (1, repeated, message, f"{o}.StructuralElement"),
        },
        b5_chat_pb2.SourceRevision: {
            "source": (1, singular, message, f"{o}.SourceId"),
        },
        b5_chat_pb2.CitationSource: {
            "ingested_source": (1, singular, message, f"{o}.SourceRevision"),
        },
        b5_chat_pb2.Citation: {
            "fragment": (5, singular, message, f"{o}.TailwindDocFragment"),
            "source_attribution": (6, singular, message, f"{o}.CitationSource"),
            "object_id": (7, singular, message, f"{o}.ObjectId"),
        },
        b5_chat_pb2.DocumentObject: {
            "object_id": (1, singular, message, f"{o}.ObjectId"),
            "citation": (2, singular, message, f"{o}.Citation"),
        },
        b5_chat_pb2.TailwindDoc: {
            "body": (1, singular, message, f"{o}.Body"),
            "objects": (4, repeated, message, f"{o}.DocumentObject"),
        },
        b5_chat_pb2.AnswerResponse: {
            "response": (1, singular, string, None),
            "conversation_turn_key": (3, singular, message, f"{o}.ConversationTurnKey"),
            "response_doc": (5, singular, message, f"{o}.TailwindDoc"),
        },
        b5_chat_pb2.ActOnSourcesResponse: {
            "response": (1, singular, message, f"{o}.AnswerResponse"),
        },
        b5_chat_pb2.ChatHistoryMessage: {
            "message_id": (1, singular, string, None),
            "timestamp": (2, singular, message, "google.protobuf.Timestamp"),
            "observed_event_type": (3, singular, int32, None),
            "user_query_text": (4, singular, string, None),
            "act_on_sources_response": (
                5,
                singular,
                message,
                f"{o}.ActOnSourcesResponse",
            ),
        },
        b5_chat_pb2.ListChatSessionsRequest: {
            "project_id": (3, singular, string, None),
        },
        b5_chat_pb2.ListChatSessionsResponse: {
            "sessions": (1, repeated, message, f"{COMMON_PACKAGE}.ChatSession"),
        },
        b5_chat_pb2.ListChatTurnsRequest: {
            "chat_session_id": (4, singular, string, None),
            "page_token": (6, singular, string, None),
        },
        b5_chat_pb2.ListChatTurnsResponse: {
            "chat_turns": (1, repeated, message, f"{o}.ChatHistoryMessage"),
            "next_page_token": (2, singular, string, None),
        },
        b5_chat_pb2.DeleteChatTurnsRequest: {
            "chat_session_id": (2, singular, string, None),
            "delete_all_history": (4, singular, boolean, None),
        },
        b5_chat_pb2.GenerateFreeFormStreamedRequest: {
            "sources": (1, repeated, message, f"{o}.InputSource"),
            "user_query": (2, singular, string, None),
            "conversation_history": (3, repeated, message, f"{o}.ConversationEvent"),
            "chat_session_id": (5, singular, string, None),
            "user_message_id": (6, singular, string, None),
            "project_id": (8, singular, string, None),
            "origin": (9, singular, enum, f"{o}.QueryOrigin"),
        },
        b5_chat_pb2.GenerateFreeFormStreamedResponse: {
            "answer": (1, singular, message, f"{o}.AnswerResponse"),
            "is_final_response": (5, singular, boolean, None),
        },
    }

    assert {
        message_type.DESCRIPTOR.name
        for message_type in expected
        if message_type is not b3_sources_pb2.InputSource
    } == set(b5_chat_pb2.DESCRIPTOR.message_types_by_name)
    for message_type, fields in expected.items():
        assert _field_shapes(message_type) == fields


def test_b5_enum_names_and_numbers_match_checked_in_evidence() -> None:
    assert {value.name: value.number for value in b5_chat_pb2.QueryOrigin.DESCRIPTOR.values} == {
        "QUERY_ORIGIN_UNSPECIFIED": 0,
        "QUERY_ORIGIN_CHAT_TEXT_BOX": 1,
        "QUERY_ORIGIN_SUGGESTION_CHIP": 2,
        "QUERY_ORIGIN_APP_VIEWER_EXPLAIN_BUTTON": 3,
        "QUERY_ORIGIN_SOURCE_KEY_TOPIC_CHIP": 4,
        "QUERY_ORIGIN_MINDMAP_TOPIC_CHIP": 5,
        "QUERY_ORIGIN_GETTING_STARTED_MESSAGE": 6,
        "QUERY_ORIGIN_LINK_SERVICE": 7,
        "QUERY_ORIGIN_ARTIFACT_VIEWER": 8,
    }
    nested = b5_chat_pb2.ConversationEvent.ConversationEventType.DESCRIPTOR
    assert {value.name: value.number for value in nested.values} == {
        "EVENT_TYPE_UNKNOWN": 0,
        "USER_QUERY": 1,
        "GENERATED_RESPONSE": 2,
    }


def test_b5_descriptor_fixture_matches_generated_file_descriptors() -> None:
    descriptor_set = descriptor_pb2.FileDescriptorSet.FromString(
        (FIXTURES / "android_descriptor_set.pb").read_bytes()
    )
    files = {file.name: file for file in descriptor_set.file}
    assert {
        "google/internal/labs/tailwind/orchestration/v1/b1_read.proto",
        "google/internal/labs/tailwind/orchestration/v1/b5_chat.proto",
        "google/internal/labs/tailwind/v1/source_settings.proto",
        "google/protobuf/timestamp.proto",
        "labs/language/tailwind/common/protos/chat_history.proto",
    } <= set(files)
    assert _without_implicit_json_names(files[b5_chat_pb2.DESCRIPTOR.name]) == (
        descriptor_pb2.FileDescriptorProto.FromString(b5_chat_pb2.DESCRIPTOR.serialized_pb)
    )
    assert _without_implicit_json_names(files[chat_history_pb2.DESCRIPTOR.name]) == (
        descriptor_pb2.FileDescriptorProto.FromString(chat_history_pb2.DESCRIPTOR.serialized_pb)
    )


def test_checked_in_b5_wire_fixture_round_trips_without_unknown_semantics() -> None:
    fixture = json.loads((FIXTURES / "b5_chat_wire.json").read_text(encoding="utf-8"))
    types = {
        "generate_request": b5_chat_pb2.GenerateFreeFormStreamedRequest,
        "partial_frame": b5_chat_pb2.GenerateFreeFormStreamedResponse,
        "final_frame": b5_chat_pb2.GenerateFreeFormStreamedResponse,
        "history_response": b5_chat_pb2.ListChatTurnsResponse,
        "sessions_response": b5_chat_pb2.ListChatSessionsResponse,
    }
    decoded = {}
    for name, message_type in types.items():
        wire = bytes(fixture[name])
        message = message_type.FromString(wire)
        assert message.SerializeToString(deterministic=True) == wire
        decoded[name] = message

    request = decoded["generate_request"]
    assert request.user_message_id == "00000000-0000-4000-8000-000000000099"
    assert [(event.text, event.type) for event in request.conversation_history] == [
        ("Cached answer.", b5_chat_pb2.ConversationEvent.GENERATED_RESPONSE),
        ("Cached question?", b5_chat_pb2.ConversationEvent.USER_QUERY),
    ]
    assert decoded["partial_frame"].is_final_response is False
    assert decoded["final_frame"].is_final_response is True
    assert decoded["final_frame"].answer.response == "Final answer [2]"
    assert decoded["final_frame"].answer.conversation_turn_key.observed_field_3 == 17
    response_doc = decoded["final_frame"].answer.response_doc
    assert len(response_doc.objects) == 2
    assert not response_doc.objects[0].HasField("citation")
    references = decode_references(response_doc, decode_document(response_doc))
    assert [reference.citation_number for reference in references] == [2]
    assert decoded["history_response"].next_page_token == "next-page"
    assert decoded["history_response"].chat_turns[0].observed_event_type == 1
    assert decoded["sessions_response"].sessions[0].chat_session_id == "conversation-1"
