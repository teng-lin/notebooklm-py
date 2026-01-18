"""Tests for generate CLI commands."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli

from .conftest import create_mock_client, patch_client_for_module


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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "--wait", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Audio ready" in result.output or "example.com" in result.output

    def test_generate_audio_failure(self, runner, mock_auth):
        with patch_client_for_module("generate") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.generate_audio = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "video", "--style", "kawaii", "-n", "nb_123"]
                )

            assert result.exit_code == 0


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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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
                        "-n",
                        "nb_123",
                    ],
                )

            assert result.exit_code == 0


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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "report", "Create a white paper", "-n", "nb_123"]
                )

            assert result.exit_code == 0


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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["generate", "audio", "-n", "nb_123", "--language", "ja"]
                )

            assert result.exit_code == 0
