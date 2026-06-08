"""Tests for language CLI commands (list, get, set)."""

import importlib
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.exceptions import AuthError, NetworkError
from notebooklm.notebooklm_cli import cli

# Import the module explicitly to avoid confusion with the Click group
# (notebooklm.cli exports 'language' as a Click Group, which shadows the module)
language_module = importlib.import_module("notebooklm.cli.language_cmd")


def test_save_config_alias_removed() -> None:
    """The deprecated public ``save_config`` alias is removed in v0.5.0."""
    assert not hasattr(language_module, "save_config")
    assert hasattr(language_module, "_save_config")


def test_swallow_helpers_removed() -> None:
    """The broad-``except`` silent-degrade helpers are gone (issue #1309).

    Server interaction now routes through the standard error envelope; the
    old helpers that swallowed auth/network/RPC errors must not return.
    """
    assert not hasattr(language_module, "_get_language_from_server")
    assert not hasattr(language_module, "_sync_language_to_server")
    assert not hasattr(language_module, "_run_language_rpc")


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


def write_config(path, config):
    """Write a config dict to ``path`` with explicit UTF-8 + LF semantics.

    Centralizes the cross-platform file-write contract (utf-8 encoding, ``\n``
    newlines, ``ensure_ascii=False``) so individual tests stay terse and no
    test relies on the platform default of ``Path.write_text``.
    """
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(config, ensure_ascii=False))


