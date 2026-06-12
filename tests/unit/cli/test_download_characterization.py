"""Characterization (golden-snapshot) tests for download CLI commands.

This file locks the observable CLI behavior of all 9 leaf download commands
(audio, video, slide-deck, infographic, report, mind-map, data-table, quiz,
flashcards) so the registry-driven implementation stays byte-for-byte
equivalent.

These characterization tests must keep passing without modification as
``download_cmd.py`` evolves — that is what makes them a behavioral safety net.

Coverage matrix (parametrized across the 9 leaf commands):

- happy path: single-artifact download to explicit path; JSON envelope shape
  ``{"operation": "download_single", "artifact": {...}, "output_path", "status": "downloaded"}``
  with no top-level ``error`` key; exit 0.
- missing-artifact: artifacts list empty for the target kind; JSON envelope
  ``{"error": "No completed <type> artifacts found", "suggestion": ...}``;
  exit 1.
- partial-failure (``--all``): one of two artifacts raises during download;
  JSON envelope ``{"error": True, "failed_count": 1, "succeeded_count": 1,
  "artifacts": [...]}``; exit non-zero.
- dry-run: ``--dry-run`` on the single-artifact path; JSON envelope
  ``{"dry_run": True, "operation": "download_single", "artifact": {...},
  "output_path": <derived>}``; exit 0; no download_<kind> call.
- ``--all``: full happy ``--all`` with 2 artifacts; JSON envelope
  ``{"operation": "download_all", "succeeded_count": 2, "failed_count": 0,
  "skipped_count": 0, "artifacts": [...]}``; exit 0; two files written.

The format-bearing commands (slide-deck, quiz, flashcards) get extra checks
in their happy-path entries that exercise ``--format`` extension override —
this is the only behavioral axis the 9-leaf registry must preserve beyond the
shared options block.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
import notebooklm.cli.helpers as helpers_module
from notebooklm.notebooklm_cli import cli
from notebooklm.types import Artifact

from .conftest import create_mock_client, inject_client


# Map command name → (ArtifactTypeCode raw value, default extension, download method attr,
# optional _variant override for QUIZ/FLASHCARDS).
# Raw values from src/notebooklm/rpc/types.py::ArtifactTypeCode:
#   AUDIO=1, REPORT=2, VIDEO=3, QUIZ=4 (+ variant: 1=flashcards / 2=quiz),
#   MIND_MAP=5, INFOGRAPHIC=7, SLIDE_DECK=8, DATA_TABLE=9.
@dataclass(frozen=True)
class _CmdSpec:
    name: str
    artifact_type: int
    extension: str
    download_attr: str
    # Variant override; only QUIZ (artifact_type=4) cares — variant=2 → QUIZ,
    # variant=1 → FLASHCARDS. Default None → leave the Artifact default.
    variant: int | None = None
    # Extra args appended to every invocation (e.g. format choice).
    format_args: tuple[str, ...] = ()
    # If set, overrides ``extension`` for the happy-path filename check.
    format_extension: str | None = None


_LEAF_COMMANDS: list[_CmdSpec] = [
    _CmdSpec("audio", 1, ".mp3", "download_audio"),
    _CmdSpec("video", 3, ".mp4", "download_video"),
    _CmdSpec("infographic", 7, ".png", "download_infographic"),
    _CmdSpec("slide-deck", 8, ".pdf", "download_slide_deck"),
    _CmdSpec("report", 2, ".md", "download_report"),
    _CmdSpec("mind-map", 5, ".json", "download_mind_map"),
    _CmdSpec("data-table", 9, ".csv", "download_data_table"),
    _CmdSpec("quiz", 4, ".json", "download_quiz", variant=2),
    _CmdSpec("flashcards", 4, ".json", "download_flashcards", variant=1),
]

# Format-bearing extras to verify the runtime extension-override path.
_FORMAT_VARIANTS: list[_CmdSpec] = [
    _CmdSpec(
        "slide-deck",
        8,
        ".pdf",
        "download_slide_deck",
        format_args=("--format", "pptx"),
        format_extension=".pptx",
    ),
    _CmdSpec(
        "quiz",
        4,
        ".json",
        "download_quiz",
        variant=2,
        format_args=("--format", "markdown"),
        format_extension=".md",
    ),
    _CmdSpec(
        "quiz",
        4,
        ".json",
        "download_quiz",
        variant=2,
        format_args=("--format", "html"),
        format_extension=".html",
    ),
    _CmdSpec(
        "flashcards",
        4,
        ".json",
        "download_flashcards",
        variant=1,
        format_args=("--format", "markdown"),
        format_extension=".md",
    ),
    _CmdSpec(
        "flashcards",
        4,
        ".json",
        "download_flashcards",
        variant=1,
        format_args=("--format", "html"),
        format_extension=".html",
    ),
]


def _make_artifact(
    id: str,
    title: str,
    spec: _CmdSpec,
    created_at: datetime | None = None,
) -> Artifact:
    kwargs: dict[str, Any] = {
        "id": id,
        "title": title,
        "_artifact_type": spec.artifact_type,
        "status": 3,  # COMPLETED
        "created_at": created_at or datetime.fromtimestamp(1234567890),
    }
    if spec.variant is not None:
        kwargs["_variant"] = spec.variant
    return Artifact(**kwargs)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _parse_json_stdout(result: Any) -> dict:
    """Parse the JSON envelope from the result, tolerating a stderr-only
    "output path does not end with .ext" warning prefix that some legacy
    commands (slide-deck, quiz, flashcards) emit via ``click.echo(..., err=True)``
    when the user passes an output path whose extension doesn't match the
    selected ``--format``.

    On Click 8.3 ``result.stdout`` and ``result.stderr`` are independent;
    on older Click ``result.output`` was the merged stream. Prefer the
    dedicated stdout stream when present and fall back to ``output``.
    """
    text = getattr(result, "stdout", None) or result.output
    # Tolerate any "Warning:" lines on the *out* stream too — strip them
    # heuristically and locate the first '{' as the JSON envelope start.
    brace_idx = text.find("{")
    if brace_idx > 0:
        text = text[brace_idx:]
    return json.loads(text)


@pytest.fixture
def mock_auth():
    """Stub auth loading + token fetching so the CLI workflow runs end-to-end."""
    from notebooklm.auth import AuthTokens

    auth = AuthTokens(
        cookies={
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        },
        csrf_token="csrf",
        session_id="session",
    )

    with (
        patch.object(helpers_module, "load_auth_from_storage") as mock_load,
        patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch,
    ):
        mock_load.return_value = auth.flat_cookies
        mock_fetch.return_value = ("csrf", "session")
        yield mock_load


def _install_client(
    spec: _CmdSpec,
    *,
    artifacts: list[Artifact] | None,
    download_fn: Callable[..., Any] | None,
) -> Any:
    """Build a ``create_mock_client`` instance wired with the test artifacts.

    Returns the ``mock_client`` so callers can pass it through
    ``inject_client(...)``; the Click adapter receives it via the client-factory
    seam.
    """
    mock_client = create_mock_client()
    mock_client.artifacts.list = AsyncMock(return_value=artifacts or [])
    if download_fn is not None:
        setattr(mock_client.artifacts, spec.download_attr, download_fn)
    return mock_client


def _ids(spec: _CmdSpec) -> str:
    """Stable, command-specific test ID for parametrize readability."""
    return spec.name + ("/" + "+".join(spec.format_args) if spec.format_args else "")


# ---------------------------------------------------------------------------
# Happy path — single artifact, explicit output path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _LEAF_COMMANDS, ids=_ids)
def test_happy_single_download(
    spec: _CmdSpec, runner: CliRunner, mock_auth: Any, tmp_path: Path
) -> None:
    """Lock the JSON envelope shape for the single-artifact happy path."""
    output_file = tmp_path / f"out{spec.extension}"

    async def fake_download(
        notebook_id: str, output_path: str, artifact_id: str | None = None, **_kw: Any
    ):
        Path(output_path).write_bytes(b"x")
        return output_path

    result = runner.invoke(
        cli,
        [
            "download",
            spec.name,
            str(output_file),
            "-n",
            "nb_123",
            "--json",
            *spec.format_args,
        ],
        obj=inject_client(
            _install_client(
                spec,
                artifacts=[_make_artifact(f"{spec.name}_1", "Sample Title", spec)],
                download_fn=fake_download,
            )
        ),
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json_stdout(result)
    assert payload["operation"] == "download_single"
    assert payload["artifact"]["id"] == f"{spec.name}_1"
    assert payload["artifact"]["title"] == "Sample Title"
    assert payload.get("status") == "downloaded"
    assert "error" not in payload
    assert Path(payload["output_path"]).exists()


@pytest.mark.parametrize("spec", _FORMAT_VARIANTS, ids=_ids)
def test_format_override_extension(
    spec: _CmdSpec, runner: CliRunner, mock_auth: Any, tmp_path: Path
) -> None:
    """``--format <fmt>`` for slide-deck/quiz/flashcards overrides the default
    extension when the user does not pass an explicit output path.

    Locks the runtime branch that swaps ``.pdf``/``.pptx`` and
    ``.json``/``.md``/``.html`` based on the format choice.
    """
    expected_ext = spec.format_extension or spec.extension

    async def fake_download(
        notebook_id: str, output_path: str, artifact_id: str | None = None, **_kw: Any
    ):
        Path(output_path).write_bytes(b"x")
        return output_path

    # Run inside an isolated cwd so the derived filename can land safely.
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            cli,
            [
                "download",
                spec.name,
                "-n",
                "nb_123",
                "--json",
                *spec.format_args,
            ],
            obj=inject_client(
                _install_client(
                    spec,
                    artifacts=[_make_artifact(f"{spec.name}_1", "Sample", spec)],
                    download_fn=fake_download,
                )
            ),
        )

        assert result.exit_code == 0, result.output
        payload = _parse_json_stdout(result)
        # The runtime-derived filename must carry the overridden extension.
        assert payload["output_path"].endswith(expected_ext), payload


# ---------------------------------------------------------------------------
# Missing-artifact path — empty completed-artifacts list.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _LEAF_COMMANDS, ids=_ids)
def test_missing_artifact_returns_error(
    spec: _CmdSpec, runner: CliRunner, mock_auth: Any, tmp_path: Path
) -> None:
    """Empty artifact list → ``{"error": "No completed <type> artifacts ..."}``
    + exit code 1."""
    output_file = tmp_path / f"out{spec.extension}"

    result = runner.invoke(
        cli,
        [
            "download",
            spec.name,
            str(output_file),
            "-n",
            "nb_123",
            "--json",
            *spec.format_args,
        ],
        obj=inject_client(_install_client(spec, artifacts=[], download_fn=None)),
    )

    assert result.exit_code == 1, result.output
    payload = _parse_json_stdout(result)
    assert isinstance(payload.get("error"), str)
    assert "No completed" in payload["error"]
    assert spec.name in payload["error"]
    assert "suggestion" in payload


# ---------------------------------------------------------------------------
# Partial-failure path under --all — exit-code + envelope contract (P1.T4).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _LEAF_COMMANDS, ids=_ids)
def test_partial_failure_under_all(
    spec: _CmdSpec, runner: CliRunner, mock_auth: Any, tmp_path: Path
) -> None:
    """``--all`` with 1 succeed + 1 fail: exit != 0, envelope reports both."""
    output_dir = tmp_path / "out"
    call_count = {"n": 0}

    async def fake_download(
        notebook_id: str, output_path: str, artifact_id: str | None = None, **_kw: Any
    ):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        Path(output_path).write_bytes(b"x")
        return output_path

    result = runner.invoke(
        cli,
        [
            "download",
            spec.name,
            "--all",
            "--json",
            str(output_dir),
            "-n",
            "nb_123",
            *spec.format_args,
        ],
        obj=inject_client(
            _install_client(
                spec,
                artifacts=[
                    _make_artifact(f"{spec.name}_a", "First", spec),
                    _make_artifact(f"{spec.name}_b", "Second", spec),
                ],
                download_fn=fake_download,
            )
        ),
    )

    assert result.exit_code != 0, result.output
    payload = _parse_json_stdout(result)
    assert payload["operation"] == "download_all"
    assert payload.get("error") is True
    assert payload["failed_count"] == 1
    assert payload["succeeded_count"] == 1
    assert len(payload["artifacts"]) == 2
    statuses = sorted(a["status"] for a in payload["artifacts"])
    assert statuses == ["downloaded", "failed"]


# ---------------------------------------------------------------------------
# Dry-run path — single artifact, no download invocation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _LEAF_COMMANDS, ids=_ids)
def test_dry_run_single(spec: _CmdSpec, runner: CliRunner, mock_auth: Any, tmp_path: Path) -> None:
    """``--dry-run`` reports the planned output_path without calling
    ``download_<kind>``."""
    output_file = tmp_path / f"out{spec.extension}"
    fake_download = AsyncMock()

    result = runner.invoke(
        cli,
        [
            "download",
            spec.name,
            str(output_file),
            "-n",
            "nb_123",
            "--json",
            "--dry-run",
            *spec.format_args,
        ],
        obj=inject_client(
            _install_client(
                spec,
                artifacts=[_make_artifact(f"{spec.name}_1", "Only", spec)],
                download_fn=fake_download,
            )
        ),
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json_stdout(result)
    assert payload["dry_run"] is True
    assert payload["operation"] == "download_single"
    assert payload["artifact"]["id"] == f"{spec.name}_1"
    assert payload["output_path"] == str(output_file)
    fake_download.assert_not_awaited()


# ---------------------------------------------------------------------------
# Happy --all path — N artifacts, all succeed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _LEAF_COMMANDS, ids=_ids)
def test_happy_all_path(spec: _CmdSpec, runner: CliRunner, mock_auth: Any, tmp_path: Path) -> None:
    """``--all`` happy path: every artifact downloaded; exit 0; envelope
    omits the ``error`` key."""
    output_dir = tmp_path / "out"

    async def fake_download(
        notebook_id: str, output_path: str, artifact_id: str | None = None, **_kw: Any
    ):
        Path(output_path).write_bytes(b"x")
        return output_path

    result = runner.invoke(
        cli,
        [
            "download",
            spec.name,
            "--all",
            "--json",
            str(output_dir),
            "-n",
            "nb_123",
            *spec.format_args,
        ],
        obj=inject_client(
            _install_client(
                spec,
                artifacts=[
                    _make_artifact(f"{spec.name}_a", "First", spec),
                    _make_artifact(f"{spec.name}_b", "Second", spec),
                ],
                download_fn=fake_download,
            )
        ),
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json_stdout(result)
    assert payload["operation"] == "download_all"
    assert "error" not in payload
    assert payload["total"] == 2
    assert payload["succeeded_count"] == 2
    assert payload["failed_count"] == 0
    assert payload["skipped_count"] == 0
    # Both files exist on disk with the expected extension (default unless
    # format-override was active).
    expected_ext = spec.format_extension or spec.extension
    files = sorted(output_dir.glob(f"*{expected_ext}"))
    assert len(files) == 2


# ---------------------------------------------------------------------------
# cinematic-video alias — locks the historical behavior that this alias maps
# to the video download path (P3.T2 keeps it as a click alias, not an entry
# in the DownloadTypeSpec registry).
# ---------------------------------------------------------------------------


def test_cinematic_video_alias_is_video(runner: CliRunner, mock_auth: Any, tmp_path: Path) -> None:
    """``download cinematic-video`` and ``download video`` route to the same
    download method (``ArtifactType.VIDEO``)."""
    output_file = tmp_path / "out.mp4"

    seen_method: list[str] = []

    async def fake_download(
        notebook_id: str, output_path: str, artifact_id: str | None = None, **_kw: Any
    ):
        seen_method.append("video")
        Path(output_path).write_bytes(b"x")
        return output_path

    video_spec = _CmdSpec("video", 3, ".mp4", "download_video")
    mock_client = create_mock_client()
    mock_client.artifacts.list = AsyncMock(
        return_value=[_make_artifact("video_1", "Cinematic Pilot", video_spec)]
    )
    mock_client.artifacts.download_video = fake_download

    result = runner.invoke(
        cli,
        ["download", "cinematic-video", str(output_file), "-n", "nb_123", "--json"],
        obj=inject_client(mock_client),
    )

    assert result.exit_code == 0, result.output
    payload = _parse_json_stdout(result)
    assert payload["operation"] == "download_single"
    assert payload["artifact"]["id"] == "video_1"
    assert seen_method == ["video"]


# ---------------------------------------------------------------------------
# Group-level help text — every leaf command surfaces under `download --help`.
# ---------------------------------------------------------------------------


def test_all_leaf_commands_registered(runner: CliRunner) -> None:
    """The 9 leaf commands + ``cinematic-video`` alias must all appear in the
    ``download`` subgroup. Locks the click-group registration surface."""
    result = runner.invoke(cli, ["download", "--help"])
    assert result.exit_code == 0
    for spec in _LEAF_COMMANDS:
        assert spec.name in result.output, f"{spec.name} missing from `download --help` output"
    assert "cinematic-video" in result.output
