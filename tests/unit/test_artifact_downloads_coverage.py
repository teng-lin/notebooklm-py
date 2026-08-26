"""Focused coverage for the P5.8 semantic representation service."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._semantic.operations import Operation
from notebooklm._semantic.records import (
    ARTIFACT_DOWNLOAD_DEF,
    ArtifactDownloadResult,
    ArtifactRecord,
    ArtifactRepresentationRecord,
    MindMapRepresentationRecord,
)
from notebooklm._studio.downloads import DownloadResult, StudioDownloadClient
from notebooklm._studio.representations import ArtifactRepresentationService
from notebooklm._studio.serialization import StudioSerializationClient
from notebooklm._web.codec.artifacts import decode_artifact_representation
from notebooklm.exceptions import (
    ArtifactDownloadError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    UnknownRPCMethodError,
    ValidationError,
)
from tests._fixtures.recording_backend import RecordingBackend


def _record(
    family: str,
    *,
    artifact_id: str = "artifact-id",
    status: str = "completed",
    **values: object,
) -> ArtifactRepresentationRecord:
    return ArtifactRepresentationRecord(
        ArtifactRecord(
            artifact_id,
            family.title(),
            family,
            status,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        **values,
    )


def _service(
    backend: RecordingBackend | None = None,
) -> tuple[ArtifactRepresentationService, MagicMock, MagicMock]:
    remote = MagicMock(spec=StudioDownloadClient)
    remote.download = AsyncMock(return_value="output.bin")
    remote.download_batch = AsyncMock(
        return_value=DownloadResult(succeeded=["output.bin"], failed=[])
    )
    serialization = MagicMock(spec=StudioSerializationClient)
    serialization.write_text = AsyncMock(return_value="output.txt")
    serialization.write_csv = AsyncMock(return_value="output.csv")
    serialization.write_json_string = AsyncMock(return_value="output.json")
    return (
        ArtifactRepresentationService(
            backend,
            remote=remote,
            serialization=serialization,
        ),
        remote,
        serialization,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "field", "method"),
    [
        ("audio", "audio_url", "download_audio"),
        ("video", "video_url", "download_video"),
        ("infographic", "infographic_url", "download_infographic"),
    ],
)
async def test_remote_family_downloads_use_prefetched_neutral_records(
    family: str,
    field: str,
    method: str,
) -> None:
    service, remote, _ = _service()
    record = _record(family, **{field: f"https://storage.googleapis.com/{family}"})

    result = await getattr(service, method)(
        "nb",
        "output.bin",
        representations=(record,),
    )

    assert result == "output.bin"
    remote.download.assert_awaited_once_with(
        f"https://storage.googleapis.com/{family}",
        "output.bin",
    )


@pytest.mark.asyncio
async def test_empty_prefetch_is_authoritative_and_does_not_requery_backend() -> None:
    backend = RecordingBackend()
    backend.set_result(
        ARTIFACT_DOWNLOAD_DEF,
        ArtifactDownloadResult((_record("audio", audio_url="https://example.invalid"),)),
    )
    service, _, _ = _service(backend)

    with pytest.raises(ArtifactNotReadyError):
        await service.download_audio("nb", "audio.bin", representations=())

    assert backend.invocations == []


@pytest.mark.asyncio
async def test_slide_deck_format_and_url_contract() -> None:
    service, remote, _ = _service()
    record = _record(
        "slide_deck",
        slide_deck_pdf_url="https://storage.googleapis.com/deck.pdf",
        slide_deck_pptx_url="https://storage.googleapis.com/deck.pptx",
    )

    await service.download_slide_deck(
        "nb",
        "deck.pptx",
        output_format="pptx",
        representations=(record,),
    )
    remote.download.assert_awaited_once_with(
        "https://storage.googleapis.com/deck.pptx",
        "deck.pptx",
    )
    with pytest.raises(ValidationError):
        await service.download_slide_deck(
            "nb",
            "deck.docx",
            output_format="docx",
            representations=(record,),
        )


@pytest.mark.asyncio
async def test_local_report_and_table_serialization_preserves_values() -> None:
    service, _, serialization = _service()
    report = _record("report", report_markdown="# Report")
    table = _record(
        "data_table",
        data_table_headers=("name", "count"),
        data_table_rows=(("one", "1"),),
    )

    assert (
        await service.download_report("nb", "report.md", representations=(report,)) == "output.txt"
    )
    assert (
        await service.download_data_table("nb", "table.csv", representations=(table,))
        == "output.csv"
    )
    serialization.write_text.assert_awaited_once_with("report.md", "# Report")
    serialization.write_csv.assert_awaited_once_with(
        "table.csv",
        ["name", "count"],
        [["one", "1"]],
    )


@pytest.mark.asyncio
async def test_representation_parse_errors_remain_public_and_scrubbed() -> None:
    service, _, _ = _service()
    record = _record("audio", parse_error="shape moved")

    with pytest.raises(ArtifactParseError, match="shape moved"):
        await service.download_audio("nb", "audio.bin", representations=(record,))
    assert "https://" not in repr(record)


@pytest.mark.asyncio
async def test_codec_parse_failure_preserves_sanitized_public_cause_graph() -> None:
    # A present-but-empty slide metadata block is strict structural drift at
    # the required PDF URL leaf, not an absent optional representation.
    row: list[object] = ["deck-id", "Deck", 8, None, 3]
    row.extend([None] * 11)
    row.append([])
    record = decode_artifact_representation(row)
    assert record.parse_failure is not None
    assert "raw" not in repr(record.parse_failure)

    service, _, _ = _service()
    with pytest.raises(ArtifactParseError) as caught:
        await service.download_slide_deck("nb", "deck.pdf", representations=(record,))

    assert isinstance(caught.value.cause, UnknownRPCMethodError)
    assert caught.value.__cause__ is caught.value.cause
    assert caught.value.cause.method_id is not None
    assert caught.value.cause.data_at_failure == "[]"


@pytest.mark.asyncio
async def test_data_table_nested_parse_cause_survives_codec_boundary() -> None:
    row: list[object] = ["table-id", "Table", 9, None, 3]
    row.extend([None] * 13)
    row.append([[[]]])
    record = decode_artifact_representation(row)
    assert record.data_table_error is not None
    assert record.data_table_failure is not None

    service, _, _ = _service()
    with pytest.raises(ArtifactParseError) as caught:
        await service.download_data_table("nb", "table.csv", representations=(record,))

    assert isinstance(caught.value.cause, UnknownRPCMethodError)
    assert caught.value.__cause__ is caught.value.cause


@pytest.mark.asyncio
async def test_interactive_download_uses_backend_content_and_local_serializer() -> None:
    backend = RecordingBackend()
    backend.set_result(
        ARTIFACT_DOWNLOAD_DEF,
        ArtifactDownloadResult(content='<body data-app-data="{&quot;quiz&quot;: []}"></body>'),
    )
    service, _, serialization = _service(backend)
    artifact = ArtifactRecord("quiz-id", "Quiz", "quiz", "completed")

    result = await service.download_interactive(
        "nb",
        "quiz.json",
        "quiz-id",
        "json",
        "quiz",
        artifacts=(artifact,),
    )

    assert result == "output.txt"
    assert backend.invocations[0].operation is Operation.ARTIFACT_DOWNLOAD
    assert backend.invocations[0].value.action == "interactive_html"
    serialization.write_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_mind_map_selection_distinguishes_not_ready_and_not_found() -> None:
    service, _, _ = _service()
    other = MindMapRepresentationRecord("other", "Other", "{}")

    with pytest.raises(ArtifactNotFoundError):
        await service.download_mind_map(
            "nb",
            "map.json",
            "missing",
            mind_maps=(other,),
            representations=(),
        )
    with pytest.raises(ArtifactNotReadyError):
        await service.download_mind_map(
            "nb",
            "map.json",
            mind_maps=(),
            representations=(),
        )


@pytest.mark.asyncio
async def test_remote_batch_is_delegated_to_single_trusted_client() -> None:
    service, remote, _ = _service()

    result = await service.download_batch([("https://storage.googleapis.com/x", "x.bin")])

    assert result.all_succeeded
    remote.download_batch.assert_awaited_once_with([("https://storage.googleapis.com/x", "x.bin")])


@pytest.mark.asyncio
async def test_missing_slide_url_keeps_download_error_contract() -> None:
    service, _, _ = _service()
    with pytest.raises(ArtifactDownloadError):
        await service.download_slide_deck(
            "nb",
            "deck.pdf",
            representations=(_record("slide_deck"),),
        )
