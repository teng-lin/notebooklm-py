"""Exact-package, local-wire, and deterministic Android protobuf gates."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

from google.protobuf import descriptor_pb2, text_format
from google.protobuf.descriptor import FieldDescriptor

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    notebooks_pb2 as exact_notebooks_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
)
from notebooklm._android.proto.google.internal.labs.tailwind.v1 import source_settings_pb2
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import notebooks_pb2

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "android"
ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"
SETTINGS_PACKAGE = "google.internal.labs.tailwind.v1"
LOCAL_WIRE_PACKAGE = "notebooklm.internal.android.wire.v1"


def _field_shapes(message: Any) -> dict[str, tuple[int, bool, int, str | None]]:
    result: dict[str, tuple[int, bool, int, str | None]] = {}
    for field in message.DESCRIPTOR.fields:
        target = None
        if field.message_type is not None:
            target = field.message_type.full_name
        elif field.enum_type is not None:
            target = field.enum_type.full_name
        result[field.name] = (field.number, field.is_repeated, field.type, target)
    return result


def _enum_values(enum: Any) -> dict[str, int]:
    return {value.name: value.number for value in enum.DESCRIPTOR.values}


def _without_implicit_json_names(
    descriptor: descriptor_pb2.FileDescriptorProto,
) -> descriptor_pb2.FileDescriptorProto:
    """Normalize a protoc descriptor against the runtime's compact form."""
    normalized = descriptor_pb2.FileDescriptorProto()
    normalized.CopyFrom(descriptor)
    pending = list(normalized.message_type)
    while pending:
        message = pending.pop()
        pending.extend(message.nested_type)
        for field in message.field:
            field.ClearField("json_name")
    return normalized


def test_exact_read_packages_and_imports_are_minimal() -> None:
    assert read_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert source_settings_pb2.DESCRIPTOR.package == SETTINGS_PACKAGE
    assert [dependency.name for dependency in read_pb2.DESCRIPTOR.dependencies] == [
        "google/internal/labs/tailwind/orchestration/v1/account.proto",
        "google/internal/labs/tailwind/v1/source_settings.proto",
        "google/protobuf/timestamp.proto",
        "labs/language/tailwind/common/protos/common.proto",
    ]

    assert read_pb2.DESCRIPTOR.services_by_name == {}


