"""Exact-package descriptor and projection gates for the artifact closure."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

import pytest
from google.protobuf import descriptor_pb2, text_format
from google.protobuf.descriptor import FieldDescriptor

from notebooklm._android.artifacts import (
    CREATE_ARTIFACT_METHOD,
    DELETE_ARTIFACT_METHOD,
    DERIVE_ARTIFACT_METHOD,
    EXPORT_TO_DRIVE_METHOD,
    GENERATE_ARTIFACT_METHOD,
    GENERATE_REPORT_SUGGESTIONS_METHOD,
    GET_ARTIFACT_METHOD,
    LIST_ARTIFACTS_METHOD,
    UPDATE_ARTIFACT_METHOD,
)
from notebooklm._android.codecs.artifacts import decode_artifact, decode_artifacts
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    artifacts_pb2,
)
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import artifacts_pb2 as wire_pb2
from notebooklm._types.artifact_content import (
    ArtifactMediaType,
    AudioArtifactUserState,
    FlashcardArtifactUserState,
)
from notebooklm.exceptions import DecodingError
from notebooklm.types import ArtifactType

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "android"
ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"


def _shapes(message: Any) -> dict[str, tuple[int, bool, int, str | None]]:
    result: dict[str, tuple[int, bool, int, str | None]] = {}
    for field in message.DESCRIPTOR.fields:
        target = None
        if field.message_type is not None:
            target = field.message_type.full_name
        elif field.enum_type is not None:
            target = field.enum_type.full_name
        result[field.name] = (field.number, field.is_repeated, field.type, target)
    return result


def _expected(
    *items: tuple[str, int, bool, int, str | None],
) -> dict[str, tuple[int, bool, int, str | None]]:
    return {
        name: (number, repeated, kind, target) for name, number, repeated, kind, target in items
    }


def _enum_values(enum: Any) -> dict[str, int]:
    return {value.name: value.number for value in enum.DESCRIPTOR.values}


def test_generated_package_contains_report_suggestions_without_local_overlay() -> None:
    assert artifacts_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert list(artifacts_pb2.DESCRIPTOR.services_by_name) == []
    assert hasattr(artifacts_pb2, "GenerateReportSuggestionsRequest")
    assert hasattr(artifacts_pb2, "GenerateReportSuggestionsResponse")


def test_full_method_paths_are_exact_and_local_overlay_does_not_claim_a_service() -> None:
    prefix = f"/{ORCHESTRATION_PACKAGE}.LabsTailwindOrchestrationService/"
    assert f"{prefix}ListArtifacts" == LIST_ARTIFACTS_METHOD
    assert f"{prefix}GetArtifact" == GET_ARTIFACT_METHOD
    assert f"{prefix}CreateArtifact" == CREATE_ARTIFACT_METHOD
    assert f"{prefix}DeriveArtifact" == DERIVE_ARTIFACT_METHOD
    assert f"{prefix}UpdateArtifact" == UPDATE_ARTIFACT_METHOD
    assert f"{prefix}DeleteArtifact" == DELETE_ARTIFACT_METHOD
    assert f"{prefix}GenerateReportSuggestions" == GENERATE_REPORT_SUGGESTIONS_METHOD
    assert f"{prefix}GenerateArtifact" == GENERATE_ARTIFACT_METHOD
    assert f"{prefix}ExportToDrive" == EXPORT_TO_DRIVE_METHOD


def test_artifact_request_response_fields_are_exhaustive() -> None:
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    integer = FieldDescriptor.TYPE_INT32
    message = FieldDescriptor.TYPE_MESSAGE

    expected = {
        artifacts_pb2.CreateArtifactRequest: _expected(
            ("project_id", 2, singular, string, None),
            ("artifact", 3, singular, message, f"{ORCHESTRATION_PACKAGE}.Artifact"),
        ),
        artifacts_pb2.CreateArtifactResponse: _expected(
            ("artifact", 1, singular, message, f"{ORCHESTRATION_PACKAGE}.Artifact"),
        ),
        artifacts_pb2.GenerateArtifactRequest: _expected(
            (
                "request_context",
                1,
                singular,
                message,
                "labs.language.tailwind.common.protos.RequestContext",
            ),
            ("artifact_id", 2, singular, string, None),
        ),
        artifacts_pb2.GenerateArtifactResponse: _expected(
            ("artifact", 1, singular, message, f"{ORCHESTRATION_PACKAGE}.Artifact"),
        ),
        artifacts_pb2.ExportToDriveRequest: _expected(
            (
                "request_context",
                1,
                singular,
                message,
                "labs.language.tailwind.common.protos.RequestContext",
            ),
            ("artifact_id", 2, singular, string, None),
            ("content", 3, singular, string, None),
            ("title", 4, singular, string, None),
            ("destination", 5, singular, integer, None),
        ),
        artifacts_pb2.ExportToDriveResponse: _expected(
            ("url", 1, singular, string, None),
        ),
        artifacts_pb2.DeriveArtifactRequest: _expected(
            (
                "request_context",
                1,
                singular,
                message,
                "labs.language.tailwind.common.protos.RequestContext",
            ),
            ("original_artifact_id", 2, singular, string, None),
            (
                "slides_derivation_options",
                3,
                singular,
                message,
                f"{ORCHESTRATION_PACKAGE}.SlidesDerivationOptions",
            ),
        ),
        artifacts_pb2.DeriveArtifactResponse: _expected(
            ("artifact", 1, singular, message, f"{ORCHESTRATION_PACKAGE}.Artifact"),
        ),
        artifacts_pb2.GetArtifactRequest: _expected(
            ("artifact_id", 1, singular, string, None),
        ),
        artifacts_pb2.GetArtifactResponse: _expected(
            ("artifact", 1, singular, message, f"{ORCHESTRATION_PACKAGE}.Artifact"),
        ),
        artifacts_pb2.ListArtifactsRequest: _expected(
            ("project_id", 2, singular, string, None),
        ),
        artifacts_pb2.ListArtifactsResponse: _expected(
            ("artifacts", 1, repeated, message, f"{ORCHESTRATION_PACKAGE}.Artifact"),
        ),
        artifacts_pb2.UpdateArtifactRequest: _expected(
            ("artifact", 1, singular, message, f"{ORCHESTRATION_PACKAGE}.Artifact"),
            ("update_mask", 2, singular, message, "google.protobuf.FieldMask"),
            ("etag", 3, singular, string, None),
        ),
        artifacts_pb2.DeleteArtifactRequest: _expected(
            ("artifact_id", 2, singular, string, None),
        ),
    }
    for message_type, fields in expected.items():
        assert _shapes(message_type) == fields
    target = artifacts_pb2.ExportToDriveRequest.DESCRIPTOR.oneofs_by_name["target"]
    assert [field.name for field in target.fields] == [
        "artifact_id",
        "content",
    ]


def test_local_table_and_option_overlays_pin_live_unknown_fields_without_google_fqns() -> None:
    package = ORCHESTRATION_PACKAGE
    assert wire_pb2.DESCRIPTOR.package == "notebooklm.internal.android.wire.v1"
    assert _shapes(wire_pb2.WireTableArtifactGenerationOptions) == _expected(
        ("user_steering_prompt", 1, False, FieldDescriptor.TYPE_STRING, None),
        ("language_code", 2, False, FieldDescriptor.TYPE_STRING, None),
    )
    assert _shapes(wire_pb2.WireTableArtifact) == _expected(
        ("document", 1, False, FieldDescriptor.TYPE_MESSAGE, f"{package}.TailwindDoc"),
        (
            "generation_options",
            2,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            "notebooklm.internal.android.wire.v1.WireTableArtifactGenerationOptions",
        ),
    )
    assert _shapes(wire_pb2.WireArtifactTableProjection)["table"][0] == 19
    assert _shapes(wire_pb2.WireAudioOverviewGenerationOptionsProjection)["format"][0] == 7
    assert _shapes(wire_pb2.WireInfographicGenerationOptionsProjection)["detail_level"][0] == 5


def test_reachable_artifact_enum_names_and_numbers_are_exhaustive() -> None:
    assert _enum_values(artifacts_pb2.ArtifactType) == {
        "ARTIFACT_TYPE_UNKNOWN": 0,
        "ARTIFACT_TYPE_AUDIO_OVERVIEW": 1,
        "ARTIFACT_TYPE_TAILORED_REPORT": 2,
        "ARTIFACT_TYPE_EXPLAINER_VIDEO": 3,
        "ARTIFACT_TYPE_APP": 4,
        "ARTIFACT_TYPE_MINDMAP": 5,
        "ARTIFACT_TYPE_FANTASY_MAP": 6,
        "ARTIFACT_TYPE_INFOGRAPHIC": 7,
        "ARTIFACT_TYPE_SLIDES": 8,
        "ARTIFACT_TYPE_TABLE": 9,
        "ARTIFACT_TYPE_FILE": 10,
    }
    assert _enum_values(artifacts_pb2.ArtifactStatus) == {
        "ARTIFACT_STATUS_UNKNOWN": 0,
        "ARTIFACT_STATUS_INITIALIZED": 1,
        "ARTIFACT_STATUS_PROCESSING": 2,
        "ARTIFACT_STATUS_READY": 3,
        "ARTIFACT_STATUS_FAILED": 4,
        "ARTIFACT_STATUS_SUGGESTED": 5,
        "ARTIFACT_PENDING_REVIEW": 6,
    }
    assert _enum_values(artifacts_pb2.AppType) == {
        "APP_TYPE_UNSPECIFIED": 0,
        "APP_TYPE_FLASHCARDS": 1,
        "APP_TYPE_QUIZ": 2,
        "APP_TYPE_PROTOTYPE": 3,
        "APP_TYPE_MINDMAP": 4,
        "APP_TYPE_CANVAS": 5,
    }
    assert _enum_values(artifacts_pb2.EpisodeLength) == {
        "EPISODE_LENGTH_UNSPECIFIED": 0,
        "EPISODE_LENGTH_SHORT": 1,
        "EPISODE_LENGTH_MEDIUM": 2,
        "EPISODE_LENGTH_LONG": 3,
    }
    assert _enum_values(artifacts_pb2.MediaStreamingType) == {
        "MEDIA_STREAMING_TYPE_UNSPECIFIED": 0,
        "MEDIA_STREAMING_TYPE_PROGRESSIVE_STREAMING": 1,
        "MEDIA_STREAMING_TYPE_ADAPTIVE_STREAMING_HLS": 2,
        "MEDIA_STREAMING_TYPE_ADAPTIVE_STREAMING_DASH": 3,
        "MEDIA_STREAMING_TYPE_DOWNLOAD": 4,
    }
    assert _enum_values(artifacts_pb2.VideoOverviewTemplateFormat) == {
        "TEMPLATE_FORMAT_UNSPECIFIED": 0,
        "TEMPLATE_FORMAT_EXPLAINER": 1,
        "TEMPLATE_FORMAT_BRIEF": 2,
        "TEMPLATE_FORMAT_BREAKDOWN": 3,
        "TEMPLATE_FORMAT_SHORT": 4,
        "TEMPLATE_FORMAT_WHITEBOARD_ANIMATION": 5,
    }
    assert _enum_values(artifacts_pb2.VideoOverviewStyle) == {
        "VIDEO_OVERVIEW_STYLE_UNSPECIFIED": 0,
        "VIDEO_OVERVIEW_STYLE_AUTO_SELECT": 1,
        "VIDEO_OVERVIEW_STYLE_CLASSIC": 2,
        "VIDEO_OVERVIEW_STYLE_WHITEBOARD": 3,
        "VIDEO_OVERVIEW_STYLE_HERITAGE": 4,
        "VIDEO_OVERVIEW_STYLE_PAPERCRAFT": 5,
        "VIDEO_OVERVIEW_STYLE_WATERCOLOR": 6,
        "VIDEO_OVERVIEW_STYLE_ANIME": 7,
        "VIDEO_OVERVIEW_STYLE_RISOGRAPHIC": 8,
        "VIDEO_OVERVIEW_STYLE_KAWAII": 9,
    }
    assert _enum_values(artifacts_pb2.DeckType) == {
        "DECK_TYPE_UNSPECIFIED": 0,
        "DECK_TYPE_READING": 1,
        "DECK_TYPE_PRESENTATION": 2,
    }
    assert _enum_values(artifacts_pb2.SlideDeckLength) == {
        "SLIDE_DECK_LENGTH_UNSPECIFIED": 0,
        "SLIDE_DECK_LENGTH_DYNAMIC": 1,
        "SLIDE_DECK_LENGTH_SHORT": 2,
        "SLIDE_DECK_LENGTH_MEDIUM": 3,
        "SLIDE_DECK_LENGTH_LONG": 4,
    }
    assert _enum_values(artifacts_pb2.QuizGenerationOptions.QuestionQuantity) == {
        "QUESTION_QUANTITY_UNSPECIFIED": 0,
        "QUESTION_QUANTITY_FEWER": 1,
        "QUESTION_QUANTITY_STANDARD": 2,
        "QUESTION_QUANTITY_MORE": 3,
    }
    assert _enum_values(artifacts_pb2.QuizGenerationOptions.QuizDifficulty) == {
        "QUIZ_DIFFICULTY_UNSPECIFIED": 0,
        "QUIZ_DIFFICULTY_EASY": 1,
        "QUIZ_DIFFICULTY_MEDIUM": 2,
        "QUIZ_DIFFICULTY_HARD": 3,
    }
    assert _enum_values(artifacts_pb2.FlashcardsGenerationOptions.CardQuantity) == {
        "CARD_QUANTITY_UNSPECIFIED": 0,
        "CARD_QUANTITY_FEWER": 1,
        "CARD_QUANTITY_STANDARD": 2,
        "CARD_QUANTITY_MORE": 3,
    }
    assert _enum_values(artifacts_pb2.FlashcardsGenerationOptions.FlashcardsDifficulty) == {
        "FLASHCARDS_DIFFICULTY_UNSPECIFIED": 0,
        "FLASHCARDS_DIFFICULTY_EASY": 1,
        "FLASHCARDS_DIFFICULTY_MEDIUM": 2,
        "FLASHCARDS_DIFFICULTY_HARD": 3,
    }
    assert _enum_values(artifacts_pb2.InfographicGenerationOptions.AspectRatio) == {
        "ASPECT_RATIO_UNSPECIFIED": 0,
        "ASPECT_RATIO_LANDSCAPE": 1,
        "ASPECT_RATIO_PORTRAIT": 2,
        "ASPECT_RATIO_SQUARE": 3,
    }
    assert _enum_values(artifacts_pb2.InfographicGenerationOptions.InfographicStyle) == {
        "STYLE_UNSPECIFIED": 0,
        "STYLE_AUTO": 1,
        "STYLE_SKETCH_NOTE": 2,
        "STYLE_PROFESSIONAL": 3,
        "STYLE_BENTO_GRID": 4,
        "STYLE_EDITORIAL": 5,
        "STYLE_STORYBOARD": 6,
        "STYLE_BRICKS": 7,
        "STYLE_CLAYMATION": 8,
        "STYLE_ANIME": 9,
        "STYLE_KAWAII": 10,
        "STYLE_SCIENTIFIC": 11,
        "STYLE_ACADEMIC": 12,
    }


def test_artifact_projection_fields_are_exhaustive() -> None:
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    boolean = FieldDescriptor.TYPE_BOOL
    integer = FieldDescriptor.TYPE_INT32
    message = FieldDescriptor.TYPE_MESSAGE
    enum = FieldDescriptor.TYPE_ENUM
    duration = "google.protobuf.Duration"
    package = ORCHESTRATION_PACKAGE

    expected = {
        artifacts_pb2.MediaStreamingUrl: _expected(
            ("url", 1, singular, string, None),
            ("type", 2, singular, enum, f"{package}.MediaStreamingType"),
        ),
        artifacts_pb2.QuizGenerationOptions: _expected(
            (
                "question_quantity",
                1,
                singular,
                enum,
                f"{package}.QuizGenerationOptions.QuestionQuantity",
            ),
            (
                "quiz_difficulty",
                2,
                singular,
                enum,
                f"{package}.QuizGenerationOptions.QuizDifficulty",
            ),
        ),
        artifacts_pb2.FlashcardsGenerationOptions: _expected(
            (
                "card_quantity",
                1,
                singular,
                enum,
                f"{package}.FlashcardsGenerationOptions.CardQuantity",
            ),
            (
                "flashcards_difficulty",
                2,
                singular,
                enum,
                f"{package}.FlashcardsGenerationOptions.FlashcardsDifficulty",
            ),
        ),
        artifacts_pb2.AppArtifactGenerationOptions: _expected(
            ("app_type", 1, singular, enum, f"{package}.AppType"),
            ("free_text_steering_prompt", 3, singular, string, None),
            ("language_code", 4, singular, string, None),
            (
                "flashcards_generation_options",
                7,
                singular,
                message,
                f"{package}.FlashcardsGenerationOptions",
            ),
            (
                "quiz_generation_options",
                8,
                singular,
                message,
                f"{package}.QuizGenerationOptions",
            ),
        ),
        artifacts_pb2.TemplatizedApp: _expected(
            ("app_data", 1, singular, string, None),
        ),
        artifacts_pb2.AppArtifact: _expected(
            ("app_html", 1, singular, string, None),
            (
                "generation_options",
                2,
                singular,
                message,
                f"{package}.AppArtifactGenerationOptions",
            ),
            ("templatized_app", 3, singular, message, f"{package}.TemplatizedApp"),
            ("mind_map_json", 4, singular, string, None),
        ),
        artifacts_pb2.AudioOverviewGenerationOptions: _expected(
            ("episode_focus", 1, singular, string, None),
            ("episode_length", 2, singular, enum, f"{package}.EpisodeLength"),
            ("source_ids", 4, repeated, message, f"{package}.SourceId"),
            ("language_code", 5, singular, string, None),
        ),
        artifacts_pb2.AudioOverviewArtifact: _expected(
            (
                "generation_options",
                2,
                singular,
                message,
                f"{package}.AudioOverviewGenerationOptions",
            ),
            ("is_interactive", 5, singular, boolean, None),
            ("media_urls", 6, repeated, message, f"{package}.MediaStreamingUrl"),
            ("duration", 7, singular, message, duration),
        ),
        artifacts_pb2.ExplainerVideoGenerationOptions: _expected(
            ("source_ids", 1, repeated, message, f"{package}.SourceId"),
            ("language_code", 2, singular, string, None),
            ("video_focus", 3, singular, string, None),
            (
                "template_format",
                5,
                singular,
                enum,
                f"{package}.VideoOverviewTemplateFormat",
            ),
            (
                "video_overview_style",
                6,
                singular,
                enum,
                f"{package}.VideoOverviewStyle",
            ),
            ("style_prompt", 7, singular, string, None),
        ),
        artifacts_pb2.ExplainerVideoArtifact: _expected(
            (
                "generation_options",
                3,
                singular,
                message,
                f"{package}.ExplainerVideoGenerationOptions",
            ),
            ("media_urls", 5, repeated, message, f"{package}.MediaStreamingUrl"),
            ("duration", 6, singular, message, duration),
        ),
        artifacts_pb2.TailoredReportArtifactGenerationOptions: _expected(
            ("type", 1, singular, string, None),
            ("description", 2, singular, string, None),
            ("source_ids", 4, repeated, message, f"{package}.SourceId"),
            ("language_code", 5, singular, string, None),
            ("document_directive", 6, singular, string, None),
        ),
        artifacts_pb2.TailoredReportArtifact: _expected(
            (
                "generation_options",
                2,
                singular,
                message,
                f"{package}.TailoredReportArtifactGenerationOptions",
            ),
            ("report_doc", 3, singular, message, f"{package}.TailwindDoc"),
        ),
        artifacts_pb2.ServedImage: _expected(("url", 1, singular, string, None)),
        artifacts_pb2.InfographicGenerationOptions: _expected(
            ("user_steering_prompt", 1, singular, string, None),
            ("language_code", 2, singular, string, None),
            (
                "aspect_ratio",
                4,
                singular,
                enum,
                f"{package}.InfographicGenerationOptions.AspectRatio",
            ),
            (
                "style",
                6,
                singular,
                enum,
                f"{package}.InfographicGenerationOptions.InfographicStyle",
            ),
        ),
        artifacts_pb2.Infographic: _expected(
            ("title", 1, singular, string, None),
            ("image", 2, singular, message, f"{package}.ServedImage"),
        ),
        artifacts_pb2.InfographicArtifact: _expected(
            (
                "generation_options",
                1,
                singular,
                message,
                f"{package}.InfographicGenerationOptions",
            ),
            ("infographics", 3, repeated, message, f"{package}.Infographic"),
        ),
        artifacts_pb2.SlidesGenerationOptions: _expected(
            ("user_steering_prompt", 1, singular, string, None),
            ("language_code", 2, singular, string, None),
            ("deck_type", 3, singular, enum, f"{package}.DeckType"),
            ("length", 4, singular, enum, f"{package}.SlideDeckLength"),
        ),
        artifacts_pb2.Slide: _expected(
            ("image", 1, singular, message, f"{package}.ServedImage"),
        ),
        artifacts_pb2.SlidesArtifact: _expected(
            (
                "generation_options",
                1,
                singular,
                message,
                f"{package}.SlidesGenerationOptions",
            ),
            ("slides", 3, repeated, message, f"{package}.Slide"),
            ("pdf_download_url", 4, singular, string, None),
            ("pptx_download_url", 5, singular, string, None),
        ),
        artifacts_pb2.SlideEditInstruction: _expected(
            ("slide_index", 1, singular, integer, None),
            ("edit_instruction", 2, singular, string, None),
        ),
        artifacts_pb2.SlidesDerivationOptions: _expected(
            (
                "slide_edit_instructions",
                1,
                repeated,
                message,
                f"{package}.SlideEditInstruction",
            ),
        ),
        artifacts_pb2.FileArtifact: _expected(
            ("file_name", 1, singular, string, None),
            ("mime_type", 2, singular, string, None),
            ("file_preview_url", 3, singular, string, None),
            ("file_download_url", 4, singular, string, None),
        ),
        artifacts_pb2.AudioOverviewState: _expected(
            ("playback_position", 1, singular, message, "google.protobuf.Duration"),
        ),
        artifacts_pb2.VideoOverviewState: _expected(),
        artifacts_pb2.AppArtifactState: _expected(
            ("app_state", 1, singular, message, "google.protobuf.Struct"),
        ),
        artifacts_pb2.ScheduledNotificationConfig: _expected(),
        artifacts_pb2.ArtifactState: _expected(
            (
                "audio_overview_state",
                1,
                singular,
                message,
                f"{package}.AudioOverviewState",
            ),
            (
                "video_overview_state",
                2,
                singular,
                message,
                f"{package}.VideoOverviewState",
            ),
            ("app_artifact_state", 3, singular, message, f"{package}.AppArtifactState"),
            (
                "scheduled_notification_configs",
                4,
                repeated,
                message,
                f"{package}.ScheduledNotificationConfig",
            ),
        ),
        artifacts_pb2.ArtifactSource: _expected(
            ("source_id", 1, singular, message, f"{package}.SourceId"),
        ),
        artifacts_pb2.Artifact: _expected(
            ("artifact_id", 1, singular, string, None),
            ("title", 2, singular, string, None),
            ("type", 3, singular, enum, f"{package}.ArtifactType"),
            ("sources", 4, repeated, message, f"{package}.ArtifactSource"),
            ("status", 5, singular, enum, f"{package}.ArtifactStatus"),
            ("audio_overview", 7, singular, message, f"{package}.AudioOverviewArtifact"),
            ("tailored_report", 8, singular, message, f"{package}.TailoredReportArtifact"),
            ("explainer_video", 9, singular, message, f"{package}.ExplainerVideoArtifact"),
            ("app", 10, singular, message, f"{package}.AppArtifact"),
            ("last_modified_timestamp", 11, singular, message, "google.protobuf.Timestamp"),
            ("infographic", 15, singular, message, f"{package}.InfographicArtifact"),
            ("slides", 17, singular, message, f"{package}.SlidesArtifact"),
            ("artifact_user_state", 18, singular, message, f"{package}.ArtifactState"),
            ("etag", 22, singular, string, None),
            ("file", 25, singular, message, f"{package}.FileArtifact"),
        ),
    }
    for message_type, fields in expected.items():
        assert _shapes(message_type) == fields
    assert integer == FieldDescriptor.TYPE_INT32


def test_web_derived_report_suggestion_fields_are_exhaustive() -> None:
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    integer = FieldDescriptor.TYPE_INT32
    message = FieldDescriptor.TYPE_MESSAGE
    assert _shapes(artifacts_pb2.GenerateReportSuggestionsRequest) == _expected(
        (
            "request_context",
            1,
            singular,
            message,
            "labs.language.tailwind.common.protos.RequestContext",
        ),
        ("project_id", 2, singular, string, None),
        ("source_ids", 3, repeated, message, f"{ORCHESTRATION_PACKAGE}.SourceId"),
    )
    assert _shapes(artifacts_pb2.ReportSuggestion) == _expected(
        ("title", 1, singular, string, None),
        ("description", 2, singular, string, None),
        ("source_ids", 4, repeated, message, f"{ORCHESTRATION_PACKAGE}.SourceId"),
        ("prompt", 5, singular, string, None),
        ("audience_level", 6, singular, integer, None),
    )
    assert _shapes(artifacts_pb2.GenerateReportSuggestionsResponse) == _expected(
        ("suggestions", 1, repeated, message, f"{ORCHESTRATION_PACKAGE}.ReportSuggestion")
    )


def test_descriptor_fixture_contains_the_cumulative_android_closure() -> None:
    descriptor_set = descriptor_pb2.FileDescriptorSet.FromString(
        (FIXTURES / "android_descriptor_set.pb").read_bytes()
    )
    names = {file.name for file in descriptor_set.file}
    assert {
        "google/internal/labs/tailwind/orchestration/v1/read.proto",
        "google/internal/labs/tailwind/orchestration/v1/artifacts.proto",
        "google/internal/labs/tailwind/v1/source_settings.proto",
        "google/protobuf/duration.proto",
        "google/protobuf/field_mask.proto",
        "google/protobuf/struct.proto",
        "google/protobuf/timestamp.proto",
    } <= names


def test_synthetic_fixture_exercises_every_admitted_public_projection() -> None:
    response = text_format.Parse(
        (FIXTURES / "list_artifacts_response.textproto").read_text(encoding="utf-8"),
        artifacts_pb2.ListArtifactsResponse(),
    )
    artifacts = decode_artifacts(response.artifacts, method_id=LIST_ARTIFACTS_METHOD)
    by_id = {artifact.id: artifact for artifact in artifacts}

    audio = by_id["audio-1"]
    assert audio.kind is ArtifactType.AUDIO
    assert audio.url == "https://lh3.googleusercontent.com/audio-stream"
    assert audio.generation_prompt == "Audio focus"
    assert audio.duration_seconds == 61.5
    assert audio.source_ids == ("source-a",)
    assert audio.etag == "etag-audio"
    assert [item.kind for item in audio.media_urls] == [
        ArtifactMediaType.PROGRESSIVE,
        ArtifactMediaType.DOWNLOAD,
    ]
    assert audio.last_modified_at is not None

    assert by_id["report-1"].report_kind == "Briefing Doc"
    assert by_id["report-1"].generation_prompt == "Report directive"
    assert by_id["video-1"].url.endswith("video-download")  # type: ignore[union-attr]
    assert by_id["video-1"].duration_seconds == 42
    assert by_id["quiz-1"].kind is ArtifactType.QUIZ
    assert by_id["quiz-1"].generation_prompt == "Quiz prompt"
    assert by_id["infographic-1"].url.endswith("cap=fixture")  # type: ignore[union-attr]
    assert by_id["infographic-1"].generation_prompt == "Infographic prompt"
    assert by_id["slides-1"].url.endswith("deck.pdf")  # type: ignore[union-attr]
    assert by_id["slides-1"].slides[0].image_url.endswith("slide-1.png")  # type: ignore[union-attr]
    assert by_id["table-1"].kind is ArtifactType.DATA_TABLE
    assert by_id["file-1"].url.endswith("file-download")  # type: ignore[union-attr]
    assert by_id["suggested-1"].status == artifacts_pb2.ARTIFACT_STATUS_SUGGESTED


def test_audio_options_project_only_onto_existing_public_artifact_fields() -> None:
    raw = artifacts_pb2.Artifact(
        artifact_id="audio-nested",
        title="Audio",
        type=artifacts_pb2.ARTIFACT_TYPE_AUDIO_OVERVIEW,
        status=artifacts_pb2.ARTIFACT_STATUS_READY,
        audio_overview=artifacts_pb2.AudioOverviewArtifact(
            generation_options=artifacts_pb2.AudioOverviewGenerationOptions(
                episode_focus="Nested focus",
                episode_length=artifacts_pb2.EPISODE_LENGTH_LONG,
                source_ids=[{"id": "source-nested"}],
                language_code="fr",
            ),
            is_interactive=True,
        ),
    )

    artifact = decode_artifact(raw, method_id=GET_ARTIFACT_METHOD)

    assert artifact.generation_prompt == "Nested focus"
    assert artifact.source_ids == ("source-nested",)
    assert not hasattr(artifact, "episode_length")
    assert not hasattr(artifact, "language_code")
    assert not hasattr(artifact, "is_interactive")


@pytest.mark.parametrize(
    ("artifact_type", "payload", "expected_source_id"),
    [
        (
            artifacts_pb2.ARTIFACT_TYPE_TAILORED_REPORT,
            {
                "tailored_report": artifacts_pb2.TailoredReportArtifact(
                    generation_options=artifacts_pb2.TailoredReportArtifactGenerationOptions(
                        source_ids=[{"id": "source-report"}]
                    )
                )
            },
            "source-report",
        ),
        (
            artifacts_pb2.ARTIFACT_TYPE_EXPLAINER_VIDEO,
            {
                "explainer_video": artifacts_pb2.ExplainerVideoArtifact(
                    generation_options=artifacts_pb2.ExplainerVideoGenerationOptions(
                        source_ids=[{"id": "source-video"}]
                    )
                )
            },
            "source-video",
        ),
    ],
)
def test_report_and_video_source_ids_fall_back_to_exact_nested_options(
    artifact_type: int,
    payload: dict[str, object],
    expected_source_id: str,
) -> None:
    raw = artifacts_pb2.Artifact(
        artifact_id="nested-sources",
        type=artifact_type,
        **payload,
    )

    artifact = decode_artifact(raw, method_id=GET_ARTIFACT_METHOD)

    assert artifact.source_ids == (expected_source_id,)


def test_top_level_artifact_sources_take_precedence_over_nested_options() -> None:
    raw = artifacts_pb2.Artifact(
        artifact_id="top-level-sources",
        type=artifacts_pb2.ARTIFACT_TYPE_EXPLAINER_VIDEO,
        sources=[{"source_id": {"id": "source-top"}}],
        explainer_video=artifacts_pb2.ExplainerVideoArtifact(
            generation_options=artifacts_pb2.ExplainerVideoGenerationOptions(
                source_ids=[{"id": "source-nested"}]
            )
        ),
    )

    artifact = decode_artifact(raw, method_id=GET_ARTIFACT_METHOD)

    assert artifact.source_ids == ("source-top",)


def test_exact_audio_and_flashcard_user_state_projects_to_public_types() -> None:
    audio = decode_artifact(
        artifacts_pb2.Artifact(
            artifact_id="audio-state",
            type=artifacts_pb2.ARTIFACT_TYPE_AUDIO_OVERVIEW,
            artifact_user_state=artifacts_pb2.ArtifactState(
                audio_overview_state=artifacts_pb2.AudioOverviewState(
                    playback_position={"seconds": 123, "nanos": 500_000_000}
                )
            ),
        ),
        method_id=GET_ARTIFACT_METHOD,
    )
    assert audio.user_state == AudioArtifactUserState(playback_position_seconds=123.5)

    flashcards = decode_artifact(
        artifacts_pb2.Artifact(
            artifact_id="flashcard-state",
            type=artifacts_pb2.ARTIFACT_TYPE_APP,
            app=artifacts_pb2.AppArtifact(
                generation_options=artifacts_pb2.AppArtifactGenerationOptions(
                    app_type=artifacts_pb2.APP_TYPE_FLASHCARDS
                )
            ),
            artifact_user_state=artifacts_pb2.ArtifactState(
                app_artifact_state=artifacts_pb2.AppArtifactState(
                    app_state={
                        "cardAcquisitionsMapping": {"0": "acquired"},
                        "currentCardIndex": 2,
                        "hiddenCardIndices": [4, 7],
                        "lastShownOrder": [2, 0, 1],
                        "currentView": "card",
                    }
                )
            ),
        ),
        method_id=GET_ARTIFACT_METHOD,
    )
    assert flashcards.user_state == FlashcardArtifactUserState(
        card_acquisitions={"0": "acquired"},
        current_card_index=2,
        hidden_card_indices=(4, 7),
        last_shown_order=(2, 0, 1),
        current_view="card",
    )


def test_decode_failure_drops_raw_message_capability_and_exception_frames() -> None:
    secret = "https://lh3.googleusercontent.com/image.png?cap=decoder-secret"

    class ExplodingArtifact:
        artifact_id = "artifact-1"
        type = 7

        def HasField(self, field: str) -> bool:
            raise ValueError(f"raw decoder failure {field} {secret}")

        def __repr__(self) -> str:
            return f"ExplodingArtifact(secret={secret!r})"

    raw = ExplodingArtifact()
    with pytest.raises(DecodingError) as raised:
        decode_artifact(raw, method_id=LIST_ARTIFACTS_METHOD)

    error = raised.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in str(error)
    for frame, _line in traceback.walk_tb(error.__traceback__):
        if "/src/notebooklm/" not in frame.f_code.co_filename:
            continue
        assert secret not in repr(frame.f_locals)
        assert raw not in frame.f_locals.values()


def test_copy_and_customization_choice_shapes_are_pinned() -> None:
    """#2283: CopyArtifactsAsync (web-derived) and GetArtifactCustomizationChoices (APK-exact)."""
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    integer = FieldDescriptor.TYPE_INT32
    message = FieldDescriptor.TYPE_MESSAGE
    enum = FieldDescriptor.TYPE_ENUM
    context = "labs.language.tailwind.common.protos.RequestContext"
    assert _shapes(artifacts_pb2.CopyArtifactsAsyncRequest) == _expected(
        ("request_context", 1, singular, message, context),
        ("artifact_ids", 2, repeated, string, None),
        ("target_project_id", 3, singular, string, None),
    )
    assert _shapes(artifacts_pb2.CopiedArtifact) == _expected(
        ("source_artifact_id", 1, singular, string, None),
        ("artifact", 2, singular, message, f"{ORCHESTRATION_PACKAGE}.Artifact"),
    )
    assert _shapes(artifacts_pb2.CopyArtifactsAsyncResponse) == _expected(
        ("copied_artifacts", 1, repeated, message, f"{ORCHESTRATION_PACKAGE}.CopiedArtifact"),
    )
    assert _shapes(artifacts_pb2.GetArtifactCustomizationChoicesRequest) == _expected(
        ("request_context", 1, singular, message, context),
        ("project_id", 2, singular, string, None),
        ("artifact_type", 3, singular, enum, f"{ORCHESTRATION_PACKAGE}.ArtifactType"),
    )
    assert _shapes(artifacts_pb2.FormatChoice) == _expected(
        ("format", 1, singular, integer, None),
        ("title", 2, singular, string, None),
        ("description", 3, singular, string, None),
    )
    assert _shapes(artifacts_pb2.FormatChoices) == _expected(
        ("choices", 1, repeated, message, f"{ORCHESTRATION_PACKAGE}.FormatChoice"),
    )
    assert _shapes(artifacts_pb2.SlidesType) == _expected(
        ("deck_type", 1, singular, enum, f"{ORCHESTRATION_PACKAGE}.DeckType"),
        ("title", 2, singular, string, None),
        ("description", 3, singular, string, None),
    )
    assert _shapes(artifacts_pb2.SlidesCustomizationChoices) == _expected(
        ("types", 1, repeated, message, f"{ORCHESTRATION_PACKAGE}.SlidesType"),
    )
    assert _shapes(artifacts_pb2.TailoredReportTypeOption) == _expected(
        ("report_type", 1, singular, string, None),
        ("report_description", 2, singular, string, None),
        ("report_directive", 3, singular, string, None),
    )
    assert _shapes(artifacts_pb2.TailoredReportCustomizationChoices) == _expected(
        (
            "report_type_options",
            1,
            repeated,
            message,
            f"{ORCHESTRATION_PACKAGE}.TailoredReportTypeOption",
        ),
    )
    assert _shapes(artifacts_pb2.ArtifactCustomizationChoices) == _expected(
        ("audio_overview_choices", 1, singular, message, f"{ORCHESTRATION_PACKAGE}.FormatChoices"),
        ("video_overview_choices", 2, singular, message, f"{ORCHESTRATION_PACKAGE}.FormatChoices"),
        (
            "slides_customization_choices",
            3,
            singular,
            message,
            f"{ORCHESTRATION_PACKAGE}.SlidesCustomizationChoices",
        ),
        (
            "tailored_report_customization_choices",
            4,
            singular,
            message,
            f"{ORCHESTRATION_PACKAGE}.TailoredReportCustomizationChoices",
        ),
    )
    assert _shapes(artifacts_pb2.GetArtifactCustomizationChoicesResponse) == _expected(
        (
            "artifact_customization_choices",
            1,
            singular,
            message,
            f"{ORCHESTRATION_PACKAGE}.ArtifactCustomizationChoices",
        ),
    )
