"""Direct service-layer tests for ``cli/services/download.py``.

These cover the P3.T2 public service surface without going through
``CliRunner`` or Click command registration.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from notebooklm.cli._download_specs import DOWNLOAD_SPECS_BY_NAME
from notebooklm.cli.services import download as download_service
from notebooklm.cli.services.download import (
    DownloadPlanValidationError,
    build_download_plan,
    execute_download,
)
from notebooklm.types import Artifact


def _artifact(
    artifact_id: str,
    title: str,
    artifact_type: int,
    *,
    variant: int | None = None,
    status: int = 3,
) -> Artifact:
    return Artifact(
        id=artifact_id,
        title=title,
        _artifact_type=artifact_type,
        _variant=variant,
        status=status,
    )


def _args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "output_path": None,
        "notebook_id": "nb_123",
        "latest": False,
        "earliest": False,
        "download_all": False,
        "name": None,
        "artifact_id": None,
        "json_output": False,
        "dry_run": False,
        "force": False,
        "no_clobber": False,
    }
    args.update(overrides)
    return args


@pytest.fixture(autouse=True)
def resolved_notebook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        download_service,
        "resolve_notebook_id",
        AsyncMock(return_value="nb_resolved"),
    )


def test_build_download_plan_rejects_force_and_no_clobber() -> None:
    spec = DOWNLOAD_SPECS_BY_NAME["audio"]

    with pytest.raises(
        DownloadPlanValidationError, match="Cannot specify both --force and --no-clobber"
    ):
        build_download_plan(spec, _args(force=True, no_clobber=True))


def test_build_download_plan_applies_registry_format_extension_and_warning() -> None:
    spec = DOWNLOAD_SPECS_BY_NAME["slide-deck"]

    plan = build_download_plan(
        spec,
        _args(output_path="deck.pdf", slide_format="pptx"),
        Path("/workspace"),
    )

    assert plan.file_extension == ".pptx"
    assert plan.format_choice == "pptx"
    assert list(plan.warnings) == [
        "Warning: output path 'deck.pdf' does not end with '.pptx' but --format pptx was requested."
    ]


@pytest.mark.asyncio
async def test_execute_download_all_dry_run_applies_name_filter_and_duplicate_filenames(
    tmp_path: Path,
) -> None:
    spec = DOWNLOAD_SPECS_BY_NAME["audio"]
    artifacts = SimpleNamespace(
        list=AsyncMock(
            return_value=[
                _artifact("a1", "Episode", 1),
                _artifact("a2", "Episode", 1),
                _artifact("a3", "Trailer", 1),
            ]
        ),
        download_audio=AsyncMock(),
    )
    facade = SimpleNamespace(artifacts=artifacts)
    plan = build_download_plan(
        spec,
        _args(
            output_path=str(tmp_path / "downloads"),
            download_all=True,
            name="episode",
            dry_run=True,
        ),
        tmp_path,
    )

    result = await execute_download(plan, facade)

    assert result == {
        "dry_run": True,
        "operation": "download_all",
        "count": 2,
        "output_dir": str(tmp_path / "downloads"),
        "artifacts": [
            {"id": "a1", "title": "Episode", "filename": "Episode.mp3"},
            {"id": "a2", "title": "Episode", "filename": "Episode (2).mp3"},
        ],
    }
    artifacts.download_audio.assert_not_called()


@pytest.mark.asyncio
async def test_execute_download_all_reports_partial_failure_and_progress(tmp_path: Path) -> None:
    spec = DOWNLOAD_SPECS_BY_NAME["audio"]
    artifacts = SimpleNamespace(
        list=AsyncMock(
            return_value=[
                _artifact("a1", "Episode 1", 1),
                _artifact("a2", "Episode 2", 1),
            ]
        ),
        download_audio=AsyncMock(
            side_effect=[str(tmp_path / "Episode 1.mp3"), RuntimeError("boom")]
        ),
    )
    facade = SimpleNamespace(artifacts=artifacts)
    plan = build_download_plan(
        spec,
        _args(output_path=str(tmp_path), download_all=True),
        tmp_path,
    )
    progress: list[str] = []

    result = await execute_download(plan, facade, text_progress_sink=progress.append)

    assert result["error"] is True
    assert result["succeeded_count"] == 1
    assert result["failed_count"] == 1
    assert result["skipped_count"] == 0
    assert [item["status"] for item in result["artifacts"]] == ["downloaded", "failed"]
    assert result["artifacts"][1]["error"] == "boom"
    assert progress == [
        "[dim]Downloading 1/2:[/dim] Episode 1",
        "[dim]Downloading 2/2:[/dim] Episode 2",
    ]


@pytest.mark.asyncio
async def test_execute_download_single_forwards_format_kwarg(tmp_path: Path) -> None:
    spec = DOWNLOAD_SPECS_BY_NAME["quiz"]
    artifacts = SimpleNamespace(
        list=AsyncMock(return_value=[_artifact("quiz_1", "Quiz", 4, variant=2)]),
        download_quiz=AsyncMock(return_value=str(tmp_path / "quiz.md")),
    )
    facade = SimpleNamespace(artifacts=artifacts)
    plan = build_download_plan(
        spec,
        _args(output_path=str(tmp_path / "quiz.md"), output_format="markdown"),
        tmp_path,
    )

    result = await execute_download(plan, facade)

    assert result["status"] == "downloaded"
    artifacts.download_quiz.assert_awaited_once_with(
        "nb_resolved",
        str(tmp_path / "quiz.md"),
        artifact_id="quiz_1",
        output_format="markdown",
    )