def test_orchestration_message_fields_tags_types_and_cardinality_are_exhaustive() -> None:
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    boolean = FieldDescriptor.TYPE_BOOL
    double = FieldDescriptor.TYPE_DOUBLE
    message = FieldDescriptor.TYPE_MESSAGE
    enum = FieldDescriptor.TYPE_ENUM

    expected = {
        read_pb2.SourceId: {"id": (1, singular, string, None)},
        read_pb2.WebpageMetadata: {"url": (1, singular, string, None)},
        read_pb2.GoogleDocsSourceMetadata: {
            "document_id": (1, singular, string, None),
        },
        read_pb2.GoogleDriveSourceMetadata: {
            "document_id": (1, singular, string, None),
            "mime_type": (3, singular, string, None),
        },
        read_pb2.ExpertIntelligenceSourceMetadata: {
            "content_id": (1, singular, string, None),
            "title": (3, singular, string, None),
            "authors": (4, repeated, string, None),
            "thumbnail_image_url": (5, singular, string, None),
            "description": (6, singular, string, None),
            "field_type": (7, singular, double, None),
        },
        read_pb2.SourceMetadata: {
            "google_docs_metadata": (
                1,
                singular,
                message,
                f"{ORCHESTRATION_PACKAGE}.GoogleDocsSourceMetadata",
            ),
            "source_added_timestamp": (
                3,
                singular,
                message,
                "google.protobuf.Timestamp",
            ),
            "original_source_content_type": (
                5,
                singular,
                enum,
                f"{ORCHESTRATION_PACKAGE}.OriginalSourceContentType",
            ),
            "webpage_metadata": (
                8,
                singular,
                message,
                f"{ORCHESTRATION_PACKAGE}.WebpageMetadata",
            ),
            "google_drive_source_metadata": (
                10,
                singular,
                message,
                f"{ORCHESTRATION_PACKAGE}.GoogleDriveSourceMetadata",
            ),
            "expert_intelligence_source_metadata": (
                19,
                singular,
                message,
                f"{ORCHESTRATION_PACKAGE}.ExpertIntelligenceSourceMetadata",
            ),
        },
        read_pb2.Source: {
            "source_id": (1, singular, message, f"{ORCHESTRATION_PACKAGE}.SourceId"),
            "title": (2, singular, string, None),
            "metadata": (3, singular, message, f"{ORCHESTRATION_PACKAGE}.SourceMetadata"),
            "settings": (4, singular, message, f"{SETTINGS_PACKAGE}.SourceSettings"),
        },
        read_pb2.ProjectMetadata: {
            "user_role": (1, singular, enum, f"{ORCHESTRATION_PACKAGE}.ProjectRole"),
            "create_time": (9, singular, message, "google.protobuf.Timestamp"),
            "is_public": (13, singular, boolean, None),
            "audio_overview_artifact_ids": (17, repeated, string, None),
        },
        read_pb2.PremiumFeatureInfo: {
            "can_edit_advanced_settings": (1, singular, boolean, None),
            "can_edit_guidebook_config": (2, singular, boolean, None),
            "can_view_analytics": (3, singular, boolean, None),
        },
        read_pb2.Project: {
            "title": (1, singular, string, None),
            "sources": (2, repeated, message, f"{ORCHESTRATION_PACKAGE}.Source"),
            "id": (3, singular, string, None),
            "emoji": (4, singular, string, None),
            "metadata": (6, singular, message, f"{ORCHESTRATION_PACKAGE}.ProjectMetadata"),
            "premium_feature_info": (
                10,
                singular,
                message,
                f"{ORCHESTRATION_PACKAGE}.PremiumFeatureInfo",
            ),
            "project_tier_limits": (
                11,
                singular,
                message,
                f"{ORCHESTRATION_PACKAGE}.TierLimits",
            ),
            "chat_sessions": (
                12,
                repeated,
                message,
                "labs.language.tailwind.common.protos.ChatSession",
            ),
        },
        read_pb2.GetProjectRequest: {
            "project_id": (1, singular, string, None),
            "include_audio_overview_ids": (2, singular, boolean, None),
        },
        read_pb2.GetProjectResponse: {
            "project": (1, singular, message, f"{ORCHESTRATION_PACKAGE}.Project"),
        },
        read_pb2.ListRecentlyViewedProjectsRequest: {
            "include_own_projects": (2, singular, boolean, None),
            "include_audio_overview_ids": (3, singular, boolean, None),
        },
        read_pb2.ListRecentlyViewedProjectsResponse: {
            "projects": (1, repeated, message, f"{ORCHESTRATION_PACKAGE}.Project"),
        },
    }

    assert {message_type.DESCRIPTOR.name for message_type in expected} == set(
        read_pb2.DESCRIPTOR.message_types_by_name
    )
    for message_type, expected_fields in expected.items():
        assert _field_shapes(message_type) == expected_fields


def test_source_settings_has_only_fields_two_and_four() -> None:
    assert set(source_settings_pb2.DESCRIPTOR.message_types_by_name) == {"SourceSettings"}
    assert _field_shapes(source_settings_pb2.SourceSettings) == {
        "status": (
            2,
            False,
            FieldDescriptor.TYPE_ENUM,
            f"{SETTINGS_PACKAGE}.SourceStatus",
        ),
        "user_drive_source_status": (
            4,
            False,
            FieldDescriptor.TYPE_ENUM,
            f"{SETTINGS_PACKAGE}.UserDriveSourceStatus",
        ),
    }