def read_config(path):
    """Read a config dict from ``path`` with explicit UTF-8 decoding."""
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def mock_server(*, get_returns="en", set_returns="en", get_error=None, set_error=None):
    """Patch the auth bootstrap + ``NotebookLMClient`` for the server path.

    ``language get``/``set`` (without ``--local``) now route through
    ``with_auth_and_errors`` → ``NotebookLMClient`` → ``client.settings``.
    This stubs that whole stack so a unit test can drive the server response
    (or inject an auth/network error to exercise the hard-fail envelope).
    """
    settings = MagicMock()
    if get_error is not None:
        settings.get_output_language = AsyncMock(side_effect=get_error)
    else:
        settings.get_output_language = AsyncMock(return_value=get_returns)
    if set_error is not None:
        settings.set_output_language = AsyncMock(side_effect=set_error)
    else:
        settings.set_output_language = AsyncMock(return_value=set_returns)

    client = MagicMock()
    client.settings = settings
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load,
        patch("notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock) as mock_fetch,
        patch.object(language_module, "NotebookLMClient", return_value=client),
    ):
        mock_load.return_value = {
            "SID": "test",
            "__Secure-1PSIDTS": "test_1psidts",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        mock_fetch.return_value = ("csrf", "session")
        yield settings


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
        write_config(mock_config_file, {"language": "zh_Hans"})

        # Use --local to test local config only
        result = runner.invoke(cli, ["language", "get", "--local"])

        assert result.exit_code == 0
        assert "zh_Hans" in result.output
        assert "中文" in result.output or "global" in result.output.lower()

    def test_language_get_json_output(self, runner, mock_config_file):
        """Test 'language get --local --json' outputs JSON format."""
        write_config(mock_config_file, {"language": "ja"})

        # Use --local to test local config only
        result = runner.invoke(cli, ["language", "get", "--local", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["language"] == "ja"
        assert data["name"] == "日本語"
        assert data["is_default"] is False
        # --local never contacts the server, so the sync flag is always False.
        assert data["synced_from_server"] is False

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
        """Test 'language set' with valid language code (server-authoritative)."""
        with mock_server(set_returns="zh_Hans") as settings:
            result = runner.invoke(cli, ["language", "set", "zh_Hans"])

        assert result.exit_code == 0
        assert "zh_Hans" in result.output
        assert "中文" in result.output or "GLOBAL" in result.output
        # Server sync happened before the local write.
        settings.set_output_language.assert_awaited_once_with("zh_Hans")

        # Verify config was written (only after the server confirmed).
        config = read_config(mock_config_file)
        assert config["language"] == "zh_Hans"

    def test_language_set_shows_global_warning(self, runner, mock_config_file):
        """Test 'language set' shows global setting warning."""
        with mock_server(set_returns="ko"):
            result = runner.invoke(cli, ["language", "set", "ko"])

        assert result.exit_code == 0
        assert "GLOBAL" in result.output or "global" in result.output.lower()
        assert "all notebooks" in result.output.lower()

    def test_language_set_invalid_code(self, runner, mock_config_file):
        """Test 'language set' with invalid language code.

        Validation runs before any auth/client, so no server fixture is needed
        and storage is never touched.
        """
        result = runner.invoke(cli, ["language", "set", "invalid_code"])

        assert result.exit_code == 1
        assert "Unknown language code" in result.output
        assert "language list" in result.output.lower()
        assert not mock_config_file.exists()

    def test_language_set_json_output(self, runner, mock_config_file):
        """Test 'language set --json' outputs JSON format."""
        with mock_server(set_returns="fr"):
            result = runner.invoke(cli, ["language", "set", "fr", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["language"] == "fr"
        assert data["name"] == "Français"
        assert data["synced_to_server"] is True

    def test_language_set_local_skips_server(self, runner, mock_config_file):
        """``language set --local`` persists locally with NO auth/client (offline)."""
        # No mock_server fixture: if --local touched auth/client this would fail.
        result = runner.invoke(cli, ["language", "set", "ja", "--local"])

        assert result.exit_code == 0
        assert "ja" in result.output
        assert "server sync skipped" in result.output
        config = read_config(mock_config_file)
        assert config["language"] == "ja"

    def test_language_set_local_json(self, runner, mock_config_file):
        """``language set --local --json`` reports ``synced_to_server`` False offline."""
        result = runner.invoke(cli, ["language", "set", "ko", "--local", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["language"] == "ko"
        assert data["synced_to_server"] is False

    def test_language_set_server_error_hard_fails(self, runner, mock_config_file):
        """A server RPC failure surfaces the envelope + non-zero exit (NOT swallowed)."""
        with mock_server(set_error=NetworkError("connection refused")):
            result = runner.invoke(cli, ["language", "set", "fr"])

        assert result.exit_code != 0
        assert "Network error" in result.output or "error" in result.output.lower()
        # Server-authoritative ordering: the failed sync must NOT leave a
        # misleading local value behind.
        assert not mock_config_file.exists()

    def test_language_set_server_error_json_envelope(self, runner, mock_config_file):
        """``language set --json`` on a server failure emits the typed error envelope."""
        with mock_server(set_error=AuthError("expired session")):
            result = runner.invoke(cli, ["language", "set", "fr", "--json"])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "AUTH_ERROR"
        assert "synced_to_server" not in data
        # Local value never written when the server rejected the change.
        assert not mock_config_file.exists()

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
        write_config(mock_config_file, {"language": "zh_Hans"})

        # Just verify the help shows the default behavior
        result = runner.invoke(cli, ["generate", "audio", "--help"])

        assert result.exit_code == 0
        assert "--language" in result.output
        assert "from config" in result.output.lower() or "default" in result.output.lower()


# =============================================================================
# GET_CONFIG ERROR PATHS
#
# The pure ``get_config()`` corrupt-JSON / OSError fallback tests moved to
# ``tests/unit/app/test_app_language.py::TestGetConfig`` (the behavior lives in
# ``_app/language.LanguageConfigStore.get_config``; the CLI ``get_config()`` is
# a thin wrapper that binds this module's path/home helpers into the store).
# =============================================================================


# =============================================================================
# LANGUAGE GET SERVER PATH (issue #1309: hard-fail via the error envelope)
# =============================================================================


class TestLanguageGetServerPath:
    def test_server_different_value_updates_local(self, runner, mock_config_file):
        """'language get' updates local config when the server has a different value."""
        # Local is "en", server returns "fr" → local should be updated to "fr".
        write_config(mock_config_file, {"language": "en"})

        with mock_server(get_returns="fr") as settings:
            result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code == 0
        settings.get_output_language.assert_awaited_once()
        config = read_config(mock_config_file)
        assert config["language"] == "fr"
        assert "fr" in result.output

    def test_server_different_shows_synced(self, runner, mock_config_file):
        """'language get' shows the synced message when the server differs from local."""
        write_config(mock_config_file, {"language": "en"})

        with mock_server(get_returns="ja"):
            result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code == 0
        assert "synced" in result.output.lower()

    def test_server_same_value_no_update(self, runner, mock_config_file):
        """'language get' does not rewrite local config when the server value matches."""
        write_config(mock_config_file, {"language": "en"})

        with (
            mock_server(get_returns="en"),
            patch.object(language_module, "set_language") as mock_set,
        ):
            result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code == 0
        mock_set.assert_not_called()

    def test_server_returns_none_falls_back_to_local(self, runner, mock_config_file):
        """When the server has no value set, fall back to local for display."""
        write_config(mock_config_file, {"language": "de"})

        with mock_server(get_returns=None):
            result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code == 0
        assert "de" in result.output

    def test_server_and_local_unset_shows_not_set(self, runner, mock_config_file):
        """'language get' shows 'not set' when neither server nor local has a value."""
        with mock_server(get_returns=None):
            result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code == 0
        assert "not set" in result.output

    def test_server_sync_json_output(self, runner, mock_config_file):
        """'language get --json' reflects synced_from_server when values differ."""
        write_config(mock_config_file, {"language": "en"})

        with mock_server(get_returns="de"):
            result = runner.invoke(cli, ["language", "get", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["language"] == "de"
        assert data["synced_from_server"] is True

    def test_server_error_hard_fails(self, runner, mock_config_file):
        """A server RPC failure surfaces the envelope + non-zero exit (NOT swallowed)."""
        write_config(mock_config_file, {"language": "en"})

        with mock_server(get_error=NetworkError("connection refused")):
            result = runner.invoke(cli, ["language", "get"])

        assert result.exit_code != 0
        assert "Network error" in result.output or "error" in result.output.lower()
        # Local config must be left untouched on a failed fetch.
        config = read_config(mock_config_file)
        assert config["language"] == "en"

    def test_server_error_json_envelope(self, runner, mock_config_file):
        """'language get --json' on a server failure emits the typed error envelope."""
        with mock_server(get_error=AuthError("expired session")):
            result = runner.invoke(cli, ["language", "get", "--json"])

        assert result.exit_code != 0
        data = json.loads(result.output)
        assert data["error"] is True
        assert data["code"] == "AUTH_ERROR"
        # The success payload keys must not leak into the error envelope.
        assert "synced_from_server" not in data
