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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123", "--json"])

            data = json.loads(result.output)
            assert data["error"] is True
            assert data["code"] == "RATE_LIMITED"
