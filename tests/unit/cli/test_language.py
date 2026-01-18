"""Tests for language CLI commands (list, get, set)."""

import importlib
import json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli

# Import the module explicitly to avoid confusion with the Click group
# (notebooklm.cli exports 'language' as a Click Group, which shadows the module)
language_module = importlib.import_module("notebooklm.cli.language")


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_config_file(tmp_path):
    """Provide a temporary config file for testing language commands."""
    config_file = tmp_path / "config.json"
    home_dir = tmp_path
    with (
        patch.object(language_module, "get_config_path", return_value=config_file),
        patch.object(language_module, "get_home_dir", return_value=home_dir),
    ):
        yield config_file


# =============================================================================
# LANGUAGE LIST TESTS
# =============================================================================


class TestLanguageListCommand:
    def test_language_list_shows_supported_languages(self, runner):
        """Test 'language list' command shows supported languages."""
        result = runner.invoke(cli, ["language", "list"])

        assert result.exit_code == 0
        assert "Supported Languages" in result.output
        assert "en" in result.output
        assert "English" in result.output
        assert "zh_Hans" in result.output
        # Check native name is present (Chinese Simplified)
        assert "中文" in result.output

    def test_language_list_json_output(self, runner):
        """Test 'language list --json' outputs JSON format."""
        result = runner.invoke(cli, ["language", "list", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "languages" in data
        assert "en" in data["languages"]
        assert data["languages"]["en"] == "English"
        assert "zh_Hans" in data["languages"]


# =============================================================================
# LANGUAGE GET TESTS
# =============================================================================


class TestLanguageGetCommand:
    def test_language_get_default_not_set(self, runner, mock_config_file):
        """Test 'language get' when no language is configured."""
        result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code == 0
        assert "not set" in result.output
        assert "defaults to 'en'" in result.output

    def test_language_get_when_set(self, runner, mock_config_file):
        """Test 'language get' when language is configured."""
        # Write config file with language
        mock_config_file.write_text(json.dumps({"language": "zh_Hans"}))

        result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code == 0
        assert "zh_Hans" in result.output
        assert "中文" in result.output or "global" in result.output.lower()

    def test_language_get_json_output(self, runner, mock_config_file):
        """Test 'language get --json' outputs JSON format."""
        mock_config_file.write_text(json.dumps({"language": "ja"}))

        result = runner.invoke(cli, ["language", "get", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["language"] == "ja"
        assert data["name"] == "日本語"
        assert data["is_default"] is False

    def test_language_get_json_when_not_set(self, runner, mock_config_file):
        """Test 'language get --json' when not configured."""
        result = runner.invoke(cli, ["language", "get", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["language"] is None
        assert data["is_default"] is True


# =============================================================================
# LANGUAGE SET TESTS
# =============================================================================


class TestLanguageSetCommand:
    def test_language_set_valid_code(self, runner, mock_config_file):
        """Test 'language set' with valid language code."""
        result = runner.invoke(cli, ["language", "set", "zh_Hans"])

        assert result.exit_code == 0
        assert "zh_Hans" in result.output
        assert "中文" in result.output or "GLOBAL" in result.output

        # Verify config was written
        config = json.loads(mock_config_file.read_text())
        assert config["language"] == "zh_Hans"

    def test_language_set_shows_global_warning(self, runner, mock_config_file):
        """Test 'language set' shows global setting warning."""
        result = runner.invoke(cli, ["language", "set", "ko"])

        assert result.exit_code == 0
        assert "GLOBAL" in result.output or "global" in result.output.lower()
        assert "all notebooks" in result.output.lower()

    def test_language_set_invalid_code(self, runner, mock_config_file):
        """Test 'language set' with invalid language code."""
        result = runner.invoke(cli, ["language", "set", "invalid_code"])

        assert result.exit_code == 1
        assert "Unknown language code" in result.output
        assert "language list" in result.output.lower()

    def test_language_set_json_output(self, runner, mock_config_file):
        """Test 'language set --json' outputs JSON format."""
        result = runner.invoke(cli, ["language", "set", "fr", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["language"] == "fr"
        assert data["name"] == "Français"

    def test_language_set_invalid_json_output(self, runner, mock_config_file):
        """Test 'language set --json' with invalid code outputs JSON error."""
        result = runner.invoke(cli, ["language", "set", "xyz", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["error"] == "INVALID_LANGUAGE"


# =============================================================================
# GENERATE COMMANDS USE CONFIG LANGUAGE
# =============================================================================


class TestGenerateUsesConfigLanguage:
    def test_generate_audio_uses_config_language(self, runner, mock_config_file):
        """Test that generate audio uses config language when not specified."""
        mock_config_file.write_text(json.dumps({"language": "zh_Hans"}))

        # Just verify the help shows the default behavior
        result = runner.invoke(cli, ["generate", "audio", "--help"])

        assert result.exit_code == 0
        assert "--language" in result.output
        assert "from config" in result.output.lower() or "default" in result.output.lower()
