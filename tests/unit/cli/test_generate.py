"""Tests for generate CLI commands."""

import asyncio
import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
from notebooklm._app.generate_retry import (
    GenerationOutcome,
)
from notebooklm.cli import helpers as helpers_module
from notebooklm.cli.polling_ui import status_with_elapsed
from notebooklm.notebooklm_cli import cli
from notebooklm.rpc.types import ReportFormat

from .conftest import create_mock_client, inject_client, mind_map_result

# ``notebooklm.cli.generate_cmd`` (the module) is shadowed by ``cli.__init__``'s
# re-export of the ``generate`` Click Group (same name). Use ``importlib`` so
# tests target the module's attribute set (``console``, helpers) rather than
# the Click Group sitting at the same dotted path.
generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
polling_ui_module = importlib.import_module("notebooklm.cli.polling_ui")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_auth():
    with patch.object(helpers_module, "load_auth_from_storage") as mock:
        mock.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield mock


# =============================================================================
# PER-TYPE SMOKE TESTS (PARAMETRIZED)
# =============================================================================
#
# The bare "patch client -> mock generate_<type> -> invoke -> assert" smoke
# tests for every artifact type collapse into one parametrize over
# ``(cmd, method, task_id, extra_args)`` crossed with text/JSON output mode.
# Each row exercises both the text-mode happy path (exit 0 + task id surfaced)
# and the ``--json`` envelope (parseable ``task_id``), replacing the former
# per-type ``test_generate_<type>`` + ``TestGenerateJsonOutput`` clusters
# (issues #1315 and #1317). Tests that assert option-specific kwargs, distinct
# return structures, or wait/timeout behavior remain standalone below.
# (cmd, method, task_id, extra_args) — extra_args carries the required
# positional description for commands that need one (data-table).
_STANDARD_GENERATE_CASES = [
    ("audio", "generate_audio", "audio_123", []),
    ("video", "generate_video", "video_123", []),
    ("cinematic-video", "generate_cinematic_video", "cin_123", []),
    ("quiz", "generate_quiz", "quiz_123", []),
    ("flashcards", "generate_flashcards", "flash_123", []),
    ("slide-deck", "generate_slide_deck", "slides_123", []),
    ("infographic", "generate_infographic", "info_123", []),
    ("data-table", "generate_data_table", "table_123", ["Compare key concepts"]),
    ("report", "generate_report", "report_123", []),
]


class TestGenerateStandardTypes:
    """Per-type happy-path smoke coverage across text and JSON output modes."""

    @pytest.mark.parametrize("output_mode", ["text", "json"])
    @pytest.mark.parametrize(
        "cmd,method,task_id,extra_args",
        _STANDARD_GENERATE_CASES,
        ids=[case[0] for case in _STANDARD_GENERATE_CASES],
    )
    def test_generate_standard_type(
        self, runner, mock_auth, output_mode, cmd, method, task_id, extra_args
    ):
        mock_client = create_mock_client()
        setattr(
            mock_client.artifacts,
            method,
            AsyncMock(return_value={"task_id": task_id, "status": "processing"}),
        )
        args = ["generate", cmd, *extra_args, "-n", "nb_123"]
        if output_mode == "json":
            args.append("--json")
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(cli, args, obj=inject_client(mock_client))
        assert result.exit_code == 0, result.output
        if output_mode == "json":
            data = json.loads(result.output)
            assert data["task_id"] == task_id
        else:
            assert task_id in result.output or "Started" in result.output


