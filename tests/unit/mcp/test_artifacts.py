"""Unit tests for the artifact MCP tools.

Drives each tool through the in-memory FastMCP ``Client`` against a server bound
to the mocked ``NotebookLMClient``, asserting the serialized
``structured_content``. Covers each tool's happy path, name-vs-id resolution
reaching the tool, the per-``type`` ``artifact_generate`` / ``artifact_download``
enum dispatch, the start→status poll shape, and error projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm._types.artifacts import (  # noqa: E402
    QUIZ_VARIANT,
    ArtifactStatus,
    ArtifactTypeCode,
)
from notebooklm.exceptions import (  # noqa: E402 - after importorskip guard
    ArtifactNotFoundError,
    NotebookNotFoundError,
)
from notebooklm.types import Artifact, ArtifactType, GenerationState  # noqa: E402

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

NB_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "task-abc-123"

#: Real-``Artifact`` builders for the download core (it filters on
#: ``isinstance(a, Artifact)`` + the int type code + ``is_completed``).
_AUDIO_ARTIFACT = Artifact(
    id="art1",
    title="Podcast",
    _artifact_type=ArtifactTypeCode.AUDIO.value,
    status=int(ArtifactStatus.COMPLETED),
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
)
_QUIZ_ARTIFACT = Artifact(
    id="q1",
    title="Quiz",
    _artifact_type=ArtifactTypeCode.QUIZ.value,
    status=int(ArtifactStatus.COMPLETED),
    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    _variant=QUIZ_VARIANT,
)


@dataclass
class FakeArtifact:
    id: str
    title: str
    kind: ArtifactType = ArtifactType.AUDIO
    is_completed: bool = True
    created_at: datetime = field(default_factory=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc))


@dataclass
class FakeStatus:
    task_id: str
    status: GenerationState = GenerationState.COMPLETED
    url: str | None = "https://example.com/out.mp3"
    error: str | None = None
    error_code: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def is_complete(self) -> bool:
        return self.status == GenerationState.COMPLETED


# ---------------------------------------------------------------------------
# artifact_list
# ---------------------------------------------------------------------------


async def test_artifact_list(mcp_call, mock_client) -> None:
    mock_client.artifacts.list = AsyncMock(
        return_value=[FakeArtifact(id="art1", title="My Podcast")]
    )
    result = await mcp_call("artifact_list", {"notebook": NB_ID})
    assert result.structured_content["notebook_id"] == NB_ID
    assert result.structured_content["artifacts"][0]["id"] == "art1"
    mock_client.artifacts.list.assert_awaited_once_with(NB_ID)


async def test_artifact_list_resolves_notebook_by_name(mcp_call, mock_client) -> None:
    @dataclass
    class FakeNotebook:
        id: str
        title: str

    mock_client.notebooks.list = AsyncMock(
        return_value=[FakeNotebook(id=NB_ID, title="My Notebook")]
    )
    mock_client.artifacts.list = AsyncMock(return_value=[])
    result = await mcp_call("artifact_list", {"notebook": "My Notebook"})
    assert result.structured_content["notebook_id"] == NB_ID
    mock_client.artifacts.list.assert_awaited_with(NB_ID)


# ---------------------------------------------------------------------------
# artifact_generate
# ---------------------------------------------------------------------------


async def test_artifact_generate_audio(mcp_call, mock_client) -> None:
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    result = await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "audio"})
    assert result.structured_content["kind"] == "audio"
    assert result.structured_content["task_id"] == TASK_ID
    mock_client.artifacts.generate_audio.assert_awaited_once()
    # notebook id is the first positional arg.
    assert mock_client.artifacts.generate_audio.await_args.args[0] == NB_ID


async def test_artifact_generate_quiz_routes_to_quiz(mcp_call, mock_client) -> None:
    mock_client.artifacts.generate_quiz = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    result = await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "quiz"})
    assert result.structured_content["kind"] == "quiz"
    mock_client.artifacts.generate_quiz.assert_awaited_once()


async def test_artifact_generate_video_routes_to_video(mcp_call, mock_client) -> None:
    mock_client.artifacts.generate_video = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "video"})
    mock_client.artifacts.generate_video.assert_awaited_once()


async def test_artifact_generate_report_routes_to_report(mcp_call, mock_client) -> None:
    mock_client.artifacts.generate_report = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "report", "report_format": "study-guide"},
    )
    mock_client.artifacts.generate_report.assert_awaited_once()


async def test_artifact_generate_passes_source_ids(mcp_call, mock_client) -> None:
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "audio", "source_ids": ["src-1", "src-2"]},
    )
    kwargs = mock_client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] == ("src-1", "src-2")


async def test_artifact_generate_unknown_type_is_validation_error(mcp_call, mock_client) -> None:
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "bogus"})
    assert "VALIDATION" in str(excinfo.value)


async def test_artifact_generate_bad_enum_is_validation_error(mcp_call, mock_client) -> None:
    """A bad per-kind option (e.g. report_format) projects as VALIDATION."""
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_generate",
            {"notebook": NB_ID, "artifact_type": "report", "report_format": "nonsense"},
        )
    assert "VALIDATION" in str(excinfo.value)


async def test_artifact_generate_bad_language_is_validation_error(mcp_call, mock_client) -> None:
    """An unsupported ``language`` projects as VALIDATION up front (not forwarded raw)."""
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_generate",
            {"notebook": NB_ID, "artifact_type": "audio", "language": "klingon"},
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.artifacts.generate_audio.assert_not_called()


async def test_artifact_generate_valid_language_passes(mcp_call, mock_client) -> None:
    """A supported language code is accepted and forwarded."""
    mock_client.artifacts.generate_audio = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    result = await mcp_call(
        "artifact_generate",
        {"notebook": NB_ID, "artifact_type": "audio", "language": "es"},
    )
    assert result.structured_content["kind"] == "audio"
    mock_client.artifacts.generate_audio.assert_awaited_once()


# ---------------------------------------------------------------------------
# artifact_status (stateless poll)
# ---------------------------------------------------------------------------


async def test_artifact_status(mcp_call, mock_client) -> None:
    mock_client.artifacts.poll_status = AsyncMock(return_value=FakeStatus(task_id=TASK_ID))
    result = await mcp_call("artifact_status", {"notebook": NB_ID, "task_id": TASK_ID})
    assert result.structured_content["task_id"] == TASK_ID
    assert result.structured_content["is_complete"] is True
    assert result.structured_content["status"] == GenerationState.COMPLETED.value
    mock_client.artifacts.poll_status.assert_awaited_once_with(NB_ID, TASK_ID)


async def test_artifact_generate_then_status_poll_shape(mcp_call, mock_client) -> None:
    """The start→status poll loop: generate returns a task_id, status polls it."""
    mock_client.artifacts.generate_audio = AsyncMock(
        return_value=FakeStatus(task_id=TASK_ID, status=GenerationState.PENDING, url=None)
    )
    started = await mcp_call("artifact_generate", {"notebook": NB_ID, "artifact_type": "audio"})
    task_id = started.structured_content["task_id"]
    assert task_id == TASK_ID

    mock_client.artifacts.poll_status = AsyncMock(
        return_value=FakeStatus(task_id=TASK_ID, status=GenerationState.COMPLETED)
    )
    polled = await mcp_call("artifact_status", {"notebook": NB_ID, "task_id": task_id})
    assert polled.structured_content["is_complete"] is True


# ---------------------------------------------------------------------------
# artifact_download
# ---------------------------------------------------------------------------


async def test_artifact_download_audio(mcp_call, mock_client, tmp_path) -> None:
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT])
    mock_client.artifacts.download_audio = AsyncMock(return_value=out)
    result = await mcp_call(
        "artifact_download", {"notebook": NB_ID, "artifact_type": "audio", "path": out}
    )
    assert result.structured_content["outcome"] == "single_downloaded"
    assert result.structured_content["output_path"] == out
    mock_client.artifacts.download_audio.assert_awaited_once()


async def test_artifact_download_quiz_with_format(mcp_call, mock_client, tmp_path) -> None:
    out = str(tmp_path / "quiz.md")
    mock_client.artifacts.list = AsyncMock(return_value=[_QUIZ_ARTIFACT])
    mock_client.artifacts.download_quiz = AsyncMock(return_value=out)
    result = await mcp_call(
        "artifact_download",
        {"notebook": NB_ID, "artifact_type": "quiz", "path": out, "output_format": "markdown"},
    )
    assert result.structured_content["outcome"] == "single_downloaded"
    # The format kwarg flows through to the bound download coroutine.
    assert mock_client.artifacts.download_quiz.await_args.kwargs.get("output_format") == "markdown"


async def test_artifact_download_unknown_type_is_validation_error(mcp_call, mock_client) -> None:
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_download", {"notebook": NB_ID, "artifact_type": "bogus", "path": "/tmp/x"}
        )
    assert "VALIDATION" in str(excinfo.value)


async def test_artifact_download_bad_format_for_supported_type_is_validation(
    mcp_call, mock_client, tmp_path
) -> None:
    """A bad ``format`` for a type that DOES support format projects VALIDATION."""
    out = str(tmp_path / "quiz.json")
    mock_client.artifacts.list = AsyncMock(return_value=[_QUIZ_ARTIFACT])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_download",
            {"notebook": NB_ID, "artifact_type": "quiz", "path": out, "output_format": "bogus"},
        )
    assert "VALIDATION" in str(excinfo.value)


async def test_artifact_download_format_for_unsupported_type_is_validation(
    mcp_call, mock_client, tmp_path
) -> None:
    """Supplying ``format`` for a type WITHOUT format choices errors (was silently dropped)."""
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[_AUDIO_ARTIFACT])
    mock_client.artifacts.download_audio = AsyncMock(return_value=out)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(
            "artifact_download",
            {"notebook": NB_ID, "artifact_type": "audio", "path": out, "output_format": "pdf"},
        )
    assert "VALIDATION" in str(excinfo.value)
    mock_client.artifacts.download_audio.assert_not_called()


async def test_artifact_download_no_artifacts(mcp_call, mock_client, tmp_path) -> None:
    out = str(tmp_path / "out.mp3")
    mock_client.artifacts.list = AsyncMock(return_value=[])
    result = await mcp_call(
        "artifact_download", {"notebook": NB_ID, "artifact_type": "audio", "path": out}
    )
    assert result.structured_content["outcome"] == "no_artifacts"


# ---------------------------------------------------------------------------
# error projection
# ---------------------------------------------------------------------------


async def test_artifact_status_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    def _raise(*_a: Any, **_k: Any) -> Any:
        raise ArtifactNotFoundError(TASK_ID)

    mock_client.artifacts.poll_status = AsyncMock(side_effect=_raise)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("artifact_status", {"notebook": NB_ID, "task_id": TASK_ID})
    assert "NOT_FOUND" in str(excinfo.value)


async def test_artifact_list_notebook_not_found_projects_tool_error(mcp_call, mock_client) -> None:
    mock_client.notebooks.list = AsyncMock(return_value=[])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("artifact_list", {"notebook": "No Such Notebook"})
    assert "NOT_FOUND" in str(excinfo.value)
    _ = NotebookNotFoundError  # imported for symmetry with sibling suites