def test_notebook_repository_local_wire_fields_are_exhaustive() -> None:
    assert notebooks_pb2.DESCRIPTOR.package == LOCAL_WIRE_PACKAGE
    assert not notebooks_pb2.DESCRIPTOR.services_by_name
    assert [dependency.name for dependency in notebooks_pb2.DESCRIPTOR.dependencies] == [
        "google/internal/labs/tailwind/orchestration/v1/account.proto",
        "google/internal/labs/tailwind/orchestration/v1/notebooks.proto",
        "google/internal/labs/tailwind/orchestration/v1/read.proto",
        "labs/language/tailwind/common/protos/common.proto",
        "labs/language/tailwind/common/protos/metadata.proto",
    ]

    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    int32 = FieldDescriptor.TYPE_INT32
    message = FieldDescriptor.TYPE_MESSAGE
    local = LOCAL_WIRE_PACKAGE
    expected = {
        notebooks_pb2.WireProjectChangeProperty: {
            "new_title": (2, singular, string, None),
            "new_emoji": (3, singular, string, None),
        },
        notebooks_pb2.WireProjectMutation: {
            "change_property": (
                4,
                singular,
                message,
                f"{local}.WireProjectChangeProperty",
            ),
            "advanced_settings": (
                8,
                singular,
                message,
                f"{local}.WireProjectAdvancedSettings",
            ),
            "change_view_level": (
                9,
                singular,
                message,
                f"{local}.WireProjectChangeViewLevel",
            ),
        },
        notebooks_pb2.WireProjectViewLevel: {
            "level": (1, singular, int32, None),
        },
        notebooks_pb2.WireProjectChangeViewLevel: {
            "view_level": (
                1,
                singular,
                message,
                f"{local}.WireProjectViewLevel",
            ),
        },
        notebooks_pb2.WireProjectGoalSettings: {
            "goal": (1, singular, int32, None),
            "custom_prompt": (2, singular, string, None),
        },
        notebooks_pb2.WireProjectResponseStyleSettings: {
            "response_length": (1, singular, int32, None),
        },
        notebooks_pb2.WireProjectAdvancedSettings: {
            "goal_settings": (
                1,
                singular,
                message,
                f"{local}.WireProjectGoalSettings",
            ),
            "response_style_settings": (
                2,
                singular,
                message,
                f"{local}.WireProjectResponseStyleSettings",
            ),
        },
        notebooks_pb2.WireMutateProjectRequest: {
            "project_id": (1, singular, string, None),
            "mutations": (2, repeated, message, f"{local}.WireProjectMutation"),
            "request_context": (
                3,
                singular,
                message,
                "labs.language.tailwind.common.protos.RequestContext",
            ),
        },
        notebooks_pb2.WireProjectWithAdvancedSettings: {
            "title": (1, singular, string, None),
            "sources": (
                2,
                repeated,
                message,
                f"{ORCHESTRATION_PACKAGE}.Source",
            ),
            "id": (3, singular, string, None),
            "emoji": (4, singular, string, None),
            "metadata": (
                6,
                singular,
                message,
                f"{ORCHESTRATION_PACKAGE}.ProjectMetadata",
            ),
            "advanced_settings": (
                8,
                singular,
                message,
                f"{local}.WireProjectAdvancedSettings",
            ),
            "view_level": (
                9,
                singular,
                message,
                f"{local}.WireProjectViewLevel",
            ),
            "premium_feature_info": (
                10,
                singular,
                message,
                f"{ORCHESTRATION_PACKAGE}.PremiumFeatureInfo",
            ),
            "project_tier_limits": (
                11,
                singular,
                message,
                f"{ORCHESTRATION_PACKAGE}.TierLimits",
            ),
            "chat_sessions": (
                12,
                repeated,
                message,
                "labs.language.tailwind.common.protos.ChatSession",
            ),
        },
        notebooks_pb2.WireGetProjectResponse: {
            "project": (
                1,
                singular,
                message,
                f"{local}.WireProjectWithAdvancedSettings",
            ),
        },
        notebooks_pb2.WireSuggestedTopic: {
            "question": (1, singular, string, None),
            "prompt": (2, singular, string, None),
        },
        notebooks_pb2.WireSuggestedTopics: {
            "topics": (1, repeated, message, f"{local}.WireSuggestedTopic"),
        },
        notebooks_pb2.WireNotebookGuide: {
            "summary": (1, singular, message, f"{ORCHESTRATION_PACKAGE}.NotebookSummary"),
            "suggested_topics": (2, singular, message, f"{local}.WireSuggestedTopics"),
        },
        notebooks_pb2.WireGenerateNotebookGuideResponse: {
            "notebook_guide": (1, singular, message, f"{local}.WireNotebookGuide"),
        },
    }

    assert {message_type.DESCRIPTOR.name for message_type in expected} == set(
        notebooks_pb2.DESCRIPTOR.message_types_by_name
    )
    for message_type, expected_fields in expected.items():
        assert _field_shapes(message_type) == expected_fields