# =============================================================================
# GENERATE AUDIO TESTS
# =============================================================================
class TestGenerateAudio:
    def test_generate_audio_with_format(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(
            return_value={"artifact_id": "audio_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "audio", "--format", "debate", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        mock_client.artifacts.generate_audio.assert_called()

    def test_generate_audio_with_length(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(
            return_value={"artifact_id": "audio_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "audio", "--length", "long", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0

    def test_generate_audio_with_wait(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(
            return_value={"artifact_id": "audio_123", "status": "processing"}
        )
        completed_status = MagicMock()
        completed_status.is_complete = True
        completed_status.is_failed = False
        completed_status.is_removed = False
        completed_status.url = "https://example.com/audio.mp3"
        completed_status.artifact_id = "audio_123"
        mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "--wait", "-n", "nb_123"], obj=inject_client(mock_client)
            )
        assert result.exit_code == 0
        assert "Audio ready" in result.output
        assert "https://example.com/audio.mp3" in result.output
        mock_client.artifacts.wait_for_completion.assert_awaited_once()
        kwargs = mock_client.artifacts.wait_for_completion.await_args.kwargs
        assert kwargs.get("timeout") == 1200.0

    def test_generate_audio_with_wait_timeout_interval_forwarded(self, runner, mock_auth):
        """`generate audio --wait --timeout 60 --interval 5` plumbs both into
        artifacts.wait_for_completion.
        The new `--timeout`/`--interval` flags must reach the polling call so
        that scripts can bound the wait and the cadence — not just toggle the
        wait on/off as the legacy `--wait` flag did. The CLI surface is
        uniform with `artifact wait` / `source wait` after this change.
        """
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(
            return_value={"artifact_id": "audio_xyz", "status": "processing"}
        )
        completed_status = MagicMock()
        completed_status.is_complete = True
        completed_status.is_failed = False
        completed_status.is_removed = False
        completed_status.url = "https://example.com/audio.mp3"
        completed_status.task_id = "audio_xyz"
        mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "audio",
                    "--wait",
                    "--timeout",
                    "60",
                    "--interval",
                    "5",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0, result.output
        mock_client.artifacts.wait_for_completion.assert_awaited_once()
        kwargs = mock_client.artifacts.wait_for_completion.await_args.kwargs
        assert kwargs.get("timeout") == 60.0
        assert kwargs.get("initial_interval") == 5.0, (
            f"expected --interval=5 to plumb into wait_for_completion, got kwargs={kwargs}"
        )
        assert "poll_interval" not in kwargs

    def test_generate_audio_timeout_interval_without_wait_is_no_op(self, runner, mock_auth):
        """`generate audio --timeout 60 --interval 5` (without --wait) is
        accepted but does not call wait_for_completion.
        The polling flags only take effect when paired with --wait; supplying
        them without --wait must NOT trigger a wait (preserves the default
        no-wait behavior promised by the original `--wait/--no-wait` toggle).
        """
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(
            return_value={"artifact_id": "audio_xyz", "status": "processing"}
        )
        mock_client.artifacts.wait_for_completion = AsyncMock()
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "audio",
                    "--timeout",
                    "60",
                    "--interval",
                    "5",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0, result.output
        mock_client.artifacts.wait_for_completion.assert_not_awaited()

    def test_generate_audio_with_wait_invokes_console_status(self, runner, mock_auth):
        """`generate audio --wait` wraps the polling call in `console.status`.
        The spinner gives interactive users feedback during the long wait, with
        a transient line naming the artifact kind (and a typical-duration hint).
        Asserts the wrap by patching `notebooklm.cli.polling_ui.console.status`
        and confirming it is invoked exactly once with a message that mentions
        the artifact kind. Does not assert the elapsed-timer ticker — that's a
        rendering detail that relies on a TTY which `CliRunner` doesn't have.
        """
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(
            return_value={"artifact_id": "audio_xyz", "status": "processing"}
        )
        completed_status = MagicMock()
        completed_status.is_complete = True
        completed_status.is_failed = False
        completed_status.is_removed = False
        completed_status.url = "https://example.com/audio.mp3"
        completed_status.task_id = "audio_xyz"
        mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
        with (
            patch.object(
                auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(polling_ui_module.console, "status") as mock_status,
        ):
            mock_fetch.return_value = ("csrf", "session")
            # ``console.status`` returns a context manager; emulate one so
            # the wrapped polling call still runs.
            mock_status.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_status.return_value.__exit__ = MagicMock(return_value=False)
            result = runner.invoke(
                cli, ["generate", "audio", "--wait", "-n", "nb_123"], obj=inject_client(mock_client)
            )
        assert result.exit_code == 0, result.output
        assert mock_status.called, "expected console.status to wrap the --wait polling call"
        status_msg = mock_status.call_args.args[0]
        assert "audio" in status_msg.lower(), (
            f"expected status message to mention artifact kind 'audio', got: {status_msg!r}"
        )

    def test_generate_audio_failure(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=None)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123"], obj=inject_client(mock_client)
            )
        # P1.T6: failed generation now exits non-zero in text mode (was 0
        # pre-fix). Message lands on stderr via ``output_error`` →
        # ``safe_echo(err=True)``.
        assert result.exit_code != 0
        assert "Audio generation failed" in result.stderr


# =============================================================================
# GENERATE VIDEO TESTS
# =============================================================================
class TestGenerateVideo:
    def test_generate_video_with_style(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_video = AsyncMock(
            return_value={"artifact_id": "video_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "video", "--style", "kawaii", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0

    def test_generate_video_with_custom_style_prompt(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_video = AsyncMock(
            return_value={"artifact_id": "video_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "video",
                    "--style",
                    "custom",
                    "--style-prompt",
                    "  Use hand-drawn diagrams  ",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        mock_client.artifacts.generate_video.assert_awaited_once()
        kwargs = mock_client.artifacts.generate_video.await_args.kwargs
        assert kwargs["video_style"].name == "CUSTOM"
        assert kwargs["style_prompt"] == "Use hand-drawn diagrams"

    def test_generate_video_custom_style_requires_prompt(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        result = runner.invoke(
            cli,
            ["generate", "video", "--style", "custom", "-n", "nb_123"],
        )
        # Per ADR-0015, post-parse validation failures exit 1 via
        # ``output_error`` (VALIDATION_ERROR), not 2 via Click's UsageError.
        assert result.exit_code == 1
        assert "--style custom requires --style-prompt" in result.output

    def test_generate_video_custom_style_rejects_blank_prompt(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        result = runner.invoke(
            cli,
            [
                "generate",
                "video",
                "--style",
                "custom",
                "--style-prompt",
                "   ",
                "-n",
                "nb_123",
            ],
        )
        assert result.exit_code == 1
        assert "--style custom requires --style-prompt" in result.output

    def test_generate_video_style_prompt_requires_custom_style(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        result = runner.invoke(
            cli,
            [
                "generate",
                "video",
                "--style",
                "anime",
                "--style-prompt",
                "Use hand-drawn diagrams",
                "-n",
                "nb_123",
            ],
        )
        assert result.exit_code == 1
        assert "--style-prompt requires --style custom" in result.output


# =============================================================================
# GENERATE CINEMATIC VIDEO TESTS
# =============================================================================
class TestGenerateCinematicVideo:
    def test_generate_cinematic_video_with_description(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_cinematic_video = AsyncMock(
            return_value={"artifact_id": "cin_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "cinematic-video",
                    "documentary about quantum physics",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0

    def test_generate_cinematic_video_ignores_style(self, runner, mock_auth):
        """Cinematic video accepts --style (inherited from video) but ignores it."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_cinematic_video = AsyncMock(
            return_value={"artifact_id": "cin_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "cinematic-video", "--style", "anime", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        # Should call generate_cinematic_video (not generate_video) despite --style
        mock_client.artifacts.generate_cinematic_video.assert_called_once()

    def test_generate_cinematic_video_rejects_style_prompt(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        result = runner.invoke(
            cli,
            [
                "generate",
                "cinematic-video",
                "--style-prompt",
                "Use hand-drawn diagrams",
                "-n",
                "nb_123",
            ],
        )
        # Per ADR-0015, post-parse validation exits 1 via ``output_error``.
        assert result.exit_code == 1
        assert "--style-prompt cannot be used with cinematic video" in result.output

    def test_generate_cinematic_video_rejects_non_cinematic_format(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """`cinematic-video --format explainer` (or any non-cinematic value) is
        rejected through ``output_error`` (per ADR-0015) — exit 1, not a silent
        format override."""
        for bad_format in ("explainer", "brief"):
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "cinematic-video",
                    "--format",
                    bad_format,
                    "-n",
                    "nb_123",
                ],
            )
            assert result.exit_code == 1, (
                f"--format {bad_format} should exit 1, got {result.exit_code}: {result.output}"
            )
            assert "--format" in result.output
            assert "cinematic" in result.output.lower()

    def test_generate_cinematic_video_explicit_cinematic_format_ok(self, runner, mock_auth):
        """`cinematic-video --format cinematic` is the canonical happy path."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_cinematic_video = AsyncMock(
            return_value={"artifact_id": "cin_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "cinematic-video",
                    "--format",
                    "cinematic",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0, result.output
        mock_client.artifacts.generate_cinematic_video.assert_called_once()

    def test_generate_cinematic_video_default_format_ok(self, runner, mock_auth):
        """`cinematic-video` with no --format defaults to cinematic and works."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_cinematic_video = AsyncMock(
            return_value={"artifact_id": "cin_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "cinematic-video", "-n", "nb_123"], obj=inject_client(mock_client)
            )
        assert result.exit_code == 0, result.output
        mock_client.artifacts.generate_cinematic_video.assert_called_once()

    def test_generate_cinematic_video_help_documents_format_constraint(self, runner):
        """`cinematic-video --help` must surface the --format constraint."""
        result = runner.invoke(cli, ["generate", "cinematic-video", "--help"])
        assert result.exit_code == 0
        # The help should make it explicit that --format must be 'cinematic' for
        # this subcommand.
        assert "--format" in result.output
        assert "cinematic" in result.output.lower()
        assert "cinematic format defaults to 3600" in result.output


# =============================================================================
# GENERATE QUIZ TESTS
# =============================================================================
class TestGenerateQuiz:
    def test_generate_quiz_with_options(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_quiz = AsyncMock(
            return_value={"artifact_id": "quiz_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "quiz",
                    "--quantity",
                    "more",
                    "--difficulty",
                    "hard",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0


# =============================================================================
# GENERATE SLIDE DECK TESTS
# =============================================================================
class TestGenerateSlideDeck:
    def test_generate_slide_deck_with_options(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_slide_deck = AsyncMock(
            return_value={"artifact_id": "slides_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "slide-deck",
                    "--format",
                    "presenter",
                    "--length",
                    "short",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0


# =============================================================================
# GENERATE INFOGRAPHIC TESTS
# =============================================================================
class TestGenerateInfographic:
    def test_generate_infographic_with_options(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_infographic = AsyncMock(
            return_value={"artifact_id": "info_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "infographic",
                    "--orientation",
                    "portrait",
                    "--detail",
                    "detailed",
                    "--style",
                    "anime",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        mock_client.artifacts.generate_infographic.assert_awaited_once()
        kwargs = mock_client.artifacts.generate_infographic.await_args.kwargs
        assert kwargs["orientation"].name == "PORTRAIT"
        assert kwargs["detail_level"].name == "DETAILED"
        assert kwargs["style"].name == "ANIME"


# =============================================================================
# GENERATE MIND MAP TESTS
# =============================================================================
class TestGenerateMindMap:
    def test_generate_mind_map_note_backed(self, runner, mock_auth):
        """--kind note-backed routes through client.artifacts.generate_mind_map."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_mind_map = AsyncMock(
            return_value=mind_map_result(
                {"mind_map": {"name": "Root", "children": []}, "note_id": "n1"}
            )
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "mind-map", "--kind", "note-backed", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        mock_client.artifacts.generate_mind_map.assert_awaited_once()
        mock_client.mind_maps.generate.assert_not_called()

    def test_generate_mind_map_interactive(self, runner, mock_auth):
        """--interactive routes through client.mind_maps.generate(kind=INTERACTIVE)."""
        from notebooklm.types import MindMap, MindMapKind

        mock_client = create_mock_client()
        mock_client.mind_maps.generate = AsyncMock(
            return_value=MindMap(
                id="art_42",
                notebook_id="nb_123",
                title="Interactive Mind Map",
                kind=MindMapKind.INTERACTIVE,
            )
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "mind-map", "--kind", "interactive", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        # Interactive path dispatches to the unified API, not the note-backed
        # artifacts.generate_mind_map.
        mock_client.artifacts.generate_mind_map.assert_not_called()
        mock_client.mind_maps.generate.assert_awaited_once()
        assert mock_client.mind_maps.generate.await_args.kwargs["kind"] == MindMapKind.INTERACTIVE
        assert "art_42" in result.output

    def test_generate_mind_map_interactive_json(self, runner, mock_auth):
        """--kind interactive --json emits the converged {mind_map, note_id, kind} shape."""
        from notebooklm.types import MindMap, MindMapKind

        mock_client = create_mock_client()
        mock_client.mind_maps.generate = AsyncMock(
            return_value=MindMap(
                id="art_42",
                notebook_id="nb_123",
                title="Interactive Mind Map",
                kind=MindMapKind.INTERACTIVE,
                tree={"name": "Root", "children": []},
            )
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "mind-map", "--kind", "interactive", "--json", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        # Converged shape: id under note_id, tree under mind_map, plus kind.
        assert data["note_id"] == "art_42"
        assert data["kind"] == "interactive"
        assert data["mind_map"] == {"name": "Root", "children": []}

    def test_generate_mind_map_interactive_forwards_instructions(self, runner, mock_auth):
        """--kind interactive with --instructions forwards the prompt (no drop/warning).

        The interactive CREATE_ARTIFACT payload carries a prompt slot at
        ``[9][1][2]`` that the server honors for variant 4 (verified live), so the
        CLI must thread ``--instructions`` through instead of dropping it.
        """
        from notebooklm.types import MindMap, MindMapKind

        mock_client = create_mock_client()
        mock_client.mind_maps.generate = AsyncMock(
            return_value=MindMap(
                id="art_42",
                notebook_id="nb_123",
                title="Interactive Mind Map",
                kind=MindMapKind.INTERACTIVE,
            )
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "mind-map",
                    "--kind",
                    "interactive",
                    "--instructions",
                    "focus on chapter 3",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        assert "--instructions is ignored" not in result.output
        # Behaviour: the interactive generator call forwards the prompt.
        mock_client.mind_maps.generate.assert_awaited_once()
        call_kwargs = mock_client.mind_maps.generate.await_args.kwargs
        assert call_kwargs.get("instructions") == "focus on chapter 3"

    def test_generate_mind_map_interactive_json_forwards_instructions(self, runner, mock_auth):
        """Under --json the interactive prompt is forwarded and stdout stays pure JSON.

        No behavioral warning is emitted anymore (the prompt is applied, not
        dropped), and stdout remains a parseable JSON payload.
        """
        from notebooklm.types import MindMap, MindMapKind

        mock_client = create_mock_client()
        mock_client.mind_maps.generate = AsyncMock(
            return_value=MindMap(
                id="art_42",
                notebook_id="nb_123",
                title="Interactive Mind Map",
                kind=MindMapKind.INTERACTIVE,
            )
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "mind-map",
                    "--kind",
                    "interactive",
                    "--instructions",
                    "focus on chapter 3",
                    "-n",
                    "nb_123",
                    "--json",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        # No dropped-instructions warning anywhere (the prompt is applied now).
        assert "--instructions is ignored" not in result.stderr
        assert "--instructions is ignored" not in result.stdout
        # stdout stays pure, parseable JSON.
        payload = json.loads(result.stdout)
        assert payload["kind"] == "interactive"
        # Behaviour: instructions ARE forwarded to the interactive generator.
        mock_client.mind_maps.generate.assert_awaited_once()
        assert (
            mock_client.mind_maps.generate.await_args.kwargs.get("instructions")
            == "focus on chapter 3"
        )

    def test_generate_mind_map_default_routes_interactive(self, runner, mock_auth):
        """Omitting --kind now defaults to the interactive studio-artifact path (#1272)."""
        from notebooklm.types import MindMap, MindMapKind

        mock_client = create_mock_client()
        mock_client.mind_maps.generate = AsyncMock(
            return_value=MindMap(
                id="art_42",
                notebook_id="nb_123",
                title="Interactive Mind Map",
                kind=MindMapKind.INTERACTIVE,
            )
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "mind-map", "-n", "nb_123"], obj=inject_client(mock_client)
            )
        assert result.exit_code == 0
        # The bare default dispatches to the unified interactive API, not the
        # note-backed artifacts.generate_mind_map.
        mock_client.mind_maps.generate.assert_awaited_once()
        assert mock_client.mind_maps.generate.await_args.kwargs["kind"] == MindMapKind.INTERACTIVE
        mock_client.artifacts.generate_mind_map.assert_not_called()
        assert "art_42" in result.output


# =============================================================================
# GENERATE REPORT TESTS
# =============================================================================
class TestGenerateReport:
    def test_generate_report_study_guide(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_report = AsyncMock(
            return_value={"artifact_id": "report_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "report", "--format", "study-guide", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0

    def test_generate_report_custom(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.artifacts.generate_report = AsyncMock(
            return_value={"artifact_id": "report_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "report", "Create a white paper", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0

    @pytest.mark.parametrize(
        "format_name,extra_text,expected_format",
        [
            ("briefing-doc", "Focus on financial metrics", ReportFormat.BRIEFING_DOC),
            ("study-guide", "Target audience: beginners", ReportFormat.STUDY_GUIDE),
            ("blog-post", "Keep it conversational", ReportFormat.BLOG_POST),
        ],
    )
    def test_generate_report_append(
        self, runner, mock_auth, format_name, extra_text, expected_format
    ):
        """--append passes extra_instructions while keeping built-in format."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_report = AsyncMock(
            return_value={"artifact_id": "report_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "report",
                    "--format",
                    format_name,
                    "--append",
                    extra_text,
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        call_kwargs = mock_client.artifacts.generate_report.call_args.kwargs
        assert call_kwargs["extra_instructions"] == extra_text
        assert call_kwargs["report_format"] == expected_format
        assert call_kwargs["custom_prompt"] is None

    def test_generate_report_append_with_custom_warns(self, runner, mock_auth):
        """--append with --format custom prints a warning and clears extra_instructions."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_report = AsyncMock(
            return_value={"artifact_id": "report_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "report",
                    "--format",
                    "custom",
                    "--append",
                    "extra",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "--format custom" in result.output
        call_kwargs = mock_client.artifacts.generate_report.call_args.kwargs
        assert call_kwargs["extra_instructions"] is None

    def test_generate_report_append_with_description_warns(self, runner, mock_auth):
        """--append with a description arg (auto-promoted to custom) warns and clears extra_instructions."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_report = AsyncMock(
            return_value={"artifact_id": "report_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "report", "My custom prompt", "--append", "extra", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        assert "Warning" in result.output
        call_kwargs = mock_client.artifacts.generate_report.call_args.kwargs
        assert call_kwargs["extra_instructions"] is None
        assert call_kwargs["report_format"] == ReportFormat.CUSTOM


# =============================================================================
# JSON OUTPUT TESTS (MATERIALLY DISTINCT STRUCTURE)
# =============================================================================
#
# The standard-type ``--json`` cases (audio/video/.../data-table) are covered
# by ``TestGenerateStandardTypes`` above. Only mind-map keeps a dedicated JSON
# test here because its return payload (``mind_map`` + ``note_id``) is a
# materially different structure, not "same data, other format".
class TestGenerateJsonOutput:
    """JSON-output tests for commands whose envelope differs from the standard shape."""

    def test_generate_mind_map_note_backed_json_output(self, runner, mock_auth):
        """--kind note-backed --json emits the note-backed {mind_map, note_id} shape."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_mind_map = AsyncMock(
            return_value=mind_map_result(
                {"mind_map": {"name": "Root", "children": []}, "note_id": "n1"}
            )
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "mind-map", "--kind", "note-backed", "--json", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "mind_map" in data
        assert data["note_id"] == "n1"


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================
class TestGenerateCommandsExist:
    def test_generate_group_exists(self, runner):
        result = runner.invoke(cli, ["generate", "--help"])
        assert result.exit_code == 0
        assert "audio" in result.output
        assert "video" in result.output
        assert "quiz" in result.output

    def test_generate_audio_command_exists(self, runner):
        result = runner.invoke(cli, ["generate", "audio", "--help"])
        assert result.exit_code == 0
        assert "DESCRIPTION" in result.output
        assert "--notebook" in result.output or "-n" in result.output

    def test_generate_video_command_exists(self, runner):
        result = runner.invoke(cli, ["generate", "video", "--help"])
        assert result.exit_code == 0
        assert "DESCRIPTION" in result.output

    def test_generate_cinematic_video_command_exists(self, runner):
        result = runner.invoke(cli, ["generate", "cinematic-video", "--help"])
        assert result.exit_code == 0
        assert "cinematic" in result.output.lower()

    def test_generate_quiz_command_exists(self, runner):
        result = runner.invoke(cli, ["generate", "quiz", "--help"])
        assert result.exit_code == 0

    def test_generate_slide_deck_command_exists(self, runner):
        result = runner.invoke(cli, ["generate", "slide-deck", "--help"])
        assert result.exit_code == 0


# =============================================================================
# LANGUAGE VALIDATION TESTS
# =============================================================================
class TestGenerateLanguageValidation:
    def test_invalid_language_code_rejected(self, runner, mock_auth):
        """Test that invalid language codes are rejected with helpful error."""
        mock_client = create_mock_client()
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "audio", "-n", "nb_123", "--language", "invalid_code"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code != 0
        assert "Unknown language code: invalid_code" in result.output
        assert "notebooklm language list" in result.output

    def test_valid_language_code_accepted(self, runner, mock_auth):
        """Test that valid language codes are accepted."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(
            return_value={"artifact_id": "audio_123", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "audio", "-n", "nb_123", "--language", "ja"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0


# =============================================================================
# RETRY FUNCTIONALITY TESTS
#
# The pure ``calculate_backoff_delay`` / ``generate_with_retry`` tests moved to
# ``tests/unit/app/test_app_generate_retry.py`` (they call the ``_app`` function
# directly; the function is defined in ``_app/generate_retry.py``). The
# ``--retry`` Click *option* surface stays here.
# =============================================================================
class TestRetryOptionAvailable:
    """Test that --retry option is available on generate commands."""

    def test_retry_option_in_audio_help(self, runner):
        """Test --retry option appears in audio command help."""
        result = runner.invoke(cli, ["generate", "audio", "--help"])
        assert result.exit_code == 0
        assert "--retry" in result.output

    def test_retry_option_in_video_help(self, runner):
        """Test --retry option appears in video command help."""
        result = runner.invoke(cli, ["generate", "video", "--help"])
        assert result.exit_code == 0
        assert "--retry" in result.output

    def test_retry_option_in_cinematic_video_help(self, runner):
        """Test --retry option appears in cinematic-video command help."""
        result = runner.invoke(cli, ["generate", "cinematic-video", "--help"])
        assert result.exit_code == 0
        assert "--retry" in result.output

    def test_retry_option_in_slide_deck_help(self, runner):
        """Test --retry option appears in slide-deck command help."""
        result = runner.invoke(cli, ["generate", "slide-deck", "--help"])
        assert result.exit_code == 0
        assert "--retry" in result.output

    def test_retry_option_in_quiz_help(self, runner):
        """Test --retry option appears in quiz command help."""
        result = runner.invoke(cli, ["generate", "quiz", "--help"])
        assert result.exit_code == 0
        assert "--retry" in result.output


class TestRateLimitDetection:
    """Test rate limit detection in handle_generation_result."""

    def test_rate_limit_message_shown(self, runner, mock_auth):
        """Test that rate limit error shows proper message."""
        from notebooklm.types import GenerationStatus

        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=rate_limited)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123"], obj=inject_client(mock_client)
            )
        assert "rate limited by Google" in result.output
        assert "--retry" in result.output

    def test_rate_limit_json_output(self, runner, mock_auth):
        """Test that rate limit error produces correct JSON output."""
        from notebooklm.types import GenerationStatus

        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=rate_limited)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
            )
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "RATE_LIMITED"


# =============================================================================
# RESOLVE_LANGUAGE DIRECT TESTS
# =============================================================================
class TestResolveLanguageDirect:
    """Direct tests for resolve_language() covering uncovered branches."""

    def test_invalid_language_exits_via_output_error(self, capsys):
        """Invalid language code routes through ``output_error`` (per ADR-0015):
        exit 1, message on stderr. Replaces the old ``click.BadParameter``
        contract — the post-parse JSON envelope contract supersedes it."""
        import importlib

        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with pytest.raises(SystemExit) as exc_info:
            generate_module.resolve_language("xx_INVALID")
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Unknown language code: xx_INVALID" in captured.err
        assert "notebooklm language list" in captured.err

    def test_none_language_with_config_returns_config(self):
        """language is None, config_lang is not None -> returns config_lang."""
        import importlib

        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with patch.object(generate_module, "get_language", return_value="fr"):
            result = generate_module.resolve_language(None)
        assert result == "fr"

    def test_none_language_no_config_returns_default(self):
        """language is None and config_lang is None -> returns DEFAULT_LANGUAGE."""
        import importlib

        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with patch.object(generate_module, "get_language", return_value=None):
            result = generate_module.resolve_language(None)
        assert result == "en"

    def test_env_overrides_config(self, monkeypatch):
        """NOTEBOOKLM_HL set, config also set, no flag → env wins over config."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "ja")
        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with patch.object(generate_module, "get_language", return_value="zh_Hans"):
            result = generate_module.resolve_language(None)
        assert result == "ja"

    def test_flag_overrides_env(self, monkeypatch):
        """Explicit --language argument wins over NOTEBOOKLM_HL env var."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "ja")
        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with patch.object(generate_module, "get_language", return_value="zh_Hans"):
            result = generate_module.resolve_language("ko")
        assert result == "ko"

    def test_env_only_no_config(self, monkeypatch):
        """NOTEBOOKLM_HL set, no config, no flag → env wins over default."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "ja")
        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with patch.object(generate_module, "get_language", return_value=None):
            result = generate_module.resolve_language(None)
        assert result == "ja"

    def test_empty_env_falls_through_to_config(self, monkeypatch):
        """Empty NOTEBOOKLM_HL is treated as unset and config wins."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "")
        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with patch.object(generate_module, "get_language", return_value="zh_Hans"):
            result = generate_module.resolve_language(None)
        assert result == "zh_Hans"

    def test_invalid_env_exits_via_output_error(self, monkeypatch, capsys):
        """An unsupported NOTEBOOKLM_HL value still gets validated. Per ADR-0015
        it routes through ``output_error`` (exit 1, message on stderr) rather
        than ``click.BadParameter``. The message must name ``NOTEBOOKLM_HL`` so
        the user can tell which input source is at fault — mirroring the
        ``in config`` disambiguation that the config-file branch already does."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "xx_INVALID")
        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with (
            patch.object(generate_module, "get_language", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            generate_module.resolve_language(None)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "xx_INVALID" in captured.err
        assert "NOTEBOOKLM_HL" in captured.err

    def test_resolve_language_rejects_invalid_config_value(self, capsys):
        """An unsupported language stored in the config file gets validated.
        Per ADR-0015, routes through ``output_error`` (exit 1, message on
        stderr) rather than ``click.BadParameter``."""
        import importlib

        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with (
            patch.object(generate_module, "get_language", return_value="xx_INVALID"),
            pytest.raises(SystemExit) as exc_info,
        ):
            generate_module.resolve_language(None)
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "xx_INVALID" in captured.err
        assert "notebooklm language list" in captured.err

    def test_resolve_language_accepts_valid_config_value(self):
        """A supported language stored in the config file is returned as-is."""
        import importlib

        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with patch.object(generate_module, "get_language", return_value="ja"):
            result = generate_module.resolve_language(None)
        assert result == "ja"

    def test_resolve_language_treats_whitespace_env_as_unset(self, monkeypatch):
        """Whitespace-only NOTEBOOKLM_HL falls through to config, not rejected."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "   ")
        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with patch.object(generate_module, "get_language", return_value="ja"):
            result = generate_module.resolve_language(None)
        assert result == "ja"

    def test_resolve_language_treats_whitespace_env_as_unset_no_config(self, monkeypatch):
        """Whitespace-only NOTEBOOKLM_HL with no config falls through to default."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "   ")
        generate_module = importlib.import_module("notebooklm.cli.generate_cmd")
        with patch.object(generate_module, "get_language", return_value=None):
            result = generate_module.resolve_language(None)
        assert result == "en"


# =============================================================================
# _OUTPUT_GENERATION_OUTCOME DIRECT TESTS
# =============================================================================
class TestOutputGenerationOutcomeDirect:
    """Direct tests for command-layer generation outcome rendering."""

    def setup_method(self):
        self.generate_module = generate_module

    def test_json_completed_with_url(self):
        outcome = GenerationOutcome(
            status="completed",
            artifact_type="audio",
            task_id="task_123",
            url="https://example.com/audio.mp3",
        )
        with patch.object(self.generate_module, "json_output_response") as mock_json:
            self.generate_module._output_generation_outcome(outcome, json_output=True)
        mock_json.assert_called_once_with(
            {"task_id": "task_123", "status": "completed", "url": "https://example.com/audio.mp3"}
        )

    def test_json_failed(self):
        outcome = GenerationOutcome(
            status="failed", artifact_type="audio", error="Something went wrong"
        )
        with (
            patch.object(self.generate_module, "output_error") as mock_err,
            pytest.raises(SystemExit),
        ):
            mock_err.side_effect = SystemExit(1)
            self.generate_module._output_generation_outcome(outcome, json_output=True)
        mock_err.assert_called_once_with("Something went wrong", "GENERATION_FAILED", True, 1)

    def test_json_failed_no_error_message(self):
        outcome = GenerationOutcome(status="failed", artifact_type="audio")
        with (
            patch.object(self.generate_module, "output_error") as mock_err,
            pytest.raises(SystemExit),
        ):
            mock_err.side_effect = SystemExit(1)
            self.generate_module._output_generation_outcome(outcome, json_output=True)
        mock_err.assert_called_once_with("Audio generation failed", "GENERATION_FAILED", True, 1)

    def test_json_pending_with_task_id(self):
        outcome = GenerationOutcome(status="pending", artifact_type="audio", task_id="task_456")
        with patch.object(self.generate_module, "json_output_response") as mock_json:
            self.generate_module._output_generation_outcome(outcome, json_output=True)
        mock_json.assert_called_once_with({"task_id": "task_456", "status": "pending"})

    def test_text_completed_with_url(self):
        outcome = GenerationOutcome(
            status="completed",
            artifact_type="audio",
            task_id="task_123",
            url="https://example.com/audio.mp3",
        )
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_generation_outcome(outcome, json_output=False)
        mock_console.print.assert_called_once_with(
            "[green]Audio ready:[/green] https://example.com/audio.mp3"
        )

    def test_text_completed_without_url(self):
        outcome = GenerationOutcome(status="completed", artifact_type="audio", task_id="task_123")
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_generation_outcome(outcome, json_output=False)
        mock_console.print.assert_called_once_with("[green]Audio ready[/green]")

    def test_text_failed(self):
        outcome = GenerationOutcome(
            status="failed", artifact_type="audio", error="Transcription error"
        )
        with (
            patch.object(self.generate_module, "output_error") as mock_err,
            pytest.raises(SystemExit),
        ):
            mock_err.side_effect = SystemExit(1)
            self.generate_module._output_generation_outcome(outcome, json_output=False)
        mock_err.assert_called_once_with("Transcription error", "GENERATION_FAILED", False, 1)

    def test_text_failed_no_error_message(self):
        outcome = GenerationOutcome(status="failed", artifact_type="audio")
        with (
            patch.object(self.generate_module, "output_error") as mock_err,
            pytest.raises(SystemExit),
        ):
            mock_err.side_effect = SystemExit(1)
            self.generate_module._output_generation_outcome(outcome, json_output=False)
        mock_err.assert_called_once_with("Audio generation failed", "GENERATION_FAILED", False, 1)

    def test_text_pending_with_task_id(self):
        outcome = GenerationOutcome(status="pending", artifact_type="audio", task_id="task_789")
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_generation_outcome(outcome, json_output=False)
        mock_console.print.assert_called_once_with("[yellow]Started:[/yellow] task_789")

    def test_text_pending_without_task_id_shows_raw_status(self):
        raw_status = object()
        outcome = GenerationOutcome(status="pending", artifact_type="audio", raw_status=raw_status)
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_generation_outcome(outcome, json_output=False)
        mock_console.print.assert_called_once()
        call_args = mock_console.print.call_args[0][0]
        assert "[yellow]Started:[/yellow]" in call_args


# ``TestExtractTaskIdDirect`` moved to ``tests/unit/app/test_app_generate_retry.py``
# (``_extract_task_id`` is defined in ``_app/generate_retry.py``).
# =============================================================================
# _OUTPUT_MIND_MAP_RESULT DIRECT TESTS
# =============================================================================
class TestOutputMindMapResultDirect:
    """Direct tests for _output_mind_map_result() covering uncovered branches."""

    def setup_method(self):
        import importlib

        self.generate_module = importlib.import_module("notebooklm.cli.generate_cmd")

    def test_falsy_result_json_calls_error(self):
        """Falsy result with json_output -> json_error_response."""
        with patch.object(self.generate_module, "json_error_response") as mock_err:
            self.generate_module._output_mind_map_result(None, json_output=True)
        mock_err.assert_called_once_with("GENERATION_FAILED", "Mind map generation failed")

    def test_falsy_result_no_json_prints_message(self):
        """Falsy result without json_output -> console.print yellow."""
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_mind_map_result(None, json_output=False)
        mock_console.print.assert_called_with("[yellow]No result[/yellow]")

    def test_truthy_result_json_calls_output(self):
        """Truthy result with json_output -> converged {mind_map, note_id, kind}."""
        result_data = {"note_id": "n1", "mind_map": {"name": "Root", "children": []}}
        with patch.object(self.generate_module, "json_output_response") as mock_json:
            self.generate_module._output_mind_map_result(result_data, json_output=True)
        mock_json.assert_called_once_with(
            {"mind_map": {"name": "Root", "children": []}, "note_id": "n1", "kind": "note_backed"}
        )

    def test_truthy_result_dict_text_output(self):
        """Truthy result dict with text output prints note_id and children count."""
        result_data = {
            "note_id": "n1",
            "mind_map": {"name": "Root", "children": [{"label": "Child1"}, {"label": "Child2"}]},
        }
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_mind_map_result(result_data, json_output=False)
        printed_args = [call[0][0] for call in mock_console.print.call_args_list]
        assert any("n1" in arg for arg in printed_args)
        assert any("Root" in arg for arg in printed_args)
        assert any("2" in arg for arg in printed_args)

    def test_truthy_result_non_dict_text_output(self):
        """Non-dict truthy result with text output → console.print(result)."""
        result_data = "some-string-result"
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_mind_map_result(result_data, json_output=False)
        # Should print the result directly
        printed_args = [call[0][0] for call in mock_console.print.call_args_list]
        assert any("some-string-result" in str(arg) for arg in printed_args)


# =============================================================================
# GENERATE REVISE-SLIDE CLI TESTS
# =============================================================================
class TestGenerateReviseSlide:
    """Tests for the 'generate revise-slide' CLI command."""

    def test_revise_slide_basic(self, runner, mock_auth):
        """revise-slide command invokes client.artifacts.revise_slide."""
        mock_client = create_mock_client()
        mock_client.artifacts.revise_slide = AsyncMock(
            return_value={"artifact_id": "art_rev_1", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "revise-slide",
                    "Make the title bigger",
                    "--artifact",
                    "art_1",
                    "--slide",
                    "0",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        mock_client.artifacts.revise_slide.assert_called_once()

    def test_revise_slide_passes_correct_args(self, runner, mock_auth):
        """Verify artifact_id, slide_index, and prompt are forwarded."""
        mock_client = create_mock_client()
        mock_client.artifacts.revise_slide = AsyncMock(
            return_value={"artifact_id": "art_rev_2", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "revise-slide",
                    "Remove taxonomy",
                    "--artifact",
                    "art_1",
                    "--slide",
                    "3",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        call_kwargs = mock_client.artifacts.revise_slide.call_args
        assert call_kwargs is not None, "revise_slide was not called"
        assert call_kwargs.kwargs.get("artifact_id") == "art_1"
        assert call_kwargs.kwargs.get("slide_index") == 3
        assert call_kwargs.kwargs.get("prompt") == "Remove taxonomy"

    def test_revise_slide_missing_artifact_fails(self, runner, mock_auth):
        """revise-slide requires --artifact option."""
        mock_client = create_mock_client()
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "revise-slide",
                    "Make bigger",
                    "--slide",
                    "0",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code != 0

    def test_revise_slide_missing_slide_fails(self, runner, mock_auth):
        """revise-slide requires --slide option."""
        mock_client = create_mock_client()
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "revise-slide",
                    "Make bigger",
                    "--artifact",
                    "art_1",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code != 0

    def test_revise_slide_json_output(self, runner, mock_auth):
        """revise-slide with --json flag produces JSON output."""
        mock_client = create_mock_client()
        mock_client.artifacts.revise_slide = AsyncMock(
            return_value={"artifact_id": "art_rev_3", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "revise-slide",
                    "Bold the title",
                    "--artifact",
                    "art_1",
                    "--slide",
                    "1",
                    "-n",
                    "nb_123",
                    "--json",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "task_id" in data or "artifact_id" in data or "status" in data


# =============================================================================
# GENERATE REPORT WITH DESCRIPTION (LINE 1057)
# =============================================================================
class TestGenerateReportWithNonBriefingFormat:
    """Test generate report when description is provided with non-briefing-doc format.
    The else-branch sets custom_prompt = description when report_format !=
    'briefing-doc' and description is provided.
    """

    def test_report_description_with_study_guide_format(self, runner, mock_auth):
        """Description + non-default format -> custom_prompt = description."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_report = AsyncMock(
            return_value={"artifact_id": "report_xyz", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "report",
                    "Focus on beginners",
                    "--format",
                    "study-guide",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        mock_client.artifacts.generate_report.assert_called_once()
        call_kwargs = mock_client.artifacts.generate_report.call_args.kwargs
        # custom_prompt should be the description argument
        assert call_kwargs.get("custom_prompt") == "Focus on beginners"

    def test_report_description_with_blog_post_format(self, runner, mock_auth):
        """Description + blog-post format -> custom_prompt set."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_report = AsyncMock(
            return_value={"artifact_id": "report_abc", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "generate",
                    "report",
                    "Write in casual tone",
                    "--format",
                    "blog-post",
                    "-n",
                    "nb_123",
                ],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 0
        mock_client.artifacts.generate_report.assert_called_once()
        call_kwargs = mock_client.artifacts.generate_report.call_args.kwargs
        assert call_kwargs.get("custom_prompt") == "Write in casual tone"


# =============================================================================
# HANDLE_GENERATION_RESULT PATHS (GenerationStatus, dict seam inputs, and wait paths)
# =============================================================================
class TestHandleGenerationResultPaths:
    """Test handle_generation_result branches: GenerationStatus and dict seam inputs."""

    def test_generation_result_with_generation_status_object(self, runner, mock_auth):
        """result is a GenerationStatus -> task_id = result.task_id."""
        from notebooklm.types import GenerationStatus

        status = GenerationStatus(
            task_id="task_gen_1", status="pending", error=None, error_code=None
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=status)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123"], obj=inject_client(mock_client)
            )
        assert result.exit_code == 0
        assert "task_gen_1" in result.output or "Started" in result.output

    def test_generation_result_with_dict_input(self, runner, mock_auth):
        """A dict generation-start result surfaces its task id.
        The raw positional-list path is gone — the facade ``generate_*``
        methods return typed ``GenerationStatus`` objects (dicts remain a
        tolerated seam shape at this boundary).
        """
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(
            return_value={"task_id": "task_dict_1", "status": "processing"}
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123"], obj=inject_client(mock_client)
            )
        assert result.exit_code == 0
        assert "task_dict_1" in result.output or "Started" in result.output

    def test_generation_result_falsy_shows_failed_message(self, runner, mock_auth):
        """Falsy result → stderr error message + non-zero exit (P1.T6).
        Pre-fix exited 0 in text mode; post-fix routes through
        ``output_error`` → ``SystemExit(1)`` and writes the message to
        stderr. See ``TestArtifactGenerationExitCodes`` for the contract.
        """
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=None)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123"], obj=inject_client(mock_client)
            )
        assert result.exit_code != 0
        assert "generation failed" in result.stderr.lower()

    def test_generation_result_falsy_json_shows_error(self, runner, mock_auth):
        """Falsy result with --json → GENERATION_FAILED envelope + non-zero exit.
        Post-P1.T6 the path routes through ``output_error`` (not the older
        ``json_error_response`` helper) so this test pins the JSON-mode
        contract here; ``TestArtifactGenerationExitCodes`` covers the same
        path with explicit exit-code assertions and the text-mode parity.
        """
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=None)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
            )
        # ``output_error`` raises ``SystemExit(1)``; Click reports exit_code 1.
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "GENERATION_FAILED"

    def test_generation_with_wait_and_generation_status(self, runner, mock_auth):
        """wait=True with GenerationStatus triggers wait_for_completion."""
        from notebooklm.types import GenerationStatus

        initial_status = GenerationStatus(
            task_id="task_wait_1", status="pending", error=None, error_code=None
        )
        completed_status = GenerationStatus(
            task_id="task_wait_1",
            status="completed",
            error=None,
            error_code=None,
            url="https://example.com/result.mp3",
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=initial_status)
        mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123", "--wait"], obj=inject_client(mock_client)
            )
        assert result.exit_code == 0
        mock_client.artifacts.wait_for_completion.assert_called_once()


# =============================================================================
# ADDITIONAL TARGETED COVERAGE TESTS
# =============================================================================
# ``TestGenerateWithRetryConsoleOutput`` (the pure ``on_retry``-sink retry test)
# moved to ``tests/unit/app/test_app_generate_retry.py`` as
# ``test_retry_fires_on_retry_sink`` (retargeted at the injected sink, no
# Click coupling).
class TestHandleGenerationResultListPathAndWait:
    """Test generation-result rendering branches and wait console messages."""

    def test_wait_with_task_id_shows_generating_message(self, runner, mock_auth):
        """wait=True, task_id present, not json -> console.print generating."""
        from notebooklm.types import GenerationStatus

        initial_status = GenerationStatus(
            task_id="task_console_1", status="pending", error=None, error_code=None
        )
        completed_status = GenerationStatus(
            task_id="task_console_1",
            status="completed",
            error=None,
            error_code=None,
            url="https://example.com/audio.mp3",
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=initial_status)
        mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123", "--wait"], obj=inject_client(mock_client)
            )
        assert result.exit_code == 0
        # The console message "Generating audio... Task: task_console_1" should appear
        assert "task_console_1" in result.output or "Generating" in result.output
        mock_client.artifacts.wait_for_completion.assert_called_once()

    def test_dict_result_extracts_task_id_for_wait(self, runner, mock_auth):
        """Dict result without ``artifact_id`` + wait=True → task_id from ``task_id``.
        (Formerly exercised the raw positional-list path; the facade returns
        typed statuses, so the list-sniffing branch was removed.)
        """
        from notebooklm.types import GenerationStatus

        completed_status = GenerationStatus(
            task_id="task_dict_wait",
            status="completed",
            error=None,
            error_code=None,
            url="https://example.com/audio.mp3",
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(
            return_value={"task_id": "task_dict_wait", "status": "processing"}
        )
        mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123", "--wait"], obj=inject_client(mock_client)
            )
        assert result.exit_code == 0
        mock_client.artifacts.wait_for_completion.assert_called_once()

    def test_dict_result_prefers_artifact_id_for_wait(self, runner, mock_auth):
        """Dict generation-start results preserve artifact_id-first wait semantics."""
        from notebooklm.types import GenerationStatus

        completed_status = GenerationStatus(
            task_id="artifact_wait_id",
            status="completed",
            error=None,
            error_code=None,
            url="https://example.com/audio.mp3",
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(
            return_value={
                "artifact_id": "artifact_wait_id",
                "task_id": "task_wait_id",
                "status": "processing",
            }
        )
        mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123", "--wait"], obj=inject_client(mock_client)
            )
        assert result.exit_code == 0, result.output
        mock_client.artifacts.wait_for_completion.assert_awaited_once()
        args = mock_client.artifacts.wait_for_completion.await_args.args
        assert args[:2] == ("nb_123", "artifact_wait_id")


class TestOutputMindMapNonDictMindMap:
    """Test _output_mind_map_result when mind_map value is not a dict."""

    def setup_method(self):
        import importlib

        self.generate_module = importlib.import_module("notebooklm.cli.generate_cmd")

    def test_mind_map_non_dict_value_prints_directly(self):
        """A dict result with non-dict ``mind_map`` still prints header and note id."""
        result_data = {
            "note_id": "n1",
            "mind_map": ["node1", "node2"],  # list, not dict → else branch
        }
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_mind_map_result(result_data, json_output=False)
        printed_calls = [call[0][0] for call in mock_console.print.call_args_list]
        # Should print the header and Note ID, then the raw result
        assert any("n1" in str(arg) for arg in printed_calls)


class TestStatusWithElapsed:
    """Cover the polling-UI spinner helper.
    The pure ``_format_status_message`` tests moved to
    ``tests/unit/app/test_app_generate_retry.py`` (the formatter is defined in
    ``_app/generate_retry.py``); the CLI-coupled ``status_with_elapsed``
    no-op-under-``--json`` assertion (which patches ``polling_ui.console``)
    stays here.
    """

    def test_status_with_elapsed_json_output_is_no_op(self):
        """Under --json the helper must NOT call console.status (stdout stays JSON)."""

        async def _exercise() -> None:
            with patch.object(polling_ui_module.console, "status") as mock_status:
                async with status_with_elapsed("audio", json_output=True):
                    pass
                assert not mock_status.called, "console.status must not be invoked under --json"

        asyncio.run(_exercise())


# =============================================================================
# SIGINT / RESUME-HINT TESTS
# =============================================================================
class TestGenerateWaitSigintResumeHint:
    """Ctrl-C during ``generate <kind> --wait`` surfaces the resume hint.
    The hint follows the canonical phrasing
    ``Cancelled. Resume with: notebooklm artifact poll <task_id>``
    and the process exits 130. This guards against the prior regression
    where Ctrl-C during a 30-min cinematic-video wait dumped a Python
    KeyboardInterrupt traceback with no actionable next step.
    """

    def test_generate_audio_wait_sigint_prints_resume_hint_and_exits_130(self, runner, mock_auth):
        """SIGINT during ``generate audio --wait`` exits 130 with a resume hint
        naming the task_id.
        Simulates the Ctrl-C by patching ``client.artifacts.wait_for_completion``
        to raise ``KeyboardInterrupt`` — the same exception Python delivers
        when the user hits Ctrl-C during the polling loop.
        """
        from notebooklm.types import GenerationStatus

        initial_status = GenerationStatus(
            task_id="task_sigint_1", status="pending", error=None, error_code=None
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=initial_status)
        # The polling call is where Ctrl-C lands (asyncio.sleep inside the
        # library's wait loop is the actual suspension point). Surfacing
        # KeyboardInterrupt from the awaitable is the cleanest way to
        # simulate that without spinning up a real polling loop.
        mock_client.artifacts.wait_for_completion = AsyncMock(side_effect=KeyboardInterrupt)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123", "--wait"], obj=inject_client(mock_client)
            )
        # Exit 130 = 128 + signal 2 (SIGINT). Standard convention.
        assert result.exit_code == 130, (
            f"expected SIGINT exit 130, got {result.exit_code}; output={result.output!r}"
        )
        # Specification: SIGINT under --wait must display exactly this resume
        # hint. Hard-coded here so any drift in the user-visible string is
        # caught at this layer, not by a downstream user.
        combined = result.output + (result.stderr if result.stderr_bytes else "")
        assert "Cancelled. Resume with: notebooklm artifact poll task_sigint_1" in combined, (
            f"expected resume hint with task_id; got: {combined!r}"
        )

    def test_generate_audio_wait_sigint_json_emits_cancelled_envelope(self, runner, mock_auth):
        """SIGINT under ``--json`` emits a structured CANCELLED envelope on stdout.
        Automation parsing stdout-as-JSON gets a parseable cancellation
        instead of a half-printed JSON document or a Python traceback. The
        envelope carries the resume hint so an agent can re-issue the resume
        command without scraping a human-facing string.
        """
        from notebooklm.types import GenerationStatus

        initial_status = GenerationStatus(
            task_id="task_sigint_json", status="pending", error=None, error_code=None
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=initial_status)
        mock_client.artifacts.wait_for_completion = AsyncMock(side_effect=KeyboardInterrupt)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "audio", "-n", "nb_123", "--wait", "--json"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code == 130
        # Last JSON document on stdout is the cancellation envelope (the
        # earlier ``Started`` line is suppressed under --wait + --json since
        # the wait succeeds before any status print).
        # Find a JSON object containing "code": "CANCELLED".
        assert '"code": "CANCELLED"' in result.output, (
            f"expected CANCELLED envelope on stdout under --json; got: {result.output!r}"
        )
        assert "notebooklm artifact poll task_sigint_json" in result.output

    def test_status_with_elapsed_propagates_keyboardinterrupt_when_no_resume_hint(self):
        """Without ``resume_hint``, KeyboardInterrupt propagates to the generic handler.
        Preserves the existing ``error_handler.handle_errors`` ownership of
        non-wait commands — the SIGINT-with-hint path is opt-in via the
        ``resume_hint`` argument so unrelated callers (e.g. mind-map's static
        ``console.status`` block) keep getting the generic ``Cancelled.``
        treatment.
        """

        async def _exercise() -> None:
            with patch.object(polling_ui_module.console, "status") as mock_status:
                mock_status.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mock_status.return_value.__exit__ = MagicMock(return_value=False)
                with pytest.raises(KeyboardInterrupt):
                    async with status_with_elapsed("audio", resume_hint=None):
                        raise KeyboardInterrupt

        asyncio.run(_exercise())


# =============================================================================
# P1.T6 — Exit-code parity across text/JSON modes on artifact generation failure
# =============================================================================
class TestArtifactGenerationExitCodes:
    """Failed artifact generation must exit non-zero in BOTH text and JSON modes.
    Pre-fix behavior: text mode printed a Rich error to stdout and returned
    normally (exit 0); JSON mode emitted a ``json_error_response`` envelope and
    exited 1. The exit-code asymmetry meant shell scripts driving
    ``notebooklm generate audio ...`` without ``--json`` could not detect
    failures via ``$?``.
    These tests pin the unified contract: every failure path inside
    ``handle_generation_result`` (and the command-layer outcome renderer for
    terminal failures reached via ``--wait``) routes through ``output_error``,
    which exits non-zero and writes the human-readable message to stderr in
    text mode or a structured envelope on stdout in JSON mode.
    """

    # --- Initial-call failure (result is None / falsy) ---------------------
    def test_text_mode_none_result_exits_nonzero(self, runner, mock_auth):
        """``generate audio`` without ``--json`` exits != 0 when the API returns None."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=None)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123"], obj=inject_client(mock_client)
            )
        assert result.exit_code != 0
        # Message routed to stderr via safe_echo(err=True) under Click 8.2+ which
        # separates stdout/stderr by default.
        assert "Audio generation failed" in result.stderr

    def test_json_mode_none_result_exits_nonzero(self, runner, mock_auth):
        """``generate audio --json`` exits != 0 when the API returns None."""
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=None)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
            )
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "GENERATION_FAILED"
        assert "Audio generation failed" in data["message"]

    # --- Rate-limit failure --------------------------------------------------
    def test_text_mode_rate_limited_exits_nonzero(self, runner, mock_auth):
        """Rate-limited result (no retries left) exits != 0 in text mode."""
        from notebooklm.types import GenerationStatus

        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=rate_limited)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123"], obj=inject_client(mock_client)
            )
        assert result.exit_code != 0
        # The "rate limited" message and the daily-quota hint both land on
        # stderr; the second goes through ``output_error``'s ``hint`` arg.
        assert "rate limited by Google" in result.stderr
        assert "--retry" in result.stderr

    def test_json_mode_rate_limited_exits_nonzero(self, runner, mock_auth):
        """Rate-limited result exits != 0 with a RATE_LIMITED JSON envelope."""
        from notebooklm.types import GenerationStatus

        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=rate_limited)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
            )
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "RATE_LIMITED"

    # --- Wait-then-failed terminal status -----------------------------------
    def test_text_mode_wait_then_failed_exits_nonzero(self, runner, mock_auth):
        """``--wait`` that observes a terminal is_failed status exits != 0 in text mode."""
        from notebooklm.types import GenerationStatus

        initial = GenerationStatus(
            task_id="task_fail_1", status="pending", error=None, error_code=None
        )
        terminal = GenerationStatus(
            task_id="task_fail_1",
            status="failed",
            error="Transcription error",
            error_code="INTERNAL_ERROR",
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=initial)
        mock_client.artifacts.wait_for_completion = AsyncMock(return_value=terminal)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["generate", "audio", "-n", "nb_123", "--wait"], obj=inject_client(mock_client)
            )
        assert result.exit_code != 0
        assert "Transcription error" in result.stderr

    def test_json_mode_wait_then_failed_exits_nonzero(self, runner, mock_auth):
        """``--wait --json`` that observes a terminal is_failed status exits != 0."""
        from notebooklm.types import GenerationStatus

        initial = GenerationStatus(
            task_id="task_fail_2", status="pending", error=None, error_code=None
        )
        terminal = GenerationStatus(
            task_id="task_fail_2",
            status="failed",
            error="Transcription error",
            error_code="INTERNAL_ERROR",
        )
        mock_client = create_mock_client()
        mock_client.artifacts.generate_audio = AsyncMock(return_value=initial)
        mock_client.artifacts.wait_for_completion = AsyncMock(return_value=terminal)
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["generate", "audio", "-n", "nb_123", "--wait", "--json"],
                obj=inject_client(mock_client),
            )
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "GENERATION_FAILED"
        assert "Transcription error" in data["message"]
