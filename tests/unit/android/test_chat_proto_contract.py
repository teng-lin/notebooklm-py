"""Descriptor and checked-in wire gates for the chat Android chat overlay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google.protobuf import descriptor_pb2
from google.protobuf.descriptor import FieldDescriptor

from notebooklm._android.codecs.chat import decode_document, decode_references
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
    notebooks_pb2,
    sources_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1.agency import (
    supported_pb2 as agency_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.common.protos import common_pb2

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "android"
ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"
AGENCY_PACKAGE = f"{ORCHESTRATION_PACKAGE}.agency"
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


def test_chat_packages_imports_and_service_free_overlay_are_exact() -> None:
    assert chat_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert common_pb2.DESCRIPTOR.package == COMMON_PACKAGE
    assert [dependency.name for dependency in chat_pb2.DESCRIPTOR.dependencies] == [
        "google/internal/labs/tailwind/orchestration/v1/read.proto",
        "google/internal/labs/tailwind/orchestration/v1/sources.proto",
        "google/internal/labs/tailwind/orchestration/v1/notebooks.proto",
        "google/internal/labs/tailwind/orchestration/v1/agency/supported.proto",
        "google/protobuf/timestamp.proto",
        "labs/language/tailwind/common/protos/common.proto",
        "labs/language/tailwind/common/protos/metadata.proto",
    ]
    assert chat_pb2.DESCRIPTOR.services_by_name == {}
    assert agency_pb2.DESCRIPTOR.package == AGENCY_PACKAGE
    assert agency_pb2.DESCRIPTOR.services_by_name == {}
    assert common_pb2.DESCRIPTOR.services_by_name == {}
    assert _field_shapes(common_pb2.ChatSession) == {
        "chat_session_id": (1, False, FieldDescriptor.TYPE_STRING, None)
    }
    assert _field_shapes(common_pb2.ProjectPublicSettings) == {
        "is_publicly_readable": (1, False, FieldDescriptor.TYPE_BOOL, None),
        "is_discoverable": (2, False, FieldDescriptor.TYPE_BOOL, None),
    }


def test_chat_request_response_fields_are_exhaustive() -> None:
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    boolean = FieldDescriptor.TYPE_BOOL
    int32 = FieldDescriptor.TYPE_INT32
    double = FieldDescriptor.TYPE_DOUBLE
    message = FieldDescriptor.TYPE_MESSAGE
    enum = FieldDescriptor.TYPE_ENUM
    o = ORCHESTRATION_PACKAGE

    expected = {
        sources_pb2.InputSource: {
            "source_id": (1, singular, message, f"{o}.SourceId"),
        },
        chat_pb2.ConversationEvent: {
            "text": (1, singular, string, None),
            "type": (3, singular, enum, f"{o}.ConversationEvent.ConversationEventType"),
        },
        chat_pb2.ConversationTurnKey: {
            "session_id": (1, singular, string, None),
            "conversation_id": (2, singular, string, None),
            "observed_field_3": (3, singular, int32, None),
        },
        chat_pb2.ObjectId: {"id": (1, singular, string, None)},
        chat_pb2.Range: {
            "start_index": (2, singular, int32, None),
            "end_index": (3, singular, int32, None),
        },
        chat_pb2.AnnotationMapEntry: {
            "object_id": (1, singular, message, f"{o}.ObjectId"),
            "content_range": (2, singular, message, f"{o}.Range"),
        },
        chat_pb2.FontInfo: {
            "font_family": (1, singular, string, None),
            "weight": (2, singular, int32, None),
            "font_size_pt": (3, singular, double, None),
        },
        chat_pb2.Color: {
            "red": (1, singular, int32, None),
            "green": (2, singular, int32, None),
            "blue": (3, singular, int32, None),
        },
        chat_pb2.TextStyle: {
            "bold": (1, singular, boolean, None),
            "italic": (2, singular, boolean, None),
            "underline": (3, singular, boolean, None),
            "url": (4, singular, string, None),
            "font_info": (5, singular, message, f"{o}.FontInfo"),
            "text_color": (6, singular, message, f"{o}.Color"),
            "background_color": (7, singular, message, f"{o}.Color"),
            "code": (8, singular, boolean, None),
            "strikethrough": (9, singular, boolean, None),
            "math": (10, singular, enum, f"{o}.MathStyleType"),
        },
        chat_pb2.TextRun: {
            "content": (1, singular, string, None),
            "text_style": (2, singular, message, f"{o}.TextStyle"),
        },
        chat_pb2.Image: {
            "url": (1, singular, string, None),
            "source_image_id": (3, singular, string, None),
            "block_reason": (4, singular, int32, None),
        },
        chat_pb2.Resource: {"id": (1, singular, string, None)},
        chat_pb2.ParagraphElement: {
            "start_index": (1, singular, int32, None),
            "end_index": (2, singular, int32, None),
            "text_run": (3, singular, message, f"{o}.TextRun"),
            "image": (4, singular, message, f"{o}.Image"),
            "resource": (5, singular, message, f"{o}.Resource"),
        },
        chat_pb2.BulletInfo: {
            "nesting_level": (3, singular, int32, None),
            "glyph": (101, singular, string, None),
            "list_type": (102, singular, enum, f"{o}.ListType"),
            "ordinal": (103, singular, int32, None),
            "absolute_ordinal": (104, singular, int32, None),
        },
        chat_pb2.ParagraphStyle: {
            "named_style_type": (2, singular, enum, f"{o}.NamedStyleType"),
        },
        chat_pb2.Paragraph: {
            "elements": (1, repeated, message, f"{o}.ParagraphElement"),
            "paragraph_style": (2, singular, message, f"{o}.ParagraphStyle"),
            "bullet_info": (4, singular, message, f"{o}.BulletInfo"),
        },
        chat_pb2.TableCell: {
            "start_index": (1, singular, int32, None),
            "end_index": (2, singular, int32, None),
            "content": (3, repeated, message, f"{o}.StructuralElement"),
        },
        chat_pb2.TableRow: {
            "start_index": (1, singular, int32, None),
            "end_index": (2, singular, int32, None),
            "table_cells": (3, repeated, message, f"{o}.TableCell"),
        },
        chat_pb2.Table: {
            "rows": (1, singular, int32, None),
            "columns": (2, singular, int32, None),
            "table_rows": (3, repeated, message, f"{o}.TableRow"),
        },
        chat_pb2.CodeBlock: {
            "content": (1, singular, string, None),
            "language_hint": (2, singular, string, None),
        },
        chat_pb2.A2uiBlock: {"json": (1, singular, string, None)},
        chat_pb2.Thought: {
            "elements": (1, repeated, message, f"{o}.StructuralElement"),
        },
        chat_pb2.HorizontalRule: {},
        agency_pb2.TailwindValue: {
            "number_value": (2, singular, double, None),
            "string_value": (3, singular, string, None),
            "bool_value": (4, singular, boolean, None),
        },
        agency_pb2.TailwindStructEntry: {
            "key": (1, singular, string, None),
            "value": (2, singular, message, f"{AGENCY_PACKAGE}.TailwindValue"),
        },
        agency_pb2.TailwindStruct: {
            "fields": (
                1,
                repeated,
                message,
                f"{AGENCY_PACKAGE}.TailwindStructEntry",
            ),
        },
        agency_pb2.FunctionCall: {
            "name": (1, singular, string, None),
            "args": (2, singular, message, f"{AGENCY_PACKAGE}.TailwindStruct"),
        },
        agency_pb2.FunctionResponse: {
            "name": (1, singular, string, None),
        },
        chat_pb2.StructuralElement: {
            "start_index": (1, singular, int32, None),
            "end_index": (2, singular, int32, None),
            "paragraph": (3, singular, message, f"{o}.Paragraph"),
            "table": (5, singular, message, f"{o}.Table"),
            "image": (6, singular, message, f"{o}.Image"),
            "code_block": (7, singular, message, f"{o}.CodeBlock"),
            "a2ui_block": (8, singular, message, f"{o}.A2uiBlock"),
            "thought": (9, singular, message, f"{o}.Thought"),
            "function_call": (10, singular, message, f"{AGENCY_PACKAGE}.FunctionCall"),
            "function_response": (11, singular, message, f"{AGENCY_PACKAGE}.FunctionResponse"),
            "horizontal_rule": (12, singular, message, f"{o}.HorizontalRule"),
        },
        chat_pb2.Body: {
            "content": (1, repeated, message, f"{o}.StructuralElement"),
            "inline_object_locations": (
                2,
                repeated,
                message,
                f"{o}.AnnotationMapEntry",
            ),
        },
        chat_pb2.TailwindDocFragment: {
            "elements": (1, repeated, message, f"{o}.StructuralElement"),
        },
        chat_pb2.SourceRevision: {
            "source": (1, singular, message, f"{o}.SourceId"),
        },
        chat_pb2.CitationSource: {
            "ingested_source": (1, singular, message, f"{o}.SourceRevision"),
        },
        chat_pb2.Citation: {
            "ranges": (4, repeated, message, f"{o}.Range"),
            "fragment": (5, singular, message, f"{o}.TailwindDocFragment"),
            "source_attribution": (6, singular, message, f"{o}.CitationSource"),
            "object_id": (7, singular, message, f"{o}.ObjectId"),
        },
        chat_pb2.DocumentObject: {
            "object_id": (1, singular, message, f"{o}.ObjectId"),
            "citation": (2, singular, message, f"{o}.Citation"),
        },
        chat_pb2.TailwindDoc: {
            "body": (1, singular, message, f"{o}.Body"),
            "objects": (4, repeated, message, f"{o}.DocumentObject"),
            "type": (5, singular, enum, f"{o}.ResponseType"),
        },
        chat_pb2.AnswerResponse: {
            "response": (1, singular, string, None),
            "conversation_turn_key": (3, singular, message, f"{o}.ConversationTurnKey"),
            "empty_answer_reason": (4, singular, enum, f"{o}.EmptyAnswerReason"),
            "response_doc": (5, singular, message, f"{o}.TailwindDoc"),
        },
        chat_pb2.ActOnSourcesResponse: {
            "response": (1, singular, message, f"{o}.AnswerResponse"),
            "next_step_suggestions": (
                6,
                singular,
                message,
                f"{o}.NextStepSuggestions",
            ),
        },
        chat_pb2.ActOnSourcesMindMapContext: {
            "key": (1, singular, string, None),
            "value": (2, singular, string, None),
        },
        chat_pb2.ActOnSourcesMindMapAction: {
            "action": (1, singular, string, None),
            "context": (2, repeated, message, f"{o}.ActOnSourcesMindMapContext"),
            "language": (3, singular, string, None),
        },
        chat_pb2.ActOnSourcesOptions: {
            "citation_content_type": (7, singular, enum, f"{o}.ContentType"),
            "answer_content_type": (10, singular, enum, f"{o}.ContentType"),
        },
        chat_pb2.FreeFormAction: {
            "user_query": (1, singular, string, None),
            "conversation_history": (2, repeated, message, f"{o}.ConversationEvent"),
            "user_message_id": (3, singular, string, None),
        },
        chat_pb2.InputSourceOptions: {
            "project_id": (1, singular, string, None),
            "use_all_sources": (2, singular, boolean, None),
        },
        chat_pb2.ActOnSourcesRequest: {
            "sources": (1, repeated, message, f"{o}.InputSource"),
            "options": (2, singular, message, f"{o}.ActOnSourcesOptions"),
            "free_form_action": (3, singular, message, f"{o}.FreeFormAction"),
            "mind_map_action": (6, singular, message, f"{o}.ActOnSourcesMindMapAction"),
            "source_options": (7, singular, message, f"{o}.InputSourceOptions"),
            "request_context": (
                8,
                singular,
                message,
                "labs.language.tailwind.common.protos.RequestContext",
            ),
            "chat_session_id": (10, singular, string, None),
            "origin": (11, singular, enum, f"{o}.QueryOrigin"),
        },
        chat_pb2.ChatHistoryMessage: {
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
        chat_pb2.ListChatSessionsRequest: {
            "project_id": (3, singular, string, None),
        },
        chat_pb2.ListChatSessionsResponse: {
            "sessions": (1, repeated, message, f"{COMMON_PACKAGE}.ChatSession"),
        },
        chat_pb2.GetChatSessionStatusRequest: {
            "request_context": (
                1,
                singular,
                message,
                "labs.language.tailwind.common.protos.RequestContext",
            ),
            "chat_session_id": (2, singular, string, None),
        },
        chat_pb2.GetChatSessionStatusResponse: {
            "generation_token": (1, singular, string, None),
            "status": (2, singular, int32, None),
        },
        chat_pb2.CancelGenerationRequest: {
            "request_context": (
                1,
                singular,
                message,
                "labs.language.tailwind.common.protos.RequestContext",
            ),
            "chat_session_id": (2, singular, string, None),
            "agency_session_id": (3, singular, string, None),
        },
        chat_pb2.CancelGenerationResponse: {},
        chat_pb2.ListChatTurnsRequest: {
            "chat_session_id": (4, singular, string, None),
            "page_token": (6, singular, string, None),
        },
        chat_pb2.ListChatTurnsResponse: {
            "chat_turns": (1, repeated, message, f"{o}.ChatHistoryMessage"),
            "next_page_token": (2, singular, string, None),
        },
        chat_pb2.NextStepSuggestionsRequest: {
            "project_id": (2, singular, string, None),
            "sources": (3, repeated, message, f"{o}.InputSource"),
        },
        chat_pb2.DeleteChatTurnsRequest: {
            "chat_session_id": (2, singular, string, None),
            "delete_all_history": (4, singular, boolean, None),
        },
        chat_pb2.GenerateFreeFormStreamedRequest: {
            "sources": (1, repeated, message, f"{o}.InputSource"),
            "user_query": (2, singular, string, None),
            "conversation_history": (3, repeated, message, f"{o}.ConversationEvent"),
            "request_context": (
                4,
                singular,
                message,
                "labs.language.tailwind.common.protos.RequestContext",
            ),
            "chat_session_id": (5, singular, string, None),
            "user_message_id": (6, singular, string, None),
            "project_id": (8, singular, string, None),
            "origin": (9, singular, enum, f"{o}.QueryOrigin"),
        },
        chat_pb2.GenerateFreeFormStreamedResponse: {
            "answer": (1, singular, message, f"{o}.AnswerResponse"),
            "is_final_response": (5, singular, boolean, None),
            "next_step_suggestions": (6, singular, message, f"{o}.NextStepSuggestions"),
        },
    }

    agency_message_types = {
        agency_pb2.TailwindValue,
        agency_pb2.TailwindStructEntry,
        agency_pb2.TailwindStruct,
        agency_pb2.FunctionCall,
        agency_pb2.FunctionResponse,
    }
    assert {
        message_type.DESCRIPTOR.name
        for message_type in expected
        if message_type is not sources_pb2.InputSource and message_type not in agency_message_types
    } == set(chat_pb2.DESCRIPTOR.message_types_by_name)
    assert {message_type.DESCRIPTOR.name for message_type in agency_message_types} == set(
        agency_pb2.DESCRIPTOR.message_types_by_name
    )
    for message_type, fields in expected.items():
        assert _field_shapes(message_type) == fields


def test_streamed_next_step_suggestions_round_trip_as_typed_bytes() -> None:
    response = chat_pb2.GenerateFreeFormStreamedResponse(
        next_step_suggestions=notebooks_pb2.NextStepSuggestions(
            next_steps=[notebooks_pb2.NextStep(suggestion="Follow up?", suggestion_type=99)]
        )
    )

    wire = response.SerializeToString(deterministic=True)
    decoded = chat_pb2.GenerateFreeFormStreamedResponse.FromString(wire)

    assert decoded.SerializeToString(deterministic=True) == wire
    assert [
        (next_step.suggestion, next_step.suggestion_type)
        for next_step in decoded.next_step_suggestions.next_steps
    ] == [("Follow up?", 99)]


def test_chat_enum_names_and_numbers_match_checked_in_evidence() -> None:
    assert {value.name: value.number for value in chat_pb2.QueryOrigin.DESCRIPTOR.values} == {
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
    assert {value.name: value.number for value in chat_pb2.EmptyAnswerReason.DESCRIPTOR.values} == {
        "EMPTY_ANSWER_REASON_UNKNOWN": 0,
        "UNANSWERABLE": 1,
        "FILTERED": 2,
    }
    nested = chat_pb2.ConversationEvent.ConversationEventType.DESCRIPTOR
    assert {value.name: value.number for value in nested.values} == {
        "EVENT_TYPE_UNKNOWN": 0,
        "USER_QUERY": 1,
        "GENERATED_RESPONSE": 2,
    }
    assert {value.name: value.number for value in chat_pb2.NamedStyleType.DESCRIPTOR.values} == {
        "NAMED_STYLE_TYPE_UNSPECIFIED": 0,
        "NORMAL_TEXT": 1,
        "TITLE": 2,
        "SUBTITLE": 3,
        "HEADING_1": 4,
        "HEADING_2": 5,
        "HEADING_3": 6,
        "HEADING_4": 7,
        "HEADING_5": 8,
        "HEADING_6": 9,
    }
    assert {value.name: value.number for value in chat_pb2.ListType.DESCRIPTOR.values} == {
        "LIST_TYPE_UNSPECIFIED": 0,
        "LIST_TYPE_UNORDERED": 1,
        "LIST_TYPE_ORDERED": 2,
    }
    assert {value.name: value.number for value in chat_pb2.ContentType.DESCRIPTOR.values} == {
        "CONTENT_TYPE_UNSPECIFIED": 0,
        "MARKDOWN": 1,
        "TAILWIND_DOC": 2,
        "HTML": 3,
    }
    assert {value.name: value.number for value in chat_pb2.MathStyleType.DESCRIPTOR.values} == {
        "MATH_STYLE_TYPE_UNSPECIFIED": 0,
        "MATH_STYLE_TYPE_INLINE": 1,
        "MATH_STYLE_TYPE_DISPLAY": 2,
    }
    assert {value.name: value.number for value in chat_pb2.ResponseType.DESCRIPTOR.values} == {
        "TYPE_UNSPECIFIED": 0,
        "TYPE_DEFAULT_ANSWER": 1,
        "TYPE_THOUGHT": 2,
    }


def test_chat_descriptor_fixture_matches_generated_file_descriptors() -> None:
    descriptor_set = descriptor_pb2.FileDescriptorSet.FromString(
        (FIXTURES / "android_descriptor_set.pb").read_bytes()
    )
    files = {file.name: file for file in descriptor_set.file}
    assert {
        "google/internal/labs/tailwind/orchestration/v1/read.proto",
        "google/internal/labs/tailwind/orchestration/v1/chat.proto",
        "google/internal/labs/tailwind/v1/source_settings.proto",
        "google/protobuf/timestamp.proto",
        "labs/language/tailwind/common/protos/common.proto",
        "labs/language/tailwind/common/protos/metadata.proto",
    } <= set(files)
    assert _without_implicit_json_names(files[chat_pb2.DESCRIPTOR.name]) == (
        descriptor_pb2.FileDescriptorProto.FromString(chat_pb2.DESCRIPTOR.serialized_pb)
    )
    assert _without_implicit_json_names(files[common_pb2.DESCRIPTOR.name]) == (
        descriptor_pb2.FileDescriptorProto.FromString(common_pb2.DESCRIPTOR.serialized_pb)
    )


def test_checked_in_chat_wire_fixture_round_trips_without_unknown_semantics() -> None:
    fixture = json.loads((FIXTURES / "chat_wire.json").read_text(encoding="utf-8"))
    types = {
        "generate_request": chat_pb2.GenerateFreeFormStreamedRequest,
        "partial_frame": chat_pb2.GenerateFreeFormStreamedResponse,
        "final_frame": chat_pb2.GenerateFreeFormStreamedResponse,
        "history_response": chat_pb2.ListChatTurnsResponse,
        "sessions_response": chat_pb2.ListChatSessionsResponse,
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
        ("Cached answer.", chat_pb2.ConversationEvent.GENERATED_RESPONSE),
        ("Cached question?", chat_pb2.ConversationEvent.USER_QUERY),
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