def test_exact_notebook_operation_fields_are_pinned_separately_from_local_overrides() -> None:
    package = ORCHESTRATION_PACKAGE
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    message = FieldDescriptor.TYPE_MESSAGE

    assert exact_notebooks_pb2.DESCRIPTOR.package == package
    assert exact_notebooks_pb2.DESCRIPTOR.services_by_name == {}
    assert set(exact_notebooks_pb2.DESCRIPTOR.message_types_by_name) == {
        "CreateProjectRequest",
        "CopyProjectRequest",
        "DeleteProjectsRequest",
        "RemoveRecentlyViewedProjectRequest",
        "GeneratePromptSuggestionsRequest",
        "GeneratePromptSuggestionsResponse",
        "GenerateNotebookGuideRequest",
        "GenerateNotebookGuideResponse",
        "MutateProjectRequest",
        "NextStep",
        "NextStepSuggestions",
        "NotebookGuide",
        "NotebookSummary",
        "PromptSuggestion",
        "ProjectMutation",
    }
    assert _field_shapes(exact_notebooks_pb2.CreateProjectRequest) == {
        "name": (1, singular, string, None)
    }
    assert _field_shapes(exact_notebooks_pb2.DeleteProjectsRequest) == {
        "project_ids": (1, repeated, string, None)
    }
    assert _field_shapes(exact_notebooks_pb2.RemoveRecentlyViewedProjectRequest) == {
        "project_id": (1, singular, string, None),
        "request_context": (
            2,
            singular,
            message,
            "labs.language.tailwind.common.protos.RequestContext",
        ),
    }
    assert _field_shapes(exact_notebooks_pb2.CopyProjectRequest) == {
        "request_context": (
            1,
            singular,
            message,
            "labs.language.tailwind.common.protos.RequestContext",
        ),
        "source_project_id": (2, singular, string, None),
        "title": (3, singular, string, None),
    }
    assert _field_shapes(exact_notebooks_pb2.PromptSuggestion) == {
        "title": (1, singular, string, None),
        "prompt": (2, singular, string, None),
    }
    assert _field_shapes(exact_notebooks_pb2.GeneratePromptSuggestionsRequest) == {
        "request_context": (
            1,
            singular,
            message,
            "labs.language.tailwind.common.protos.RequestContext",
        ),
        "project_id": (2, singular, string, None),
        "source_ids": (3, repeated, message, f"{package}.SourceId"),
        "config_id": (4, singular, FieldDescriptor.TYPE_ENUM, f"{package}.SuggestionConfigId"),
        "query": (6, singular, string, None),
    }
    assert _field_shapes(exact_notebooks_pb2.GeneratePromptSuggestionsResponse) == {
        "suggestions": (1, repeated, message, f"{package}.PromptSuggestion")
    }
    assert _field_shapes(exact_notebooks_pb2.MutateProjectRequest) == {
        "project_id": (1, singular, string, None),
        "mutations": (2, repeated, message, f"{package}.ProjectMutation"),
    }
    assert _field_shapes(exact_notebooks_pb2.ProjectMutation.ChangePropertyMutation) == {
        "new_title": (2, singular, string, None)
    }
    assert _field_shapes(exact_notebooks_pb2.ProjectMutation) == {
        "change_property": (
            4,
            singular,
            message,
            f"{package}.ProjectMutation.ChangePropertyMutation",
        )
    }
    assert _field_shapes(exact_notebooks_pb2.NextStep) == {
        "suggestion": (1, singular, string, None),
        "suggestion_type": (
            2,
            singular,
            FieldDescriptor.TYPE_ENUM,
            f"{package}.MagicArtifactType",
        ),
    }
    assert _field_shapes(exact_notebooks_pb2.NextStepSuggestions) == {
        "next_steps": (1, repeated, message, f"{package}.NextStep")
    }
    assert _field_shapes(exact_notebooks_pb2.NotebookGuide) == {
        "summary": (1, singular, message, f"{package}.NotebookSummary"),
        "next_step_suggestions": (6, singular, message, f"{package}.NextStepSuggestions"),
    }
    assert _field_shapes(exact_notebooks_pb2.GenerateNotebookGuideRequest) == {
        "project_id": (1, singular, string, None)
    }
    assert _field_shapes(exact_notebooks_pb2.GenerateNotebookGuideResponse) == {
        "notebook_guide": (1, singular, message, f"{package}.NotebookGuide")
    }


