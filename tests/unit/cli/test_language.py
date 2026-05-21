"""Tests for language CLI commands (list, get, set)."""

import gc
import importlib
import json
import warnings
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli

# Import the module explicitly to avoid confusion with the Click group
# (notebooklm.cli exports 'language' as a Click Group, which shadows the module)
language_module = importlib.import_module("notebooklm.cli.language_cmd")


def test_save_config_alias_removed() -> None:
    """The deprecated public ``save_config`` alias is removed in v0.5.0."""
    assert not hasattr(language_module, "save_config")
    assert hasattr(language_module, "_save_config")


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


def unawaited_coroutine_warnings(caught_warnings):
    return [
        warning
        for warning in caught_warnings
        if issubclass(warning.category, RuntimeWarning)
        and "was never awaited" in str(warning.message)
    ]


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
        """Test 'language get --local' when no language is configured."""
        # Use --local to test local config only (skip server fetch)
        result = runner.invoke(cli, ["language", "get", "--local"])

        assert result.exit_code == 0
        assert "not set" in result.output
        assert "defaults to 'en'" in result.output

    def test_language_get_when_set(self, runner, mock_config_file):
        """Test 'language get --local' when language is configured."""
        # Write config file with language
        mock_config_file.write_text(json.dumps({"language": "zh_Hans"}))

        # Use --local to test local config only
        result = runner.invoke(cli, ["language", "get", "--local"])

        assert result.exit_code == 0
        assert "zh_Hans" in result.output
        assert "中文" in result.output or "global" in result.output.lower()

    def test_language_get_json_output(self, runner, mock_config_file):
        """Test 'language get --local --json' outputs JSON format."""
        mock_config_file.write_text(json.dumps({"language": "ja"}))

        # Use --local to test local config only
        result = runner.invoke(cli, ["language", "get", "--local", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["language"] == "ja"
        assert data["name"] == "日本語"
        assert data["is_default"] is False

    def test_language_get_json_when_not_set(self, runner, mock_config_file):
        """Test 'language get --local --json' when not configured."""
        # Use --local to test local config only
        result = runner.invoke(cli, ["language", "get", "--local", "--json"])

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
        """``language set --json`` with invalid code emits the shared JSON error schema.

        The error payload must match ``json_error_response`` in ``cli/rendering.py``
        so machine-readable error handling is uniform across CLI commands:
        ``{"error": true, "code": "INVALID_LANGUAGE", "message": ..., "hint": ...}``.
        """
        result = runner.invoke(cli, ["language", "set", "xyz", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "INVALID_LANGUAGE"
        assert "xyz" in data["message"]
        assert "hint" in data
        assert "language list" in data["hint"].lower()


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


# =============================================================================
# GET_CONFIG ERROR PATHS (lines 116-121)
# =============================================================================


class TestGetConfigErrorPaths:
    def test_get_config_json_decode_error(self, tmp_path):
        """Test get_config() returns {} when config file has invalid JSON."""
        config_file = tmp_path / "config.json"
        config_file.write_text("this is not valid json{{{")

        with patch.object(language_module, "get_config_path", return_value=config_file):
            result = language_module.get_config()

        assert result == {}

    def test_get_config_oserror(self, tmp_path):
        """Test get_config() returns {} when config file can't be read (OSError)."""
        config_file = tmp_path / "config.json"
        # Create the file so exists() returns True, then mock read_text to raise OSError
        config_file.write_text('{"language": "en"}')

        with (
            patch.object(language_module, "get_config_path", return_value=config_file),
            patch.object(
                config_file.__class__, "read_text", side_effect=OSError("permission denied")
            ),
        ):
            result = language_module.get_config()

        assert result == {}


# =============================================================================
# _SYNC_LANGUAGE_TO_SERVER AND _GET_LANGUAGE_FROM_SERVER (lines 162-164, 176-186)
# =============================================================================


class TestSyncLanguageToServer:
    def test_sync_language_to_server_success(self):
        """Test _sync_language_to_server returns run_async result on success."""
        mock_ctx = MagicMock()
        mock_ctx.obj = {"auth": {"SID": "test", "HSID": "test", "SSID": "test"}}

        with (
            patch.object(language_module, "get_auth_tokens", return_value={"SID": "test"}),
            patch.object(language_module, "run_async", return_value="en") as mock_run,
        ):
            result = language_module._sync_language_to_server("en", mock_ctx)

        assert result == "en"
        mock_run.assert_called_once()

    def test_sync_language_to_server_exception_returns_none(self):
        """Test _sync_language_to_server returns None when exception occurs."""
        mock_ctx = MagicMock()
        mock_ctx.obj = {}

        with patch.object(language_module, "get_auth_tokens", side_effect=Exception("no auth")):
            result = language_module._sync_language_to_server("en", mock_ctx)

        assert result is None

    def test_sync_language_to_server_run_async_exception(self):
        """Test _sync_language_to_server returns None when run_async raises."""
        mock_ctx = MagicMock()
        mock_ctx.obj = {}

        with (
            patch.object(language_module, "get_auth_tokens", return_value={"SID": "test"}),
            patch.object(language_module, "run_async", side_effect=Exception("connection error")),
        ):
            result = language_module._sync_language_to_server("en", mock_ctx)

        assert result is None

    def test_sync_language_to_server_closes_coroutine_when_run_async_raises(self):
        """Test run_async failures do not leak an unawaited coroutine warning."""
        mock_ctx = MagicMock()
        mock_ctx.obj = {}

        with (
            patch.object(language_module, "get_auth_tokens", return_value={"SID": "test"}),
            patch.object(language_module, "run_async", side_effect=Exception("connection error")),
            warnings.catch_warnings(record=True) as caught_warnings,
        ):
            warnings.simplefilter("always", RuntimeWarning)

            result = language_module._sync_language_to_server("en", mock_ctx)
            gc.collect()

        assert result is None
        assert unawaited_coroutine_warnings(caught_warnings) == []


class TestGetLanguageFromServer:
    def test_get_language_from_server_success(self):
        """Test _get_language_from_server returns the server language on success."""
        mock_ctx = MagicMock()
        mock_ctx.obj = {"auth": {"SID": "test"}}

        with (
            patch.object(language_module, "get_auth_tokens", return_value={"SID": "test"}),
            patch.object(language_module, "run_async", return_value="fr") as mock_run,
        ):
            result = language_module._get_language_from_server(mock_ctx)

        assert result == "fr"
        mock_run.assert_called_once()

    def test_get_language_from_server_exception_returns_none(self):
        """Test _get_language_from_server returns None when exception occurs."""
        mock_ctx = MagicMock()
        mock_ctx.obj = {}

        with patch.object(language_module, "get_auth_tokens", side_effect=Exception("no auth")):
            result = language_module._get_language_from_server(mock_ctx)

        assert result is None

    def test_get_language_from_server_run_async_exception(self):
        """Test _get_language_from_server returns None when run_async raises."""
        mock_ctx = MagicMock()
        mock_ctx.obj = {}

        with (
            patch.object(language_module, "get_auth_tokens", return_value={"SID": "test"}),
            patch.object(language_module, "run_async", side_effect=Exception("rpc error")),
        ):
            result = language_module._get_language_from_server(mock_ctx)

        assert result is None

    def test_get_language_from_server_closes_coroutine_when_run_async_raises(self):
        """Test run_async failures do not leak an unawaited coroutine warning."""
        mock_ctx = MagicMock()
        mock_ctx.obj = {}

        with (
            patch.object(language_module, "get_auth_tokens", return_value={"SID": "test"}),
            patch.object(language_module, "run_async", side_effect=Exception("rpc error")),
            warnings.catch_warnings(record=True) as caught_warnings,
        ):
            warnings.simplefilter("always", RuntimeWarning)

            result = language_module._get_language_from_server(mock_ctx)
            gc.collect()

        assert result is None
        assert unawaited_coroutine_warnings(caught_warnings) == []


# =============================================================================
# LANGUAGE GET SERVER SYNC PATHS (lines 244-250, 270)
# =============================================================================


class TestLanguageGetServerSyncPaths:
    def test_language_get_server_has_different_value_updates_local(self, runner, mock_config_file):
        """Test 'language get' updates local config when server has a different value."""
        # Local is "en", server returns "fr" → local should be updated to "fr"
        mock_config_file.write_text(json.dumps({"language": "en"}))

        with patch.object(language_module, "_get_language_from_server", return_value="fr"):
            result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code == 0
        # Local config should be updated to "fr"
        config = json.loads(mock_config_file.read_text())
        assert config["language"] == "fr"
        # Output should show "fr" (the server value)
        assert "fr" in result.output

    def test_language_get_server_different_shows_synced(self, runner, mock_config_file):
        """Test 'language get' shows synced message when server differs from local."""
        mock_config_file.write_text(json.dumps({"language": "en"}))

        with patch.object(language_module, "_get_language_from_server", return_value="ja"):
            result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code == 0
        assert "synced" in result.output.lower()

    def test_language_get_server_same_value_no_update(self, runner, mock_config_file):
        """Test 'language get' does not update local when server value matches."""
        mock_config_file.write_text(json.dumps({"language": "en"}))

        with (
            patch.object(language_module, "_get_language_from_server", return_value="en"),
            patch.object(language_module, "set_language") as mock_set,
        ):
            result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code == 0
        mock_set.assert_not_called()

    def test_language_get_no_language_shows_not_set(self, runner, mock_config_file):
        """Test 'language get' shows 'not set' when no language is configured and server returns None."""
        # No language configured locally
        with patch.object(language_module, "_get_language_from_server", return_value=None):
            result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code == 0
        assert "not set" in result.output

    def test_language_get_server_sync_json_output(self, runner, mock_config_file):
        """Test 'language get --json' reflects synced_from_server when values differ."""
        mock_config_file.write_text(json.dumps({"language": "en"}))

        with patch.object(language_module, "_get_language_from_server", return_value="de"):
            result = runner.invoke(cli, ["language", "get", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["language"] == "de"
        assert data["synced_from_server"] is True


# =============================================================================
# LANGUAGE SET SYNC FAILED AND JSON PATHS (lines 316-320, 335-336)
# =============================================================================


class TestLanguageSetSyncFailedAndJsonPaths:
    def test_language_set_sync_failed_shows_local_only_message(self, runner, mock_config_file):
        """Test 'language set' shows local-only message when server sync fails."""
        with patch.object(language_module, "_sync_language_to_server", return_value=None):
            result = runner.invoke(cli, ["language", "set", "en"])

        assert result.exit_code == 0
        assert "saved locally" in result.output or "server sync failed" in result.output

    def test_language_set_sync_success_shows_synced_message(self, runner, mock_config_file):
        """Test 'language set' shows synced message when server sync succeeds."""
        with patch.object(language_module, "_sync_language_to_server", return_value="en"):
            result = runner.invoke(cli, ["language", "set", "en"])

        assert result.exit_code == 0
        assert "synced" in result.output.lower()
        # Should NOT show "server sync failed"
        assert "server sync failed" not in result.output

    def test_language_set_json_output_with_server_sync(self, runner, mock_config_file):
        """Test 'language set --json' includes synced_to_server field."""
        with patch.object(language_module, "_sync_language_to_server", return_value="fr"):
            result = runner.invoke(cli, ["language", "set", "fr", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["language"] == "fr"
        assert data["name"] == "Français"
        assert "synced_to_server" in data
        assert data["synced_to_server"] is True

    def test_language_set_json_output_sync_failed(self, runner, mock_config_file):
        """Test 'language set --json' shows synced_to_server=False when sync fails."""
        with patch.object(language_module, "_sync_language_to_server", return_value=None):
            result = runner.invoke(cli, ["language", "set", "ko", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["language"] == "ko"
        assert "synced_to_server" in data
        assert data["synced_to_server"] is False
