"""Exact-package descriptor and projection gates for the B4 artifact closure."""

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
    GENERATE_REPORT_SUGGESTIONS_METHOD,
    LIST_ARTIFACTS_METHOD,
    UPDATE_ARTIFACT_METHOD,
)
from notebooklm._android.codecs.artifacts import decode_artifact, decode_artifacts
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    artifacts_pb2,
)
from notebooklm._android.proto.notebooklm.android.internal.v1 import (
    report_suggestions_pb2,
)
from notebooklm._types.artifact_content import ArtifactMediaType
from notebooklm.exceptions import DecodingError
from notebooklm.types import ArtifactType

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "android"
ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"
LOCAL_PACKAGE = "notebooklm.android.internal.v1"


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


def _expected(*items: tuple[str, int, bool, int, str | None]) -> dict[str, tuple[int, bool, int, str | None]]:
    return {name: (number, repeated, kind, target) for name, number, repeated, kind, target in items}


def _enum_values(enum: Any) -> dict[str, int]:
    return {value.name: value.number for value in enum.DESCRIPTOR.values}


def test_b4_exact_package_and_repository_local_overlay_are_distinct() -> None:
    assert artifacts_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert report_suggestions_pb2.DESCRIPTOR.package == LOCAL_PACKAGE
    assert list(artifacts_pb2.DESCRIPTOR.services_by_name) == []
    assert list(report_suggestions_pb2.DESCRIPTOR.services_by_name) == []
    assert not hasattr(artifacts_pb2, "GenerateReportSuggestionsRequestWire")


def test_full_method_paths_are_exact_and_local_overlay_does_not_claim_a_service() -> None:
    prefix = f"/{ORCHESTRATION_PACKAGE}.LabsTailwindOrchestrationService/"
    assert f"{prefix}ListArtifacts" == LIST_ARTIFACTS_METHOD
    assert f"{prefix}CreateArtifact" == CREATE_ARTIFACT_METHOD
    assert f"{prefix}UpdateArtifact" == UPDATE_ARTIFACT_METHOD
    assert f"{prefix}DeleteArtifact" == DELETE_ARTIFACT_METHOD
    assert f"{prefix}GenerateReportSuggestions" == GENERATE_REPORT_SUGGESTIONS_METHOD


def test_b4_request_response_fields_are_exhaustive() -> None:
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    message = FieldDescriptor.TYPE_MESSAGE

    expected = {
        artifacts_pb2.CreateArtifactRequest: _expected(
            ("project_id", 2, singular, string, None),
            ("artifact", 3, singular, message, f"{ORCHESTRATION_PACKAGE}.Artifact"),
        ),
        artifacts_pb2.CreateArtifactResponse: _expected(
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


def test_b4_reachable_enum_names_and_numbers_are_exhaustive() -> None:
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
    assert _enum_values(artifacts_pb2.MediaStreamingType) == {
        "MEDIA_STREAMING_TYPE_UNSPECIFIED": 0,
        "MEDIA_STREAMING_TYPE_PROGRESSIVE_STREAMING": 1,
        "MEDIA_STREAMING_TYPE_ADAPTIVE_STREAMING_HLS": 2,
        "MEDIA_STREAMING_TYPE_ADAPTIVE_STREAMING_DASH": 3,
        "MEDIA_STREAMING_TYPE_DOWNLOAD": 4,
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


def test_b4_artifact_projection_fields_are_exhaustive() -> None:
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
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
        artifacts_pb2.AppArtifactGenerationOptions: _expected(
            ("app_type", 1, singular, enum, f"{package}.AppType"),
            ("free_text_steering_prompt", 3, singular, string, None),
            (
                "quiz_generation_options",
                8,
                singular,
                message,
                f"{package}.QuizGenerationOptions",
            ),
        ),
        artifacts_pb2.AppArtifact: _expected(
            (
                "generation_options",
                2,
                singular,
                message,
                f"{package}.AppArtifactGenerationOptions",
            ),
        ),
        artifacts_pb2.AudioOverviewGenerationOptions: _expected(
            ("episode_focus", 1, singular, string, None),
        ),
        artifacts_pb2.AudioOverviewArtifact: _expected(
            (
                "generation_options",
                2,
                singular,
                message,
                f"{package}.AudioOverviewGenerationOptions",
            ),
            ("media_urls", 6, repeated, message, f"{package}.MediaStreamingUrl"),
            ("duration", 7, singular, message, duration),
        ),
        artifacts_pb2.ExplainerVideoGenerationOptions: _expected(
            ("video_focus", 3, singular, string, None),
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
        ),
        artifacts_pb2.ServedImage: _expected(("url", 1, singular, string, None)),
        artifacts_pb2.InfographicGenerationOptions: _expected(
            ("user_steering_prompt", 1, singular, string, None),
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
        ),
        artifacts_pb2.FileArtifact: _expected(
            ("file_preview_url", 3, singular, string, None),
            ("file_download_url", 4, singular, string, None),
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
            ("etag", 22, singular, string, None),
            ("file", 25, singular, message, f"{package}.FileArtifact"),
        ),
    }
    for message_type, fields in expected.items():
        assert _shapes(message_type) == fields
    assert integer == FieldDescriptor.TYPE_INT32


def test_local_report_overlay_fields_are_exhaustive() -> None:
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    integer = FieldDescriptor.TYPE_INT32
    message = FieldDescriptor.TYPE_MESSAGE
    assert _shapes(report_suggestions_pb2.GenerateReportSuggestionsRequestWire) == _expected(
        ("project_id", 2, singular, string, None)
    )
    assert _shapes(report_suggestions_pb2.ReportSuggestionWire) == _expected(
        ("title", 1, singular, string, None),
        ("description", 2, singular, string, None),
        ("prompt", 5, singular, string, None),
        ("audience_level", 6, singular, integer, None),
    )
    assert _shapes(report_suggestions_pb2.GenerateReportSuggestionsResponseWire) == _expected(
        ("suggestions", 1, repeated, message, f"{LOCAL_PACKAGE}.ReportSuggestionWire")
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
        "google/protobuf/timestamp.proto",
        "notebooklm/android/internal/v1/report_suggestions.proto",
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
    assert audio.url == "https://lh3.googleusercontent.com/audio-download"
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
