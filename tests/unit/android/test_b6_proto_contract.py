"""Descriptor and byte fixtures for the B6 notes/sharing overlays."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google.protobuf import descriptor_pb2, text_format
from google.protobuf.descriptor import FieldDescriptor

from notebooklm._android.codecs.notes import (
    build_create_note_request,
    build_mutate_note_request,
    decode_note_entries,
)
from notebooklm._android.codecs.sharing import decode_share_status
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    b6_notes_pb2,
)
from notebooklm._android.proto.notebooklm.android.wire.v1 import b6_sharing_pb2
from notebooklm.types import ShareAccess, ShareViewLevel

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "android"
ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"
LOCAL_WIRE_PACKAGE = "notebooklm.android.wire.v1"


def _field_shapes(message: Any) -> dict[str, tuple[int, bool, int, str | None]]:
    result = {}
    for field in message.DESCRIPTOR.fields:
        target = None
        if field.message_type is not None:
            target = field.message_type.full_name
        elif field.enum_type is not None:
            target = field.enum_type.full_name
        result[field.name] = (field.number, field.is_repeated, field.type, target)
    return result


def test_note_overlay_keeps_exact_package_fields_and_no_guessed_service_descriptor() -> None:
    assert b6_notes_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert b6_notes_pb2.DESCRIPTOR.services_by_name == {}
    assert _field_shapes(b6_notes_pb2.ProjectNote) == {
        "id": (1, False, FieldDescriptor.TYPE_STRING, None),
        "content": (2, False, FieldDescriptor.TYPE_STRING, None),
        "metadata": (
            3,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.NoteMetadata",
        ),
        "name": (5, False, FieldDescriptor.TYPE_STRING, None),
    }
    assert _field_shapes(b6_notes_pb2.NoteMetadata) == {
        "type": (1, False, FieldDescriptor.TYPE_ENUM, f"{ORCHESTRATION_PACKAGE}.NoteType"),
        "last_edit_timestamp": (
            3,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            "google.protobuf.Timestamp",
        ),
        "note_prompt_type": (
            4,
            False,
            FieldDescriptor.TYPE_ENUM,
            f"{ORCHESTRATION_PACKAGE}.NotePromptType",
        ),
    }
    assert _field_shapes(b6_notes_pb2.NoteOrStatus) == {
        "note": (2, False, FieldDescriptor.TYPE_MESSAGE, f"{ORCHESTRATION_PACKAGE}.ProjectNote")
    }
    assert 1 not in b6_notes_pb2.NoteOrStatus.DESCRIPTOR.fields_by_number
    assert 6 not in b6_notes_pb2.ProjectNote.DESCRIPTOR.fields_by_number


def test_note_enums_are_exhaustive() -> None:
    assert {value.name: value.number for value in b6_notes_pb2.NoteType.DESCRIPTOR.values} == {
        "NOTE_TYPE_UNSPECIFIED": 0,
        "USER_WRITTEN": 1,
        "SAVED_RESPONSE": 2,
        "CUSTOM": 3,
    }
    assert {
        value.name: value.number for value in b6_notes_pb2.NotePromptType.DESCRIPTOR.values
    } == {
        "NOTE_PROMPT_TYPE_UNSPECIFIED": 0,
        "STUDY_GUIDE": 1,
        "BRIEFING_DOC": 2,
        "FAQ": 3,
        "TIMELINE": 4,
        "MIND_MAP": 5,
    }


def test_local_sharing_overlay_exposes_only_byte_proven_fields() -> None:
    assert b6_sharing_pb2.DESCRIPTOR.package == LOCAL_WIRE_PACKAGE
    assert b6_sharing_pb2.DESCRIPTOR.services_by_name == {}
    assert _field_shapes(b6_sharing_pb2.GetProjectDetailsResponse) == {
        "public_settings": (
            2,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{LOCAL_WIRE_PACKAGE}.ProjectPublicSettings",
        ),
        "max_individuals_share_limit": (3, False, FieldDescriptor.TYPE_INT32, None),
        "is_public_sharing_allowed": (4, False, FieldDescriptor.TYPE_BOOL, None),
    }
    assert 1 not in b6_sharing_pb2.GetProjectDetailsResponse.DESCRIPTOR.fields_by_number
    assert 7 not in b6_sharing_pb2.GetProjectDetailsResponse.DESCRIPTOR.fields_by_number
    assert 8 not in b6_sharing_pb2.GetProjectDetailsResponse.DESCRIPTOR.fields_by_number


def test_request_wire_fixture_pins_all_populated_fields() -> None:
    expected = json.loads((FIXTURES / "b6_request_wires.json").read_text(encoding="utf-8"))
    messages = {
        "get_notes": b6_notes_pb2.GetNotesRequest(project_id="project-1"),
        "create_note": build_create_note_request(
            "project-1",
            title="Pinned title",
            content="Pinned body",
            note_type=b6_notes_pb2.USER_WRITTEN,
        ),
        "mutate_note": build_mutate_note_request(
            "project-1",
            "note-1",
            title="Edited title",
            content="Edited body",
        ),
        "delete_notes": b6_notes_pb2.DeleteNotesRequest(
            project_id="project-1", note_ids=["note-1"]
        ),
        "get_project_details": b6_sharing_pb2.GetProjectDetailsRequest(project_id="project-1"),
        "share_project_public": b6_sharing_pb2.ShareProjectRequest(
            project=[
                b6_sharing_pb2.ShareProjectRequest_ProjectToShare(
                    project_id="project-1",
                    public_document_settings=(
                        b6_sharing_pb2.ShareProjectRequest_PublicDocumentSettings(
                            is_publicly_readable=True,
                            is_discoverable=False,
                        )
                    ),
                )
            ]
        ),
        "share_project_private": b6_sharing_pb2.ShareProjectRequest(
            project=[
                b6_sharing_pb2.ShareProjectRequest_ProjectToShare(
                    project_id="project-1",
                    public_document_settings=(
                        b6_sharing_pb2.ShareProjectRequest_PublicDocumentSettings(
                            is_publicly_readable=False,
                            is_discoverable=False,
                        )
                    ),
                )
            ]
        ),
    }
    assert {
        name: list(message.SerializeToString(deterministic=True))
        for name, message in messages.items()
    } == expected


def test_response_textprotos_exercise_projection_and_presence() -> None:
    notes_response = text_format.Parse(
        (FIXTURES / "get_notes_response.textproto").read_text(encoding="utf-8"),
        b6_notes_pb2.GetNotesResponse(),
    )
    notes = decode_note_entries(notes_response, "project-1", method_id="fixture")
    assert [(note.id, note.title, note.content, note.created_at) for note in notes] == [
        ("note-1", "Pinned title", "Pinned body", None)
    ]

    sharing_response = text_format.Parse(
        (FIXTURES / "get_project_details_response.textproto").read_text(encoding="utf-8"),
        b6_sharing_pb2.GetProjectDetailsResponse(),
    )
    status = decode_share_status(sharing_response, "project-1")
    assert status.is_public is True
    assert status.access is ShareAccess.ANYONE_WITH_LINK
    assert status.view_level is ShareViewLevel.FULL_NOTEBOOK
    assert status.shared_users == []
    assert status.max_individuals_share_limit == 1000
    assert status.is_public_sharing_allowed is True


def test_cumulative_descriptor_fixture_contains_b1_and_b6_without_replacing_b1_fixture() -> None:
    descriptor_set = descriptor_pb2.FileDescriptorSet.FromString(
        (FIXTURES / "android_descriptor_set.pb").read_bytes()
    )
    names = {file.name for file in descriptor_set.file}
    assert {
        "google/internal/labs/tailwind/orchestration/v1/b1_read.proto",
        "google/internal/labs/tailwind/orchestration/v1/b6_notes.proto",
        "google/internal/labs/tailwind/v1/source_settings.proto",
        "google/protobuf/timestamp.proto",
        "notebooklm/android/wire/v1/b6_sharing.proto",
    } <= names
    assert (FIXTURES / "b1_descriptor_set.pb").is_file()
