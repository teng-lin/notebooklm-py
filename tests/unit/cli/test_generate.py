"""Tests for generate CLI commands."""

import asyncio
import importlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.cli.generate import _format_status_message, _status_with_elapsed
from notebooklm.notebooklm_cli import cli
from notebooklm.rpc.types import ReportFormat

from .conftest import create_mock_client, patch_client_for_module

# ``notebooklm.cli.generate`` (the module) is shadowed by ``cli.__init__``'s
# re-export of the ``generate`` Click Group (same name). Use ``importlib`` so
# tests target the module's attribute set (``console``, helpers) rather than
# the Click Group sitting at the same dotted path.
generate_module = importlib.import_module("notebooklm.cli.generate")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_auth():
    with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock:
        mock.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield mock


# =============================================================================
# GENERATE AUDIO TESTS
# =============================================================================


class TestGenerateAudio:
    def test_generate_audio(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(
                return_value={"artifact_id": "audio_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "audio_123" in result.output or "Started" in result.output

    def test_generate_audio_with_format(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(
                return_value={"artifact_id": "audio_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "audio", "--format", "debate", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            mock_client.artifacts.generate_audio.assert_called()

    def test_generate_audio_with_length(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(
                return_value={"artifact_id": "audio_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "audio", "--length", "long", "-n", "nb_123"]
                )

            assert result.exit_code == 0

    def test_generate_audio_with_wait(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(
                return_value={"artifact_id": "audio_123", "status": "processing"}
            )
            completed_status = MagicMock()
            completed_status.is_complete = True
            completed_status.is_failed = False
            completed_status.url = "https://example.com/audio.mp3"
            completed_status.artifact_id = "audio_123"
            mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "--wait", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Audio ready" in result.output or "example.com" in result.output

    def test_generate_audio_with_wait_timeout_interval_forwarded(self, runner, mock_auth):
        """`generate audio --wait --timeout 60 --interval 5` plumbs both into
        artifacts.wait_for_completion (P5.T1 / I6).

        The new `--timeout`/`--interval` flags must reach the polling call so
        that scripts can bound the wait and the cadence — not just toggle the
        wait on/off as the legacy `--wait` flag did. The CLI surface is
        uniform with `artifact wait` / `source wait` after this change.
        """
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(
                return_value={"artifact_id": "audio_xyz", "status": "processing"}
            )
            completed_status = MagicMock()
            completed_status.is_complete = True
            completed_status.is_failed = False
            completed_status.url = "https://example.com/audio.mp3"
            completed_status.task_id = "audio_xyz"
            mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

            assert result.exit_code == 0, result.output
            mock_client.artifacts.wait_for_completion.assert_awaited_once()
            kwargs = mock_client.artifacts.wait_for_completion.await_args.kwargs
            assert kwargs.get("timeout") == 60.0
            # interval may be plumbed as either initial_interval (preferred new
            # parameter name) or poll_interval (legacy compatibility shim);
            # accept whichever the implementation chose so this test does not
            # over-constrain the wiring choice.
            interval_value = kwargs.get("initial_interval", kwargs.get("poll_interval"))
            assert interval_value == 5.0, (
                f"expected --interval=5 to plumb into wait_for_completion, got kwargs={kwargs}"
            )

    def test_generate_audio_timeout_interval_without_wait_is_no_op(self, runner, mock_auth):
        """`generate audio --timeout 60 --interval 5` (without --wait) is
        accepted but does not call wait_for_completion.

        The polling flags only take effect when paired with --wait; supplying
        them without --wait must NOT trigger a wait (preserves the default
        no-wait behavior promised by the original `--wait/--no-wait` toggle).
        """
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(
                return_value={"artifact_id": "audio_xyz", "status": "processing"}
            )
            mock_client.artifacts.wait_for_completion = AsyncMock()
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

            assert result.exit_code == 0, result.output
            mock_client.artifacts.wait_for_completion.assert_not_awaited()

    def test_generate_audio_with_wait_invokes_console_status(self, runner, mock_auth):
        """`generate audio --wait` wraps the polling call in `console.status` (P5.T2 / I7).

        The spinner gives interactive users feedback during the long wait, with
        a transient line naming the artifact kind (and a typical-duration hint).
        Asserts the wrap by patching `notebooklm.cli.generate.console.status`
        and confirming it is invoked exactly once with a message that mentions
        the artifact kind. Does not assert the elapsed-timer ticker — that's a
        rendering detail that relies on a TTY which `CliRunner` doesn't have.
        """
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(
                return_value={"artifact_id": "audio_xyz", "status": "processing"}
            )
            completed_status = MagicMock()
            completed_status.is_complete = True
            completed_status.is_failed = False
            completed_status.url = "https://example.com/audio.mp3"
            completed_status.task_id = "audio_xyz"
            mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
            mock_client_cls.return_value = mock_client

            with (
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
                patch.object(generate_module.console, "status") as mock_status,
            ):
                mock_fetch.return_value = ("csrf", "session")
                # ``console.status`` returns a context manager; emulate one so
                # the wrapped polling call still runs.
                mock_status.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mock_status.return_value.__exit__ = MagicMock(return_value=False)
                result = runner.invoke(cli, ["generate", "audio", "--wait", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            assert mock_status.called, "expected console.status to wrap the --wait polling call"
            status_msg = mock_status.call_args.args[0]
            assert "audio" in status_msg.lower(), (
                f"expected status message to mention artifact kind 'audio', got: {status_msg!r}"
            )

    def test_generate_audio_failure(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Audio generation failed" in result.output


# =============================================================================
# GENERATE VIDEO TESTS
# =============================================================================


class TestGenerateVideo:
    def test_generate_video(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_video = AsyncMock(
                return_value={"artifact_id": "video_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "video", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_video_with_style(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_video = AsyncMock(
                return_value={"artifact_id": "video_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "video", "--style", "kawaii", "-n", "nb_123"]
                )

            assert result.exit_code == 0

    def test_generate_video_with_custom_style_prompt(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_video = AsyncMock(
                return_value={"artifact_id": "video_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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

        # ``click.UsageError`` exits 2 — Click's standard convention.
        assert result.exit_code == 2
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

        assert result.exit_code == 2
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

        assert result.exit_code == 2
        assert "--style-prompt requires --style custom" in result.output


# =============================================================================
# GENERATE CINEMATIC VIDEO TESTS
# =============================================================================


class TestGenerateCinematicVideo:
    def test_generate_cinematic_video(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_cinematic_video = AsyncMock(
                return_value={"artifact_id": "cin_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "cinematic-video", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_cinematic_video_with_description(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_cinematic_video = AsyncMock(
                return_value={"artifact_id": "cin_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

            assert result.exit_code == 0

    def test_generate_cinematic_video_ignores_style(self, runner, mock_auth):
        """Cinematic video accepts --style (inherited from video) but ignores it."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_cinematic_video = AsyncMock(
                return_value={"artifact_id": "cin_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["generate", "cinematic-video", "--style", "anime", "-n", "nb_123"],
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

        assert result.exit_code == 2
        assert "--style-prompt cannot be used with cinematic video" in result.output

    def test_generate_cinematic_video_rejects_non_cinematic_format(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """`cinematic-video --format explainer` (or any non-cinematic value) must
        raise UsageError, not silently override the format."""
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

            assert result.exit_code == 2, (
                f"--format {bad_format} should exit 2, got {result.exit_code}: {result.output}"
            )
            assert "--format" in result.output
            assert "cinematic" in result.output.lower()

    def test_generate_cinematic_video_explicit_cinematic_format_ok(self, runner, mock_auth):
        """`cinematic-video --format cinematic` is the canonical happy path."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_cinematic_video = AsyncMock(
                return_value={"artifact_id": "cin_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

            assert result.exit_code == 0, result.output
            mock_client.artifacts.generate_cinematic_video.assert_called_once()

    def test_generate_cinematic_video_default_format_ok(self, runner, mock_auth):
        """`cinematic-video` with no --format defaults to cinematic and works."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_cinematic_video = AsyncMock(
                return_value={"artifact_id": "cin_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "cinematic-video", "-n", "nb_123"])

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


# =============================================================================
# GENERATE QUIZ TESTS
# =============================================================================


class TestGenerateQuiz:
    def test_generate_quiz(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_quiz = AsyncMock(
                return_value={"artifact_id": "quiz_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "quiz", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_quiz_with_options(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_quiz = AsyncMock(
                return_value={"artifact_id": "quiz_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

            assert result.exit_code == 0


# =============================================================================
# GENERATE FLASHCARDS TESTS
# =============================================================================


class TestGenerateFlashcards:
    def test_generate_flashcards(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_flashcards = AsyncMock(
                return_value={"artifact_id": "flash_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "flashcards", "-n", "nb_123"])

            assert result.exit_code == 0


# =============================================================================
# GENERATE SLIDE DECK TESTS
# =============================================================================


class TestGenerateSlideDeck:
    def test_generate_slide_deck(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_slide_deck = AsyncMock(
                return_value={"artifact_id": "slides_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "slide-deck", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_slide_deck_with_options(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_slide_deck = AsyncMock(
                return_value={"artifact_id": "slides_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

            assert result.exit_code == 0


# =============================================================================
# GENERATE INFOGRAPHIC TESTS
# =============================================================================


class TestGenerateInfographic:
    def test_generate_infographic(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_infographic = AsyncMock(
                return_value={"artifact_id": "info_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "infographic", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_infographic_with_options(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_infographic = AsyncMock(
                return_value={"artifact_id": "info_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

            assert result.exit_code == 0
            mock_client.artifacts.generate_infographic.assert_awaited_once()
            kwargs = mock_client.artifacts.generate_infographic.await_args.kwargs
            assert kwargs["orientation"].name == "PORTRAIT"
            assert kwargs["detail_level"].name == "DETAILED"
            assert kwargs["style"].name == "ANIME"


# =============================================================================
# GENERATE DATA TABLE TESTS
# =============================================================================


class TestGenerateDataTable:
    def test_generate_data_table(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_data_table = AsyncMock(
                return_value={"artifact_id": "table_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "data-table", "Compare key concepts", "-n", "nb_123"]
                )

            assert result.exit_code == 0


# =============================================================================
# GENERATE MIND MAP TESTS
# =============================================================================


class TestGenerateMindMap:
    def test_generate_mind_map(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_mind_map = AsyncMock(
                return_value={"mind_map": {"name": "Root", "children": []}, "note_id": "n1"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "mind-map", "-n", "nb_123"])

            assert result.exit_code == 0


# =============================================================================
# GENERATE REPORT TESTS
# =============================================================================


class TestGenerateReport:
    def test_generate_report(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_report = AsyncMock(
                return_value={"artifact_id": "report_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "report", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_report_study_guide(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_report = AsyncMock(
                return_value={"artifact_id": "report_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "report", "--format", "study-guide", "-n", "nb_123"]
                )

            assert result.exit_code == 0

    def test_generate_report_custom(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_report = AsyncMock(
                return_value={"artifact_id": "report_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "report", "Create a white paper", "-n", "nb_123"]
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
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_report = AsyncMock(
                return_value={"artifact_id": "report_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

            assert result.exit_code == 0
            call_kwargs = mock_client.artifacts.generate_report.call_args.kwargs
            assert call_kwargs["extra_instructions"] == extra_text
            assert call_kwargs["report_format"] == expected_format
            assert call_kwargs["custom_prompt"] is None

    def test_generate_report_append_with_custom_warns(self, runner, mock_auth):
        """--append with --format custom prints a warning and clears extra_instructions."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_report = AsyncMock(
                return_value={"artifact_id": "report_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

            assert result.exit_code == 0
            assert "Warning" in result.output
            assert "--format custom" in result.output
            call_kwargs = mock_client.artifacts.generate_report.call_args.kwargs
            assert call_kwargs["extra_instructions"] is None

    def test_generate_report_append_with_description_warns(self, runner, mock_auth):
        """--append with a description arg (auto-promoted to custom) warns and clears extra_instructions."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_report = AsyncMock(
                return_value={"artifact_id": "report_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["generate", "report", "My custom prompt", "--append", "extra", "-n", "nb_123"],
                )

            assert result.exit_code == 0
            assert "Warning" in result.output
            call_kwargs = mock_client.artifacts.generate_report.call_args.kwargs
            assert call_kwargs["extra_instructions"] is None
            assert call_kwargs["report_format"] == ReportFormat.CUSTOM


# =============================================================================
# JSON OUTPUT TESTS (PARAMETRIZED)
# =============================================================================


class TestGenerateJsonOutput:
    """Parametrized tests for --json output across all generate commands."""

    @pytest.mark.parametrize(
        "cmd,method,task_id",
        [
            ("audio", "generate_audio", "audio_123"),
            ("video", "generate_video", "video_123"),
            ("cinematic-video", "generate_cinematic_video", "cin_123"),
            ("quiz", "generate_quiz", "quiz_123"),
            ("flashcards", "generate_flashcards", "flash_123"),
            ("slide-deck", "generate_slide_deck", "slides_123"),
            ("infographic", "generate_infographic", "info_123"),
            ("report", "generate_report", "report_123"),
        ],
    )
    def test_generate_json_output(self, runner, mock_auth, cmd, method, task_id):
        """Test --json flag produces valid JSON output for standard generate commands."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            setattr(
                mock_client.artifacts,
                method,
                AsyncMock(return_value={"task_id": task_id, "status": "processing"}),
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", cmd, "--json", "-n", "nb_123"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["task_id"] == task_id

    def test_generate_data_table_json_output(self, runner, mock_auth):
        """Test --json for data-table (requires description argument)."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_data_table = AsyncMock(
                return_value={"task_id": "table_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "data-table", "Compare concepts", "--json", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["task_id"] == "table_123"

    def test_generate_mind_map_json_output(self, runner, mock_auth):
        """Test --json for mind-map (different return structure)."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_mind_map = AsyncMock(
                return_value={"mind_map": {"name": "Root", "children": []}, "note_id": "n1"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "mind-map", "--json", "-n", "nb_123"])

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
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["generate", "audio", "-n", "nb_123", "--language", "invalid_code"],
                )

        assert result.exit_code != 0
        assert "Unknown language code: invalid_code" in result.output
        assert "notebooklm language list" in result.output

    def test_valid_language_code_accepted(self, runner, mock_auth):
        """Test that valid language codes are accepted."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(
                return_value={"artifact_id": "audio_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "audio", "-n", "nb_123", "--language", "ja"]
                )

            assert result.exit_code == 0


# =============================================================================
# RETRY FUNCTIONALITY TESTS
# =============================================================================


class TestCalculateBackoffDelay:
    """Tests for the calculate_backoff_delay helper function."""

    def test_initial_delay(self):
        """Test that first attempt uses initial delay."""
        from notebooklm.cli.generate import calculate_backoff_delay

        delay = calculate_backoff_delay(0, initial_delay=60.0)
        assert delay == 60.0

    def test_exponential_backoff(self):
        """Test that delay increases exponentially."""
        from notebooklm.cli.generate import calculate_backoff_delay

        assert calculate_backoff_delay(0, initial_delay=60.0) == 60.0
        assert calculate_backoff_delay(1, initial_delay=60.0) == 120.0
        assert calculate_backoff_delay(2, initial_delay=60.0) == 240.0

    def test_max_delay_cap(self):
        """Test that delay is capped at max_delay."""
        from notebooklm.cli.generate import calculate_backoff_delay

        delay = calculate_backoff_delay(10, initial_delay=60.0, max_delay=300.0)
        assert delay == 300.0

    def test_custom_multiplier(self):
        """Test custom backoff multiplier."""
        from notebooklm.cli.generate import calculate_backoff_delay

        delay = calculate_backoff_delay(1, initial_delay=10.0, multiplier=3.0)
        assert delay == 30.0


class TestGenerateWithRetry:
    """Tests for the generate_with_retry helper function."""

    @pytest.mark.asyncio
    async def test_no_retry_on_success(self):
        """Test that successful generation doesn't trigger retry."""
        from notebooklm.cli.generate import generate_with_retry
        from notebooklm.types import GenerationStatus

        success_result = GenerationStatus(
            task_id="task_123", status="pending", error=None, error_code=None
        )
        generate_fn = AsyncMock(return_value=success_result)

        result = await generate_with_retry(generate_fn, max_retries=3, artifact_type="audio")

        assert result == success_result
        assert generate_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_on_rate_limit(self):
        """Test that rate limit triggers retry."""
        from notebooklm.cli.generate import generate_with_retry
        from notebooklm.types import GenerationStatus

        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )
        success_result = GenerationStatus(
            task_id="task_123", status="pending", error=None, error_code=None
        )
        generate_fn = AsyncMock(side_effect=[rate_limited, success_result])

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await generate_with_retry(
                generate_fn, max_retries=3, artifact_type="audio", json_output=True
            )

        assert result == success_result
        assert generate_fn.call_count == 2
        mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_retry_exhausted(self):
        """Test that all retries being exhausted returns last result."""
        from notebooklm.cli.generate import generate_with_retry
        from notebooklm.types import GenerationStatus

        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )
        generate_fn = AsyncMock(return_value=rate_limited)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await generate_with_retry(
                generate_fn, max_retries=2, artifact_type="audio", json_output=True
            )

        assert result == rate_limited
        assert generate_fn.call_count == 3  # initial + 2 retries

    @pytest.mark.asyncio
    async def test_no_retry_when_max_retries_zero(self):
        """Test that max_retries=0 means no retry attempts."""
        from notebooklm.cli.generate import generate_with_retry
        from notebooklm.types import GenerationStatus

        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )
        generate_fn = AsyncMock(return_value=rate_limited)

        result = await generate_with_retry(
            generate_fn, max_retries=0, artifact_type="audio", json_output=True
        )

        assert result == rate_limited
        assert generate_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_delays_increase_exponentially(self):
        """Verify delays follow exponential backoff pattern (60s, 120s, 240s)."""
        from notebooklm.cli.generate import generate_with_retry
        from notebooklm.types import GenerationStatus

        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )
        generate_fn = AsyncMock(return_value=rate_limited)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await generate_with_retry(
                generate_fn, max_retries=3, artifact_type="audio", json_output=True
            )

        # Verify delays: 60s, 120s, 240s (3 retries = 3 sleeps)
        delays = [call[0][0] for call in mock_sleep.call_args_list]
        assert delays == [60.0, 120.0, 240.0]

    @pytest.mark.asyncio
    async def test_retry_delay_caps_at_max(self):
        """Verify delay caps at 300s even with many retries."""
        from notebooklm.cli.generate import RETRY_MAX_DELAY, generate_with_retry
        from notebooklm.types import GenerationStatus

        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )
        generate_fn = AsyncMock(return_value=rate_limited)

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await generate_with_retry(
                generate_fn, max_retries=10, artifact_type="audio", json_output=True
            )

        # Verify no delay exceeds RETRY_MAX_DELAY (300s)
        delays = [call[0][0] for call in mock_sleep.call_args_list]
        assert len(delays) == 10  # 10 retries = 10 sleeps
        for delay in delays:
            assert delay <= RETRY_MAX_DELAY
        # Later delays should be capped at 300
        assert delays[-1] == RETRY_MAX_DELAY


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

        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=rate_limited)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123"])

            assert "rate limited by Google" in result.output
            assert "--retry" in result.output

    def test_rate_limit_json_output(self, runner, mock_auth):
        """Test that rate limit error produces correct JSON output."""
        from notebooklm.types import GenerationStatus

        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )

        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=rate_limited)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123", "--json"])

            data = json.loads(result.output)
            assert data["error"] is True
            assert data["code"] == "RATE_LIMITED"


# =============================================================================
# RESOLVE_LANGUAGE DIRECT TESTS
# =============================================================================


class TestResolveLanguageDirect:
    """Direct tests for resolve_language() covering uncovered branches."""

    def test_invalid_language_raises_bad_parameter(self):
        """Line 111: language not in SUPPORTED_LANGUAGES raises click.BadParameter."""
        import importlib

        import click

        generate_module = importlib.import_module("notebooklm.cli.generate")
        with pytest.raises(click.BadParameter) as exc_info:
            generate_module.resolve_language("xx_INVALID")
        assert "Unknown language code: xx_INVALID" in str(exc_info.value)
        assert "notebooklm language list" in str(exc_info.value)

    def test_none_language_with_config_returns_config(self):
        """Line 118: language is None, config_lang is not None → returns config_lang."""
        import importlib

        generate_module = importlib.import_module("notebooklm.cli.generate")
        with patch.object(generate_module, "get_language", return_value="fr"):
            result = generate_module.resolve_language(None)
        assert result == "fr"

    def test_none_language_no_config_returns_default(self):
        """Line 139: language is None and config_lang is None → returns DEFAULT_LANGUAGE."""
        import importlib

        generate_module = importlib.import_module("notebooklm.cli.generate")
        with patch.object(generate_module, "get_language", return_value=None):
            result = generate_module.resolve_language(None)
        assert result == "en"

    def test_env_overrides_config(self, monkeypatch):
        """NOTEBOOKLM_HL set, config also set, no flag → env wins over config."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "ja")
        generate_module = importlib.import_module("notebooklm.cli.generate")
        with patch.object(generate_module, "get_language", return_value="zh_Hans"):
            result = generate_module.resolve_language(None)
        assert result == "ja"

    def test_flag_overrides_env(self, monkeypatch):
        """Explicit --language argument wins over NOTEBOOKLM_HL env var."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "ja")
        generate_module = importlib.import_module("notebooklm.cli.generate")
        with patch.object(generate_module, "get_language", return_value="zh_Hans"):
            result = generate_module.resolve_language("ko")
        assert result == "ko"

    def test_env_only_no_config(self, monkeypatch):
        """NOTEBOOKLM_HL set, no config, no flag → env wins over default."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "ja")
        generate_module = importlib.import_module("notebooklm.cli.generate")
        with patch.object(generate_module, "get_language", return_value=None):
            result = generate_module.resolve_language(None)
        assert result == "ja"

    def test_empty_env_falls_through_to_config(self, monkeypatch):
        """Empty NOTEBOOKLM_HL is treated as unset and config wins."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "")
        generate_module = importlib.import_module("notebooklm.cli.generate")
        with patch.object(generate_module, "get_language", return_value="zh_Hans"):
            result = generate_module.resolve_language(None)
        assert result == "zh_Hans"

    def test_invalid_env_raises_bad_parameter(self, monkeypatch):
        """An unsupported NOTEBOOKLM_HL value still gets validated."""
        import importlib

        import click

        monkeypatch.setenv("NOTEBOOKLM_HL", "xx_INVALID")
        generate_module = importlib.import_module("notebooklm.cli.generate")
        with (
            patch.object(generate_module, "get_language", return_value=None),
            pytest.raises(click.BadParameter) as exc_info,
        ):
            generate_module.resolve_language(None)
        assert "xx_INVALID" in str(exc_info.value)

    def test_resolve_language_rejects_invalid_config_value(self):
        """An unsupported language stored in the config file gets validated."""
        import importlib

        import click

        generate_module = importlib.import_module("notebooklm.cli.generate")
        with (
            patch.object(generate_module, "get_language", return_value="xx_INVALID"),
            pytest.raises(click.BadParameter) as exc_info,
        ):
            generate_module.resolve_language(None)
        assert "xx_INVALID" in str(exc_info.value)
        assert "notebooklm language list" in str(exc_info.value)

    def test_resolve_language_accepts_valid_config_value(self):
        """A supported language stored in the config file is returned as-is."""
        import importlib

        generate_module = importlib.import_module("notebooklm.cli.generate")
        with patch.object(generate_module, "get_language", return_value="ja"):
            result = generate_module.resolve_language(None)
        assert result == "ja"

    def test_resolve_language_treats_whitespace_env_as_unset(self, monkeypatch):
        """Whitespace-only NOTEBOOKLM_HL falls through to config, not rejected."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "   ")
        generate_module = importlib.import_module("notebooklm.cli.generate")
        with patch.object(generate_module, "get_language", return_value="ja"):
            result = generate_module.resolve_language(None)
        assert result == "ja"

    def test_resolve_language_treats_whitespace_env_as_unset_no_config(self, monkeypatch):
        """Whitespace-only NOTEBOOKLM_HL with no config falls through to default."""
        import importlib

        monkeypatch.setenv("NOTEBOOKLM_HL", "   ")
        generate_module = importlib.import_module("notebooklm.cli.generate")
        with patch.object(generate_module, "get_language", return_value=None):
            result = generate_module.resolve_language(None)
        assert result == "en"


# =============================================================================
# _OUTPUT_GENERATION_STATUS DIRECT TESTS
# =============================================================================


class TestOutputGenerationStatusDirect:
    """Direct tests for _output_generation_status() covering uncovered branches."""

    def setup_method(self):
        import importlib

        self.generate_module = importlib.import_module("notebooklm.cli.generate")

    def _make_status(
        self, *, is_complete=False, is_failed=False, task_id=None, url=None, error=None
    ):
        status = MagicMock()
        status.is_complete = is_complete
        status.is_failed = is_failed
        status.task_id = task_id
        status.url = url
        status.error = error
        return status

    def test_json_completed_with_url(self):
        """Lines 200-201, 243: JSON output for completed status with URL."""
        status = self._make_status(
            is_complete=True, task_id="task_123", url="https://example.com/audio.mp3"
        )
        with patch.object(self.generate_module, "json_output_response") as mock_json:
            self.generate_module._output_generation_status(status, "audio", json_output=True)
        mock_json.assert_called_once_with(
            {"task_id": "task_123", "status": "completed", "url": "https://example.com/audio.mp3"}
        )

    def test_json_failed(self):
        """Line 251: JSON output for failed status."""
        status = self._make_status(is_failed=True, error="Something went wrong")
        with patch.object(self.generate_module, "json_error_response") as mock_err:
            self.generate_module._output_generation_status(status, "audio", json_output=True)
        mock_err.assert_called_once_with("GENERATION_FAILED", "Something went wrong")

    def test_json_failed_no_error_message(self):
        """Line 251: JSON failed output falls back to default message when error is None."""
        status = self._make_status(is_failed=True, error=None)
        with patch.object(self.generate_module, "json_error_response") as mock_err:
            self.generate_module._output_generation_status(status, "audio", json_output=True)
        mock_err.assert_called_once_with("GENERATION_FAILED", "Audio generation failed")

    def test_json_pending_with_task_id(self):
        """Lines 205-207, 257: JSON output for pending status extracts task_id from list."""
        # Use a list result (lines 205-207: list path in handle_generation_result)
        # and pending path in _output_generation_status (lines 255-257)
        status = MagicMock()
        status.is_complete = False
        status.is_failed = False
        status.task_id = "task_456"
        with patch.object(self.generate_module, "json_output_response") as mock_json:
            self.generate_module._output_generation_status(status, "audio", json_output=True)
        mock_json.assert_called_once_with({"task_id": "task_456", "status": "pending"})

    def test_text_completed_with_url(self):
        """Line 262: Text output for completed status with URL."""
        status = self._make_status(
            is_complete=True, task_id="task_123", url="https://example.com/audio.mp3"
        )
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_generation_status(status, "audio", json_output=False)
        mock_console.print.assert_called_once_with(
            "[green]Audio ready:[/green] https://example.com/audio.mp3"
        )

    def test_text_completed_without_url(self):
        """Line 264: Text output for completed status without URL."""
        status = self._make_status(is_complete=True, task_id="task_123", url=None)
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_generation_status(status, "audio", json_output=False)
        mock_console.print.assert_called_once_with("[green]Audio ready[/green]")

    def test_text_failed(self):
        """Line 266: Text output for failed status."""
        status = self._make_status(is_failed=True, error="Transcription error")
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_generation_status(status, "audio", json_output=False)
        mock_console.print.assert_called_once_with("[red]Failed:[/red] Transcription error")

    def test_text_pending_with_task_id(self):
        """Line 268: Text output for pending status shows task_id."""
        status = self._make_status(task_id="task_789")
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_generation_status(status, "audio", json_output=False)
        mock_console.print.assert_called_once_with("[yellow]Started:[/yellow] task_789")

    def test_text_pending_without_task_id_shows_status(self):
        """Line 268: Text output for pending status shows status object when no task_id."""
        status = MagicMock()
        status.is_complete = False
        status.is_failed = False
        # Make _extract_task_id return None by having no task_id attr and not a dict/list
        del status.task_id
        with (
            patch.object(self.generate_module, "_extract_task_id", return_value=None),
            patch.object(self.generate_module, "console") as mock_console,
        ):
            self.generate_module._output_generation_status(status, "audio", json_output=False)
        mock_console.print.assert_called_once()
        call_args = mock_console.print.call_args[0][0]
        assert "[yellow]Started:[/yellow]" in call_args


class TestExtractTaskIdDirect:
    """Direct tests for _extract_task_id() covering list path."""

    def setup_method(self):
        import importlib

        self.generate_module = importlib.import_module("notebooklm.cli.generate")

    def test_extract_from_list_first_string(self):
        """Lines 231-232: list where first element is a string."""
        result = self.generate_module._extract_task_id(["task_abc", "other"])
        assert result == "task_abc"

    def test_extract_from_list_first_not_string(self):
        """Line 233: list where first element is not a string → returns None."""
        result = self.generate_module._extract_task_id([123, "other"])
        assert result is None

    def test_extract_from_empty_list(self):
        """Line 233: empty list → returns None."""
        result = self.generate_module._extract_task_id([])
        assert result is None

    def test_extract_from_dict_task_id(self):
        """Line 228: dict with task_id key."""
        result = self.generate_module._extract_task_id({"task_id": "t1", "status": "pending"})
        assert result == "t1"

    def test_extract_from_dict_artifact_id(self):
        """Line 228: dict with artifact_id key (no task_id)."""
        result = self.generate_module._extract_task_id({"artifact_id": "a1"})
        assert result == "a1"

    def test_extract_from_object_with_task_id(self):
        """Line 228: object with task_id attribute."""
        status = MagicMock()
        status.task_id = "task_obj"
        result = self.generate_module._extract_task_id(status)
        assert result == "task_obj"


# =============================================================================
# _OUTPUT_MIND_MAP_RESULT DIRECT TESTS
# =============================================================================


class TestOutputMindMapResultDirect:
    """Direct tests for _output_mind_map_result() covering uncovered branches."""

    def setup_method(self):
        import importlib

        self.generate_module = importlib.import_module("notebooklm.cli.generate")

    def test_falsy_result_json_calls_error(self):
        """Lines 624-626: falsy result with json_output → json_error_response."""
        with patch.object(self.generate_module, "json_error_response") as mock_err:
            self.generate_module._output_mind_map_result(None, json_output=True)
        mock_err.assert_called_once_with("GENERATION_FAILED", "Mind map generation failed")

    def test_falsy_result_no_json_prints_message(self):
        """Lines 627-628: falsy result without json_output → console.print yellow."""
        with patch.object(self.generate_module, "console") as mock_console:
            self.generate_module._output_mind_map_result(None, json_output=False)
        mock_console.print.assert_called_with("[yellow]No result[/yellow]")

    def test_truthy_result_json_calls_output(self):
        """Line 631: truthy result with json_output → json_output_response."""
        result_data = {"note_id": "n1", "mind_map": {"name": "Root", "children": []}}
        with patch.object(self.generate_module, "json_output_response") as mock_json:
            self.generate_module._output_mind_map_result(result_data, json_output=True)
        mock_json.assert_called_once_with(result_data)

    def test_truthy_result_dict_text_output(self):
        """Lines 633-635: truthy result dict with text output prints note_id and children count."""
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
    """Tests for the 'generate revise-slide' CLI command (lines 971-989)."""

    def test_revise_slide_basic(self, runner, mock_auth):
        """Lines 971-975: revise-slide command invokes client.artifacts.revise_slide."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.revise_slide = AsyncMock(
                return_value={"artifact_id": "art_rev_1", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

        assert result.exit_code == 0
        mock_client.artifacts.revise_slide.assert_called_once()

    def test_revise_slide_passes_correct_args(self, runner, mock_auth):
        """Lines 985-989: verify artifact_id, slide_index, and prompt are forwarded."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.revise_slide = AsyncMock(
                return_value={"artifact_id": "art_rev_2", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

        assert result.exit_code == 0
        call_kwargs = mock_client.artifacts.revise_slide.call_args
        assert call_kwargs is not None, "revise_slide was not called"
        assert call_kwargs.kwargs.get("artifact_id") == "art_1"
        assert call_kwargs.kwargs.get("slide_index") == 3
        assert call_kwargs.kwargs.get("prompt") == "Remove taxonomy"

    def test_revise_slide_missing_artifact_fails(self, runner, mock_auth):
        """revise-slide requires --artifact option."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

        assert result.exit_code != 0

    def test_revise_slide_missing_slide_fails(self, runner, mock_auth):
        """revise-slide requires --slide option."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

        assert result.exit_code != 0

    def test_revise_slide_json_output(self, runner, mock_auth):
        """revise-slide with --json flag produces JSON output."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.revise_slide = AsyncMock(
                return_value={"artifact_id": "art_rev_3", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "task_id" in data or "artifact_id" in data or "status" in data


# =============================================================================
# GENERATE REPORT WITH DESCRIPTION (LINE 1057)
# =============================================================================


class TestGenerateReportWithNonBriefingFormat:
    """Test generate report when description is provided with non-briefing-doc format.

    Line 1057: the else-branch that sets custom_prompt = description when
    report_format != 'briefing-doc' and description is provided.
    """

    def test_report_description_with_study_guide_format(self, runner, mock_auth):
        """Line 1057: description + non-default format → custom_prompt = description."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_report = AsyncMock(
                return_value={"artifact_id": "report_xyz", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

        assert result.exit_code == 0
        mock_client.artifacts.generate_report.assert_called_once()
        call_kwargs = mock_client.artifacts.generate_report.call_args.kwargs
        # custom_prompt should be the description argument
        assert call_kwargs.get("custom_prompt") == "Focus on beginners"

    def test_report_description_with_blog_post_format(self, runner, mock_auth):
        """Line 1057: description + blog-post format → custom_prompt set."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_report = AsyncMock(
                return_value={"artifact_id": "report_abc", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
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
                )

        assert result.exit_code == 0
        mock_client.artifacts.generate_report.assert_called_once()
        call_kwargs = mock_client.artifacts.generate_report.call_args.kwargs
        assert call_kwargs.get("custom_prompt") == "Write in casual tone"


# =============================================================================
# HANDLE_GENERATION_RESULT PATHS (GenerationStatus and list result formats)
# =============================================================================


class TestHandleGenerationResultPaths:
    """Test handle_generation_result branches: GenerationStatus input and list input."""

    def test_generation_result_with_generation_status_object(self, runner, mock_auth):
        """Lines 200-201: result is a GenerationStatus → task_id = result.task_id."""
        from notebooklm.types import GenerationStatus

        status = GenerationStatus(
            task_id="task_gen_1", status="pending", error=None, error_code=None
        )

        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=status)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123"])

        assert result.exit_code == 0
        assert "task_gen_1" in result.output or "Started" in result.output

    def test_generation_result_with_list_input(self, runner, mock_auth):
        """Lines 205-207: result is a list → task_id from first element."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=["task_list_1", "extra"])
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123"])

        assert result.exit_code == 0
        assert "task_list_1" in result.output or "Started" in result.output

    def test_generation_result_falsy_shows_failed_message(self, runner, mock_auth):
        """Line 173: falsy result → text error message."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123"])

        assert result.exit_code == 0
        assert "generation failed" in result.output.lower()

    def test_generation_result_falsy_json_shows_error(self, runner, mock_auth):
        """Line 173: falsy result with --json → json_error_response (exits with code 1)."""
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123", "--json"])

        # json_error_response calls sys.exit(1), so exit_code is 1
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "GENERATION_FAILED"

    def test_generation_with_wait_and_generation_status(self, runner, mock_auth):
        """Line 213: wait=True with GenerationStatus triggers wait_for_completion."""
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

        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=initial_status)
            mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123", "--wait"])

        assert result.exit_code == 0
        mock_client.artifacts.wait_for_completion.assert_called_once()


# =============================================================================
# ADDITIONAL TARGETED COVERAGE TESTS
# =============================================================================


class TestGenerateWithRetryConsoleOutput:
    """Test generate_with_retry console output branch (line 111)."""

    @pytest.mark.asyncio
    async def test_retry_shows_console_message_when_not_json(self):
        """Line 111: console.print shown during retry when json_output=False."""
        import importlib

        from notebooklm.types import GenerationStatus

        generate_module = importlib.import_module("notebooklm.cli.generate")

        rate_limited = GenerationStatus(
            task_id="", status="failed", error="Rate limited", error_code="USER_DISPLAYABLE_ERROR"
        )
        success_result = GenerationStatus(
            task_id="task_123", status="pending", error=None, error_code=None
        )
        generate_fn = AsyncMock(side_effect=[rate_limited, success_result])

        with (
            patch.object(generate_module, "console") as mock_console,
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await generate_module.generate_with_retry(
                generate_fn, max_retries=1, artifact_type="audio", json_output=False
            )

        assert result == success_result
        # Console should have been called with the retry message
        mock_console.print.assert_called_once()
        call_text = mock_console.print.call_args[0][0]
        assert "rate limited" in call_text.lower() or "Retrying" in call_text


class TestHandleGenerationResultListPathAndWait:
    """Test handle_generation_result: list path and wait with console message."""

    def test_wait_with_task_id_shows_generating_message(self, runner, mock_auth):
        """Line 211->213: wait=True, task_id present, not json → console.print generating."""
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

        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=initial_status)
            mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123", "--wait"])

        assert result.exit_code == 0
        # The console message "Generating audio... Task: task_console_1" should appear
        assert "task_console_1" in result.output or "Generating" in result.output
        mock_client.artifacts.wait_for_completion.assert_called_once()

    def test_list_result_extracts_task_id_for_wait(self, runner, mock_auth):
        """Lines 205->210, 213: list result + wait=True → task_id from list[0]."""
        from notebooklm.types import GenerationStatus

        completed_status = GenerationStatus(
            task_id="task_list_wait",
            status="completed",
            error=None,
            error_code=None,
            url="https://example.com/audio.mp3",
        )

        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(
                return_value=["task_list_wait", "extra"]
            )
            mock_client.artifacts.wait_for_completion = AsyncMock(return_value=completed_status)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123", "--wait"])

        assert result.exit_code == 0
        mock_client.artifacts.wait_for_completion.assert_called_once()


class TestOutputMindMapNonDictMindMap:
    """Test _output_mind_map_result when mind_map value is not a dict (line 985->else)."""

    def setup_method(self):
        import importlib

        self.generate_module = importlib.import_module("notebooklm.cli.generate")

    def test_mind_map_non_dict_value_prints_directly(self):
        """Line 985->else (988-989): mind_map is not a dict → console.print(result)."""
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
    """Cover the spinner helpers added for P5.T2 / I7."""

    def test_format_status_message_known_kind_includes_typical_hint(self):
        """Known kinds get a typical-duration parenthetical so users see an ETA."""
        msg = _format_status_message("cinematic-video")
        # The exact wording matches the audit example so the user-visible
        # surface is anchored: "Waiting for cinematic-video generation
        # (typically 30-40 min)...".
        assert "cinematic-video" in msg
        assert "typically" in msg
        assert msg.endswith("...")

    def test_format_status_message_unknown_kind_omits_hint(self):
        """Unknown kinds fall back gracefully — no parenthetical, still rendered."""
        msg = _format_status_message("unknown-kind")
        assert "unknown-kind" in msg
        assert "(" not in msg, f"unknown kind should NOT add a hint, got: {msg!r}"

    def test_format_status_message_with_elapsed_appends_seconds(self):
        """Elapsed timer is appended in `[Ns elapsed]` form for the live update."""
        msg = _format_status_message("audio", elapsed=42.7)
        # Truncated to int — the spinner's per-second tick doesn't need
        # sub-second precision and an integer reads cleaner in the UI.
        assert "[42s elapsed]" in msg

    def test_status_with_elapsed_json_output_is_no_op(self):
        """Under --json the helper must NOT call console.status (stdout stays JSON)."""

        async def _exercise() -> None:
            with patch.object(generate_module.console, "status") as mock_status:
                async with _status_with_elapsed("audio", json_output=True):
                    pass
                assert not mock_status.called, "console.status must not be invoked under --json"

        asyncio.run(_exercise())


# =============================================================================
# SIGINT / RESUME-HINT TESTS (M2 / P5.T3)
# =============================================================================


class TestGenerateWaitSigintResumeHint:
    """Ctrl-C during ``generate <kind> --wait`` surfaces the resume hint (M2).

    The hint follows the canonical phrasing
    ``Cancelled. Resume with: notebooklm artifact poll <task_id>``
    and the process exits 130. This guards against the pre-P5.T3 regression
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

        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=initial_status)
            # The polling call is where Ctrl-C lands (asyncio.sleep inside the
            # library's wait loop is the actual suspension point). Surfacing
            # KeyboardInterrupt from the awaitable is the cleanest way to
            # simulate that without spinning up a real polling loop.
            mock_client.artifacts.wait_for_completion = AsyncMock(side_effect=KeyboardInterrupt)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123", "--wait"])

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

        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=initial_status)
            mock_client.artifacts.wait_for_completion = AsyncMock(side_effect=KeyboardInterrupt)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "audio", "-n", "nb_123", "--wait", "--json"]
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
            with patch.object(generate_module.console, "status") as mock_status:
                mock_status.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mock_status.return_value.__exit__ = MagicMock(return_value=False)
                with pytest.raises(KeyboardInterrupt):
                    async with _status_with_elapsed("audio", resume_hint=None):
                        raise KeyboardInterrupt

        asyncio.run(_exercise())