def test_notebook_wire_messages_match_captured_serialization() -> None:
    create = exact_notebooks_pb2.CreateProjectRequest(name="Title")
    delete = exact_notebooks_pb2.DeleteProjectsRequest(project_ids=["id-1"])
    mutate = notebooks_pb2.WireMutateProjectRequest(
        project_id="p",
        mutations=[
            notebooks_pb2.WireProjectMutation(
                change_property=notebooks_pb2.WireProjectChangeProperty(new_title="T")
            )
        ],
    )
    copy = exact_notebooks_pb2.CopyProjectRequest(source_project_id="p", title="Title")
    guide = exact_notebooks_pb2.GenerateNotebookGuideRequest(project_id="p")
    chat_settings = notebooks_pb2.WireProjectAdvancedSettings(
        goal_settings=notebooks_pb2.WireProjectGoalSettings(
            goal=2,
            custom_prompt="Be exact.",
        ),
        response_style_settings=notebooks_pb2.WireProjectResponseStyleSettings(response_length=4),
    )
    chat_mutation = notebooks_pb2.WireMutateProjectRequest(
        project_id="notebook-1",
        mutations=[notebooks_pb2.WireProjectMutation(advanced_settings=chat_settings)],
    )
    chat_response = notebooks_pb2.WireGetProjectResponse(
        project=notebooks_pb2.WireProjectWithAdvancedSettings(advanced_settings=chat_settings)
    )

    assert create.SerializeToString(deterministic=True).hex() == "0a055469746c65"
    assert delete.SerializeToString(deterministic=True).hex() == "0a0469642d31"
    assert mutate.SerializeToString(deterministic=True).hex() == "0a017012052203120154"
    assert copy.SerializeToString(deterministic=True).hex() == "1201701a055469746c65"
    assert guide.SerializeToString(deterministic=True).hex() == "0a0170"
    assert chat_mutation.SerializeToString(deterministic=True).hex() == (
        "0a0a6e6f7465626f6f6b2d31121542130a0d0802120942652065786163742e12020804"
    )
    assert chat_response.SerializeToString(deterministic=True).hex() == (
        "0a1542130a0d0802120942652065786163742e12020804"
    )


