"""Contract tests for backend-neutral artifact copy and export workflows."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from notebooklm._artifacts import ArtifactsAPI, _ArtifactCopyResult
from notebooklm._types.enums import ExportType
from notebooklm.exceptions import ArtifactNotFoundError, DecodingError, RPCError, ValidationError
from notebooklm.types import Artifact, ArtifactCustomizationChoices, CopiedArtifact


class _ConcreteArtifacts(ArtifactsAPI):
    """Minimal backend proving each shared workflow needs one wire hook."""

    def __init__(
        self,
        *,
        copy_result: _ArtifactCopyResult | Exception | None = None,
        export_result: Any = None,
        customization_result: ArtifactCustomizationChoices | None = None,
    ) -> None:
        self.copy_result = copy_result
        self.export_result = export_result
        self.customization_result = customization_result or ArtifactCustomizationChoices()
        self.copy_calls: list[tuple[str, list[str], str]] = []
        self.export_calls: list[tuple[str, str | None, str, ExportType, str | None]] = []
        self.customization_calls: list[str | None] = []

    async def _send_copy(
        self,
        notebook_id: str,
        artifact_ids: list[str],
        target_notebook_id: str,
    ) -> _ArtifactCopyResult:
        self.copy_calls.append((notebook_id, artifact_ids, target_notebook_id))
        if isinstance(self.copy_result, Exception):
            raise self.copy_result
        assert self.copy_result is not None
        return self.copy_result

    async def _send_export(
        self,
        notebook_id: str,
        artifact_id: str | None,
        title: str,
        export_type: ExportType,
        *,
        content: str | None,
    ) -> Any:
        self.export_calls.append((notebook_id, artifact_id, title, export_type, content))
        return self.export_result

    async def _read_customization_choices(
        self, notebook_id: str | None = None
    ) -> ArtifactCustomizationChoices:
        self.customization_calls.append(notebook_id)
        return self.customization_result

    async def _unsupported(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    _list_studio = _unsupported
    _send_create_artifact = _unsupported
    delete = _unsupported
    download_audio = _unsupported
    download_data_table = _unsupported
    download_flashcards = _unsupported
    download_infographic = _unsupported
    download_mind_map = _unsupported
    download_quiz = _unsupported
    download_report = _unsupported
    download_slide_deck = _unsupported
    download_video = _unsupported
    generate_mind_map = _unsupported
    get_prompt = _unsupported
    list = _unsupported
    rename = _unsupported
    retry_failed = _unsupported
    revise_slide = _unsupported
    suggest_reports = _unsupported


@pytest.mark.asyncio
async def test_customization_choices_delegates_to_the_single_typed_read_hook() -> None:
    expected = ArtifactCustomizationChoices()
    api = _ConcreteArtifacts(customization_result=expected)

    assert await api.get_customization_choices("nb") is expected
    assert api.customization_calls == ["nb"]


@pytest.mark.asyncio
async def test_export_wrappers_route_through_the_single_typed_hook() -> None:
    marker = object()
    api = _ConcreteArtifacts(export_result=marker)

    assert await api.export_report("nb", "report", "Report", ExportType.DOCS) is marker
    assert await api.export_data_table("nb", "table", "Table") is marker
    assert (
        await api.export(
            "nb",
            title="Literal",
            export_type=ExportType.SHEETS,
            content="a,b\n1,2",
        )
        is marker
    )
    assert api.export_calls == [
        ("nb", "report", "Report", ExportType.DOCS, None),
        ("nb", "table", "Table", ExportType.SHEETS, None),
        ("nb", None, "Literal", ExportType.SHEETS, "a,b\n1,2"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_id", "content"),
    [(None, None), ("artifact", "literal")],
)
async def test_export_rejects_invalid_target_before_the_hook(
    artifact_id: str | None,
    content: str | None,
) -> None:
    api = _ConcreteArtifacts(export_result=object())

    with pytest.raises(ValidationError, match="exactly one"):
        await api.export("nb", artifact_id, content=content)

    assert api.export_calls == []


@pytest.mark.asyncio
async def test_copy_returns_committed_rows_and_warns_once_for_a_partial_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    copied = CopiedArtifact(
        original_id="artifact-a",
        artifact=Artifact(id="copied-a", title="Copy", _artifact_type=4, status=3),
    )
    api = _ConcreteArtifacts(copy_result=_ArtifactCopyResult([copied], "fake.CopyArtifactsAsync"))

    with caplog.at_level(logging.WARNING, logger="notebooklm._artifacts"):
        result = await api.copy("nb", ["artifact-b", "artifact-a"], "target")

    assert result == [copied]
    assert api.copy_calls == [("nb", ["artifact-b", "artifact-a"], "target")]
    assert caplog.messages == [
        "CopyArtifactsAsync copied 1 of 2 artifact(s) into target; not copied: artifact-b"
    ]


@pytest.mark.asyncio
async def test_copy_distinguishes_empty_and_malformed_mapping_failures() -> None:
    empty = _ConcreteArtifacts(copy_result=_ArtifactCopyResult([], "fake.CopyArtifactsAsync"))
    with pytest.raises(ArtifactNotFoundError) as missing:
        await empty.copy("nb", ["artifact-a"], "target")
    assert missing.value.method_id == "fake.CopyArtifactsAsync"

    malformed = _ConcreteArtifacts(
        copy_result=_ArtifactCopyResult(
            [],
            "fake.CopyArtifactsAsync",
            malformed_count=2,
            raw_response="[['broken']]",
        )
    )
    with pytest.raises(DecodingError) as decoding:
        await malformed.copy("nb", ["artifact-a"], "target")
    assert decoding.value.method_id == "fake.CopyArtifactsAsync"
    assert decoding.value.raw_response == "[['broken']]"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_ids", "target", "message"),
    [
        ([], "target", "must not be empty"),
        (["artifact-a", ""], "target", "must not contain empty entries"),
        (["artifact-a"], "", "target_notebook_id must not be empty"),
    ],
)
async def test_copy_rejects_invalid_inputs_before_the_hook(
    artifact_ids: list[str],
    target: str,
    message: str,
) -> None:
    api = _ConcreteArtifacts(copy_result=_ArtifactCopyResult([], "fake.CopyArtifactsAsync"))

    with pytest.raises(ValidationError, match=message):
        await api.copy("nb", artifact_ids, target)

    assert api.copy_calls == []


@pytest.mark.asyncio
async def test_copy_propagates_the_backend_exception_by_identity() -> None:
    error = RPCError("backend-specific failure", method_id="fake.CopyArtifactsAsync")
    api = _ConcreteArtifacts(copy_result=error)

    with pytest.raises(RPCError) as raised:
        await api.copy("nb", ["artifact-a"], "target")

    assert raised.value is error