def test_all_reachable_enum_names_and_numbers_are_exact() -> None:
    assert _enum_values(exact_notebooks_pb2.SuggestionConfigId) == {
        "SUGGESTION_CONFIG_UNSPECIFIED": 0,
        "SUGGESTION_CONFIG_AUDIO_OVERVIEW_DEEP_DIVE": 1,
        "SUGGESTION_CONFIG_AUDIO_OVERVIEW_BRIEF": 2,
        "SUGGESTION_CONFIG_VIDEO_OVERVIEW_EXPLAINER": 3,
        "SUGGESTION_CONFIG_VIDEO_OVERVIEW_BRIEF": 4,
        "SUGGESTION_CONFIG_AUDIO_OVERVIEW_CRITIQUE": 5,
        "SUGGESTION_CONFIG_AUDIO_OVERVIEW_DEBATE": 6,
        "SUGGESTION_CONFIG_VIDEO_OVERVIEW_BREAKDOWN": 7,
        "SUGGESTION_CONFIG_QUIZ": 8,
        "SUGGESTION_CONFIG_FLASHCARDS": 9,
        "SUGGESTION_CONFIG_VIDEO_OVERVIEW_SHORT": 10,
    }
    assert _enum_values(exact_notebooks_pb2.MagicArtifactType) == {
        "MAGIC_ARTIFACT_TYPE_UNSPECIFIED": 0,
        "MINDMAP": 1,
        "AUDIO_OVERVIEW": 2,
        "VIDEO_OVERVIEW": 3,
        "NOTE": 4,
        "TABLE": 5,
        "LINE_CHART": 6,
        "FLASHCARDS": 7,
        "REPORT": 8,
        "CONVERSATIONAL_TEXT_CHIP": 9,
        "VIDEO_OVERVIEW_TEXT_CHIP": 10,
        "AUDIO_OVERVIEW_TEXT_CHIP": 11,
        "REPORT_TEXT_CHIP": 12,
        "FLASHCARDS_TEXT_CHIP": 13,
        "QUIZ_TEXT_CHIP": 14,
        "SOURCE_DISCOVERY_TEXT_CHIP": 15,
    }
    assert _enum_values(read_pb2.ProjectRole) == {
        "PROJECT_ROLE_UNKNOWN": 0,
        "PROJECT_ROLE_OWNER": 1,
        "PROJECT_ROLE_WRITER": 2,
        "PROJECT_ROLE_READER": 3,
        "PROJECT_ROLE_NOT_SHARED": 4,
    }
    assert _enum_values(read_pb2.OriginalSourceContentType) == {
        "SOURCE_CONTENT_TYPE_UNKNOWN": 0,
        "SOURCE_CONTENT_TYPE_GOOGLE_DOC": 1,
        "SOURCE_CONTENT_TYPE_GOOGLE_SLIDES": 2,
        "SOURCE_CONTENT_TYPE_PDF": 3,
        "SOURCE_CONTENT_TYPE_TEXT": 4,
        "SOURCE_CONTENT_TYPE_URL": 5,
        "SOURCE_CONTENT_TYPE_POWERPOINT": 6,
        "SOURCE_CONTENT_TYPE_GOOGLE_SHEET": 7,
        "SOURCE_CONTENT_TYPE_MARKDOWN": 8,
        "SOURCE_CONTENT_TYPE_YOUTUBE_VIDEO": 9,
        "SOURCE_CONTENT_TYPE_AUDIO": 10,
        "SOURCE_CONTENT_TYPE_WORD": 11,
        "SOURCE_CONTENT_TYPE_EXCEL": 12,
        "SOURCE_CONTENT_TYPE_IMAGE": 13,
        "SOURCE_CONTENT_TYPE_DRIVE": 14,
        "SOURCE_CONTENT_TYPE_GMAIL": 15,
        "SOURCE_CONTENT_TYPE_CSV": 16,
        "SOURCE_CONTENT_TYPE_EPUB": 17,
        "SOURCE_CONTENT_TYPE_GEMINI_CHAT": 18,
        "SOURCE_CONTENT_TYPE_AI_MODE_CHAT": 19,
        "SOURCE_CONTENT_TYPE_EXPERT_INTELLIGENCE": 20,
    }
    assert _enum_values(source_settings_pb2.SourceStatus) == {
        "SOURCE_STATUS_UNSPECIFIED": 0,
        "SOURCE_STATUS_PENDING": 1,
        "SOURCE_STATUS_COMPLETE": 2,
        "SOURCE_STATUS_ERROR": 3,
        "SOURCE_STATUS_PENDING_DELETION": 4,
        "SOURCE_STATUS_TENTATIVE": 5,
    }
    assert _enum_values(source_settings_pb2.UserDriveSourceStatus) == {
        "DRIVE_SOURCE_STATUS_UNSPECIFIED": 0,
        "DRIVE_SOURCE_STATUS_INACCESSIBLE": 1,
        "DRIVE_SOURCE_STATUS_SYNCING": 2,
        "DRIVE_SOURCE_STATUS_ACTIVE": 3,
        "DRIVE_SOURCE_STATUS_DELETED": 4,
        "DRIVE_SOURCE_STATUS_GEN_AI_ACCESS_DENIED": 5,
    }


def test_request_wire_shapes_have_no_context_or_extra_fields() -> None:
    get_request = read_pb2.GetProjectRequest(
        project_id="project-1",
        include_audio_overview_ids=True,
    )
    list_request = read_pb2.ListRecentlyViewedProjectsRequest(
        include_own_projects=True,
        include_audio_overview_ids=True,
    )

    assert get_request.SerializeToString(deterministic=True).hex() == ("0a0970726f6a6563742d311001")
    assert list_request.SerializeToString(deterministic=True).hex() == "10011801"


def test_synthetic_get_project_textproto_exercises_read_projection() -> None:
    response = text_format.Parse(
        (FIXTURES / "get_project_response.textproto").read_text(encoding="utf-8"),
        read_pb2.GetProjectResponse(),
    )

    assert response.project.id == "00000000-0000-4000-8000-000000000000"
    assert len(response.project.sources) == 4
    assert response.project.sources[0].metadata.webpage_metadata.url == (
        "https://example.invalid/source"
    )
    assert response.project.sources[3].metadata.google_docs_metadata.document_id == (
        "synthetic-drive-document"
    )
    assert (
        response.project.sources[2].settings.status == source_settings_pb2.SOURCE_STATUS_TENTATIVE
    )
    assert response.project.sources[3].metadata.google_drive_source_metadata.document_id == (
        "synthetic-drive-document"
    )
    assert response.project.sources[3].settings.user_drive_source_status == (
        source_settings_pb2.DRIVE_SOURCE_STATUS_ACTIVE
    )
    assert response.project.metadata.create_time.nanos == 123000000
    assert response.project.DESCRIPTOR.fields_by_number[10].name == "premium_feature_info"
    assert response.project.DESCRIPTOR.fields_by_number[12].name == "chat_sessions"

    reparsed = read_pb2.GetProjectResponse.FromString(
        response.SerializeToString(deterministic=True)
    )
    assert reparsed == response


def test_synthetic_list_textproto_pins_repeated_project_cardinality() -> None:
    response = text_format.Parse(
        (FIXTURES / "list_recently_viewed_projects_response.textproto").read_text(encoding="utf-8"),
        read_pb2.ListRecentlyViewedProjectsResponse(),
    )

    assert [project.id for project in response.projects] == [
        "00000000-0000-4000-8000-000000000010",
        "00000000-0000-4000-8000-000000000012",
    ]
    assert response.projects[1].sources[0].metadata.original_source_content_type == (
        read_pb2.SOURCE_CONTENT_TYPE_TEXT
    )


def test_descriptor_fixture_matches_generated_file_descriptors() -> None:
    descriptor_set = descriptor_pb2.FileDescriptorSet.FromString(
        (FIXTURES / "read_descriptor_set.pb").read_bytes()
    )
    files = {file.name: file for file in descriptor_set.file}

    assert set(files) == {
        "google/internal/labs/tailwind/orchestration/v1/account.proto",
        "google/internal/labs/tailwind/orchestration/v1/read.proto",
        "google/internal/labs/tailwind/v1/source_settings.proto",
        "google/protobuf/timestamp.proto",
        "labs/language/tailwind/common/protos/common.proto",
        "labs/language/tailwind/common/protos/metadata.proto",
        "labs/language/tailwind/common/protos/provenance.proto",
    }
    assert _without_implicit_json_names(files[read_pb2.DESCRIPTOR.name]) == (
        descriptor_pb2.FileDescriptorProto.FromString(read_pb2.DESCRIPTOR.serialized_pb)
    )
    assert _without_implicit_json_names(files[source_settings_pb2.DESCRIPTOR.name]) == (
        descriptor_pb2.FileDescriptorProto.FromString(source_settings_pb2.DESCRIPTOR.serialized_pb)
    )


def test_current_descriptor_fixture_includes_notebook_local_wire_overlay() -> None:
    descriptor_set = descriptor_pb2.FileDescriptorSet.FromString(
        (FIXTURES / "android_descriptor_set.pb").read_bytes()
    )
    files = {file.name: file for file in descriptor_set.file}

    assert {
        "google/internal/labs/tailwind/orchestration/v1/read.proto",
        "google/internal/labs/tailwind/v1/source_settings.proto",
        "google/protobuf/timestamp.proto",
        "notebooklm/internal/android/wire/v1/notebooks.proto",
    } <= set(files)
    local = files[notebooks_pb2.DESCRIPTOR.name]
    assert local.package == LOCAL_WIRE_PACKAGE
    assert _without_implicit_json_names(local) == descriptor_pb2.FileDescriptorProto.FromString(
        notebooks_pb2.DESCRIPTOR.serialized_pb
    )


def test_locked_dependency_extras_keep_android_out_of_all() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]

    assert extras["android"] == [
        "grpcio==1.76.0",
        "protobuf==6.33.5",
        "gpsoauth>=1.1.0",
    ]
    assert "grpcio==1.76.0" in extras["dev"]
    assert "grpcio-tools==1.76.0" in extras["dev"]
    assert "protobuf==6.33.5" in extras["dev"]
    assert extras["all"] == ["notebooklm-py[browser,dev,headless,markdown,mcp,server]"]


def test_checked_in_generated_tree_and_descriptor_regenerate_byte_for_byte() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/regenerate_android_protos.py", "--check"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "descriptors and generated tree are deterministic" in result.stdout


def test_proto_regenerator_uses_portable_descriptor_relative_input_paths() -> None:
    script = REPO_ROOT / "scripts" / "regenerate_android_protos.py"
    spec = importlib.util.spec_from_file_location("regenerate_android_protos_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._protoc_input_paths(
        [
            PureWindowsPath(r"labs\language\tailwind\sharing\sharing.proto"),
            PureWindowsPath(r"google\internal\labs\tailwind\orchestration\v1\account.proto"),
        ]
    ) == [
        "google/internal/labs/tailwind/orchestration/v1/account.proto",
        "labs/language/tailwind/sharing/sharing.proto",
    ]
