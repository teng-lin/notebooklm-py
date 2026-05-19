"""Tests for the ``auth`` subgroup (check, logout, refresh, inspect) and the ``--browser-cookies`` login path.

This file was extracted from the legacy ``test_session.py`` as part of
D1 PR-3 (test-monkeypatch-policy migration). The shared fixtures and
helpers live in ``_session_helpers.py``; the proxy-block-aware
``patch_session_login_dual`` lives in ``tests/_fixtures``.
"""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from _fixtures import patch_session_login_dual
from notebooklm.notebooklm_cli import cli

from ._session_helpers import (
    _multiaccount_rookiepy_mock,
    _read_account,
)


class TestAuthCheckCommand:
    """Tests for the 'auth check' command."""

    @pytest.fixture
    def mock_storage_path(self, tmp_path):
        """Provide a temporary storage path for testing."""
        storage_file = tmp_path / "storage_state.json"
        with patch_session_login_dual("get_storage_path", return_value=storage_file):
            yield storage_file

    def test_auth_check_storage_not_found(self, runner, mock_storage_path):
        """Test auth check when storage file doesn't exist."""
        # Ensure file doesn't exist
        if mock_storage_path.exists():
            mock_storage_path.unlink()

        result = runner.invoke(cli, ["auth", "check"])

        assert result.exit_code == 0
        assert "Storage exists" in result.output
        assert "fail" in result.output.lower() or "✗" in result.output

    def test_auth_check_storage_not_found_json(self, runner, mock_storage_path):
        """Test auth check --json when storage file doesn't exist.

        Spec: failure paths in --json mode must exit nonzero so automation
        can fail-fast on `notebooklm auth check --json`.
        """
        if mock_storage_path.exists():
            mock_storage_path.unlink()

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["checks"]["storage_exists"] is False
        assert "not found" in output["details"]["error"]

    def test_auth_check_invalid_json(self, runner, mock_storage_path):
        """Test auth check when storage file contains invalid JSON."""
        mock_storage_path.write_text("{ invalid json }")

        result = runner.invoke(cli, ["auth", "check"])

        assert result.exit_code == 0
        assert "JSON valid" in result.output
        assert "fail" in result.output.lower() or "✗" in result.output

    def test_auth_check_invalid_json_output(self, runner, mock_storage_path):
        """Test auth check --json when storage contains invalid JSON.

        Spec: failure paths in --json mode must exit nonzero.
        """
        mock_storage_path.write_text("not valid json at all")

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["checks"]["storage_exists"] is True
        assert output["checks"]["json_valid"] is False
        assert "Invalid JSON" in output["details"]["error"]

    def test_auth_check_missing_sid_cookie(self, runner, mock_storage_path):
        """Test auth check when SID cookie is missing."""
        # Valid JSON but no SID cookie
        storage_data = {
            "cookies": [
                {"name": "OTHER", "value": "test", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check"])

        assert result.exit_code == 0
        assert "SID" in result.output or "cookie" in result.output.lower()

    def test_auth_check_valid_storage(self, runner, mock_storage_path):
        """Test auth check with valid storage containing SID."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "test_ssid", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check"])

        assert result.exit_code == 0
        assert "pass" in result.output.lower() or "✓" in result.output
        assert "Authentication is valid" in result.output

    def test_auth_check_valid_storage_json(self, runner, mock_storage_path):
        """Test auth check --json with valid storage."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["checks"]["storage_exists"] is True
        assert output["checks"]["json_valid"] is True
        assert output["checks"]["cookies_present"] is True
        assert output["checks"]["sid_cookie"] is True
        assert "SID" in output["details"]["cookies_found"]

    def test_auth_check_missing_1psidts_surfaces_tier1_error(self, runner, mock_storage_path):
        """SID present but ``__Secure-1PSIDTS`` absent must surface the Tier 1 error.

        Pinned by the #371 two-tier pre-flight: ``MINIMUM_REQUIRED_COOKIES``
        now contains both ``SID`` and ``__Secure-1PSIDTS``; the load helpers
        in ``auth.py`` raise on absence, and ``auth check`` reports the raised
        ``ValueError`` so users see the new diagnostic.

        The fix closes the previous exit-code gap: ``auth check --json`` now exits
        nonzero whenever it reports ``status="error"``.
        """
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                # Note: __Secure-1PSIDTS deliberately omitted.
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "test_ssid", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["checks"]["cookies_present"] is False
        assert "__Secure-1PSIDTS" in output["details"].get("error", "")

    def test_auth_check_with_test_flag_success(self, runner, mock_storage_path):
        """Test auth check --test with successful token fetch."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_token_abc", "session_id_xyz")

            result = runner.invoke(cli, ["auth", "check", "--test"])

        assert result.exit_code == 0
        assert "Token fetch" in result.output
        assert "pass" in result.output.lower() or "✓" in result.output

    def test_auth_check_with_test_flag_failure(self, runner, mock_storage_path):
        """Test auth check --test when token fetch fails."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = ValueError("Authentication expired")

            result = runner.invoke(cli, ["auth", "check", "--test"])

        assert result.exit_code == 0
        assert "Token fetch" in result.output
        assert "fail" in result.output.lower() or "✗" in result.output
        assert "expired" in result.output.lower() or "refresh" in result.output.lower()

    def test_auth_check_with_test_flag_json(self, runner, mock_storage_path):
        """Test auth check --test --json with successful token fetch."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_12345", "sess_67890")

            result = runner.invoke(cli, ["auth", "check", "--test", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["checks"]["token_fetch"] is True
        assert output["details"]["csrf_length"] == 10
        assert output["details"]["session_id_length"] == 10

    def test_auth_check_env_var_takes_precedence(self, runner, mock_storage_path, monkeypatch):
        """Test auth check uses NOTEBOOKLM_AUTH_JSON when set."""
        # Even if storage file doesn't exist, env var should work
        if mock_storage_path.exists():
            mock_storage_path.unlink()

        env_storage = {
            "cookies": [
                {"name": "SID", "value": "env_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(env_storage))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["details"]["auth_source"] == "NOTEBOOKLM_AUTH_JSON"

    def test_auth_check_shows_cookie_domains(self, runner, mock_storage_path):
        """Test auth check displays cookie domains."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "NID", "value": "test_nid", "domain": ".google.com.sg"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        # Use ``==`` membership rather than ``in`` to keep CodeQL's
        # ``py/incomplete-url-substring-sanitization`` rule from flagging a
        # false positive — ``cookie_domains`` is a list, not a URL string.
        assert any(d == ".google.com" for d in output["details"]["cookie_domains"])

    def test_auth_check_shows_cookies_by_domain(self, runner, mock_storage_path):
        """Test auth check --json includes detailed cookies_by_domain."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "test_hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "test_ssid", "domain": ".google.com"},
                {"name": "SID", "value": "regional_sid", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSID", "value": "secure1", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        cookies_by_domain = output["details"]["cookies_by_domain"]

        # Verify .google.com has expected cookies. ``.get(...) is not None``
        # silences CodeQL's ``py/incomplete-url-substring-sanitization`` —
        # ``cookies_by_domain`` is a dict keyed by exact domain, not a URL
        # being substring-validated.
        assert cookies_by_domain.get(".google.com") is not None
        assert "SID" in cookies_by_domain[".google.com"]
        assert "HSID" in cookies_by_domain[".google.com"]
        assert "__Secure-1PSID" in cookies_by_domain[".google.com"]

        # Verify regional domain has its cookies
        assert cookies_by_domain.get(".google.com.sg") is not None
        assert "SID" in cookies_by_domain[".google.com.sg"]

    def test_auth_check_skipped_token_fetch_shown(self, runner, mock_storage_path):
        """Test auth check shows token fetch as skipped when --test not used."""
        storage_data = {
            "cookies": [
                {"name": "SID", "value": "test_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        mock_storage_path.write_text(json.dumps(storage_data))

        result = runner.invoke(cli, ["auth", "check", "--json"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["checks"]["token_fetch"] is None  # Not tested

    def test_auth_check_help(self, runner):
        """Test auth check --help shows usage information."""
        result = runner.invoke(cli, ["auth", "check", "--help"])

        assert result.exit_code == 0
        assert "Check authentication status" in result.output
        assert "--test" in result.output
        assert "--json" in result.output


# =============================================================================
# LOGIN LANGUAGE SYNC TESTS
# =============================================================================


class TestLoginBrowserCookies:
    """Tests for notebooklm login --browser-cookies."""

    def test_browser_cookies_in_help(self, runner):
        """--browser-cookies appears in login --help."""
        result = runner.invoke(cli, ["login", "--help"])
        assert "--browser-cookies" in result.output

    def test_rookiepy_not_installed_shows_error(self, runner):
        """Shows helpful error when rookiepy is not installed."""
        with patch.dict(sys.modules, {"rookiepy": None}):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code != 0
        assert "rookiepy" in result.output
        assert "pip install" in result.output

    def test_auto_detect_calls_rookiepy_load(self, runner, tmp_path):
        """Auto-detect calls rookiepy.load()."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "abc",
                "path": "/",
                "secure": True,
                "expires": 1234567890,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "test_1psidts",
                "path": "/",
                "secure": True,
                "expires": 1234567890,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(return_value=mock_cookies)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code == 0, result.output
        mock_rookiepy.load.assert_called_once()

    def test_named_browser_calls_rookiepy_function(self, runner, tmp_path):
        """Named browser calls the matching rookiepy function."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "abc",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "test_1psidts",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.chrome = MagicMock(return_value=mock_cookies)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "chrome"])
        assert result.exit_code == 0, result.output
        mock_rookiepy.chrome.assert_called_once()

    def test_no_google_cookies_shows_error(self, runner, tmp_path):
        """Shows error when no Google cookies found."""
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(return_value=[])

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code != 0
        assert "SID" in result.output or "Google" in result.output

    def test_locked_db_shows_close_browser_hint(self, runner, tmp_path):
        """Shows close-browser hint when DB is locked."""
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(side_effect=OSError("database is locked"))

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        assert result.exit_code != 0
        output_lower = result.output.lower()
        assert "close" in output_lower or "browser" in output_lower

    def test_cookies_saved_to_storage_file(self, runner, tmp_path):
        """Cookies are written to storage_state.json."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "mysid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "test_1psidts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "APISID",
                "value": "apisid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "SAPISID",
                "value": "sapisid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(return_value=mock_cookies)

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            runner.invoke(cli, ["login", "--browser-cookies", "auto"])
        data = json.loads(storage_file.read_text())
        assert any(c["name"] == "SID" and c["value"] == "mysid" for c in data["cookies"])

    def test_unknown_browser_shows_error(self, runner, tmp_path):
        """Unknown browser name shows a clear error."""
        mock_rookiepy = MagicMock()
        mock_rookiepy.load = MagicMock(
            side_effect=AttributeError("module has no attribute 'netscape'")
        )

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "netscape"])
        assert result.exit_code != 0

    # ------------------------------------------------------------------
    # firefox::<container> syntax (issue #367)
    # ------------------------------------------------------------------

    def test_firefox_container_syntax_invokes_extractor(self, runner, tmp_path):
        """``--browser-cookies firefox::<name>`` calls the container extractor.

        rookiepy must NOT be touched on this path — that's the whole point
        of the bypass.
        """
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "work_sid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
        ]
        mock_rookiepy = MagicMock()
        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch(
                "notebooklm.cli._firefox_containers.find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch(
                "notebooklm.cli._firefox_containers.resolve_container_id",
                return_value=2,
            ),
            patch(
                "notebooklm.cli._firefox_containers.extract_firefox_container_cookies",
                return_value=mock_cookies,
            ) as mock_extract,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::Work"])
        assert result.exit_code == 0, result.output
        mock_extract.assert_called_once()
        # rookiepy must NOT have been called for the firefox:: path.
        mock_rookiepy.firefox.assert_not_called()
        mock_rookiepy.load.assert_not_called()
        # The container's SID should land in the saved storage state.
        data = json.loads(storage_file.read_text())
        assert any(c["name"] == "SID" and c["value"] == "work_sid" for c in data["cookies"])

    def test_firefox_container_none_passes_literal_none(self, runner, tmp_path):
        """``firefox::none`` resolves to ``"none"`` and skips rookiepy."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "default_sid",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
                "same_site": 0,
            },
        ]
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch(
                "notebooklm.cli._firefox_containers.find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch(
                "notebooklm.cli._firefox_containers.extract_firefox_container_cookies",
                return_value=mock_cookies,
            ) as mock_extract,
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::none"])
        assert result.exit_code == 0, result.output
        # Confirm the extractor was called with the ``"none"`` sentinel.
        _, kwargs = mock_extract.call_args
        positional = mock_extract.call_args.args
        # signature: extract_firefox_container_cookies(profile, container_id, domains=…)
        assert positional[1] == "none" or kwargs.get("container_id") == "none"

    def test_firefox_container_unknown_name_shows_listing(self, runner, tmp_path):
        """Unknown container name shows a helpful error and exits non-zero."""
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch(
                "notebooklm.cli._firefox_containers.find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch(
                "notebooklm.cli._firefox_containers.resolve_container_id",
                side_effect=ValueError(
                    "Firefox container 'Nope' not found. Available containers: 'Work', 'Personal'."
                ),
            ),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::Nope"])
        assert result.exit_code != 0
        assert "Nope" in result.output
        assert "Work" in result.output

    def test_firefox_container_no_firefox_profile_shows_error(self, runner, tmp_path):
        """Missing Firefox install shows a friendly error, not a stack trace."""
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch(
                "notebooklm.cli._firefox_containers.find_firefox_profile_path",
                return_value=None,
            ),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::Work"])
        assert result.exit_code != 0
        # The message should mention firefox / profile so the user knows what's up.
        out_lower = result.output.lower()
        assert "firefox" in out_lower
        assert "profile" in out_lower

    def test_firefox_empty_container_spec_rejected(self, runner, tmp_path):
        """`--browser-cookies firefox::` (empty spec) must error, not silently
        fall through to the unfiltered merge this feature exists to prevent.
        Regression guard for the polish review (3-way HIGH consensus).
        """
        with (
            patch.dict("sys.modules", {"rookiepy": MagicMock()}),
            patch_session_login_dual(
                "get_storage_path",
                return_value=tmp_path / "storage.json",
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox::"])
        assert result.exit_code != 0
        assert "Empty Firefox container specifier" in result.output
        # The error should point at the correct syntax so the user can recover.
        assert "firefox::none" in result.output
        assert "container-name" in result.output

    def test_unscoped_firefox_warns_when_containers_in_use(self, runner, tmp_path):
        """Unscoped ``firefox`` emits a yellow warning if containers are in use."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.firefox = MagicMock(return_value=mock_cookies)
        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch(
                "notebooklm.cli._firefox_containers.find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch(
                "notebooklm.cli._firefox_containers.has_container_cookies_in_use",
                return_value=True,
            ),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox"])
        assert result.exit_code == 0, result.output
        # Rich may wrap the message; assert on substrings that survive wrap.
        assert "Multi-Account" in result.output
        assert "firefox::" in result.output

    def test_unscoped_firefox_no_warning_when_no_containers(self, runner, tmp_path):
        """No warning when the profile is not actually using containers."""
        storage_file = tmp_path / "storage.json"
        mock_cookies = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
            {
                "domain": ".google.com",
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "path": "/",
                "secure": True,
                "expires": 9999,
                "http_only": False,
            },
        ]
        mock_rookiepy = MagicMock()
        mock_rookiepy.firefox = MagicMock(return_value=mock_cookies)
        with (
            patch.dict("sys.modules", {"rookiepy": mock_rookiepy}),
            patch(
                "notebooklm.cli._firefox_containers.find_firefox_profile_path",
                return_value=tmp_path / "ff_profile",
            ),
            patch(
                "notebooklm.cli._firefox_containers.has_container_cookies_in_use",
                return_value=False,
            ),
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch("notebooklm.cli.session._sync_server_language_to_config"),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf", "sess"),
            ),
        ):
            result = runner.invoke(cli, ["login", "--browser-cookies", "firefox"])
        assert result.exit_code == 0, result.output
        assert "Multi-Account" not in result.output


# =============================================================================
# AUTH LOGOUT COMMAND TESTS
# =============================================================================


class TestAuthLogoutCommand:
    def test_auth_logout_deletes_storage_and_browser_profile(
        self, runner, tmp_path, mock_context_file
    ):
        """Test auth logout deletes both storage_state.json and browser_profile/."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        mock_context_file.write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@example.com"}})
        )
        browser_dir = tmp_path / "browser_profile"
        browser_dir.mkdir()
        (browser_dir / "Default").mkdir()
        (browser_dir / "Default" / "Cookies").write_text("data")

        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "Logged out" in result.output
        assert not storage_file.exists()
        assert not mock_context_file.exists()
        assert not browser_dir.exists()

    def test_auth_logout_when_already_logged_out(self, runner, tmp_path, mock_context_file):
        """Test auth logout is a no-op with friendly message when not logged in."""
        storage_file = tmp_path / "storage.json"
        browser_dir = tmp_path / "browser_profile"
        # Neither exists

        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "already" in result.output.lower() or "No active session" in result.output

    def test_auth_logout_partial_state_only_storage(self, runner, tmp_path, mock_context_file):
        """Test auth logout handles case where only storage_state.json exists."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # browser_dir does not exist

        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "Logged out" in result.output
        assert not storage_file.exists()

    def test_auth_logout_handles_permission_error_on_rmtree(
        self, runner, tmp_path, mock_context_file
    ):
        """Test auth logout handles locked browser profile gracefully."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        browser_dir.mkdir()

        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch(
                "notebooklm.cli.session.shutil.rmtree",
                side_effect=OSError("sharing violation"),
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 1
        assert "in use" in result.output.lower() or "Cannot" in result.output

    def test_auth_logout_handles_permission_error_on_unlink(
        self, runner, tmp_path, mock_context_file
    ):
        """Test auth logout handles locked storage_state.json gracefully on Windows."""
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # No browser dir

        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch.object(
                type(storage_file),
                "unlink",
                side_effect=OSError("file in use"),
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 1
        assert "Cannot" in result.output or "in use" in result.output.lower()

    def test_auth_logout_clears_cached_notebook_context(self, runner, tmp_path, mock_context_file):
        """Logout must remove context.json so the next command does not reuse
        notebook_id / conversation_id from the previous account.

        Issues #114 / #294 surfaced as "not found" / permission errors after an
        account switch. The PR's account-mismatch hint steers users to
        logout→login as the fix; the flow only works if context is actually
        cleared on logout.
        """
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        browser_dir.mkdir()

        # Simulate cached notebook / conversation from a previous session.
        mock_context_file.write_text(
            json.dumps(
                {
                    "notebook_id": "old-account-notebook",
                    "conversation_id": "old-account-conversation",
                }
            )
        )
        assert mock_context_file.exists()

        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "Logged out" in result.output
        assert not mock_context_file.exists()

    def test_auth_logout_no_context_file_does_not_error(self, runner, tmp_path, mock_context_file):
        """Logout must tolerate a missing context.json without erroring.

        clear_context() is a no-op when the file does not exist; assert that
        the main logout path still succeeds.
        """
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # No context file, no browser dir.

        assert not mock_context_file.exists()

        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 0
        assert "Logged out" in result.output

    def test_auth_logout_handles_os_error_on_context_unlink(
        self, runner, tmp_path, mock_context_file
    ):
        """Logout must surface an OSError on context.json removal as SystemExit(1).

        Parity with the existing handlers for storage_state.json and the browser
        profile: a locked/unwritable context file should produce a clean
        diagnostic message, not an unhandled traceback.
        """
        storage_file = tmp_path / "storage.json"
        storage_file.write_text('{"cookies": []}')
        browser_dir = tmp_path / "browser_profile"
        # No browser dir — nothing to remove in that step.
        mock_context_file.write_text('{"notebook_id": "stale"}')

        with (
            patch_session_login_dual("get_storage_path", return_value=storage_file),
            patch(
                "notebooklm.cli.session.get_browser_profile_dir",
                return_value=browser_dir,
            ),
            patch(
                "notebooklm.cli.session.clear_context",
                side_effect=OSError("file in use"),
            ),
        ):
            result = runner.invoke(cli, ["auth", "logout"])

        assert result.exit_code == 1
        assert "context file" in result.output.lower()


# =============================================================================
# AUTH REFRESH COMMAND TESTS
# =============================================================================


class TestAuthRefreshCommand:
    """Tests for the 'auth refresh' one-shot keepalive command."""

    @pytest.fixture
    def mock_storage_path(self, tmp_path):
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )
        with patch_session_login_dual("get_storage_path", return_value=storage_file):
            yield storage_file

    def test_auth_refresh_success(self, runner, mock_storage_path):
        """auth refresh exits 0 and prints `ok` on a successful token fetch."""
        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh"])
        assert result.exit_code == 0
        assert "ok" in result.output.lower()
        mock_fetch.assert_awaited_once()

    def test_auth_refresh_quiet_suppresses_success_output(self, runner, mock_storage_path):
        """--quiet keeps stdout clean when refresh succeeds (cron-friendly)."""
        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["auth", "refresh", "--quiet"])
        assert result.exit_code == 0
        assert result.output.strip() == ""

    def test_auth_refresh_failure_exits_nonzero(self, runner, mock_storage_path):
        """Token fetch failure exits non-zero with a friendly message — picked
        up by cron logs.

        The command body is wrapped in ``handle_errors``, so an
        unexpected ``ValueError`` flows through the UNEXPECTED_ERROR branch
        (exit 2) and the user sees a friendly 'Unexpected error: <msg>' line
        rather than a Python traceback.
        """
        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = ValueError("Authentication expired or invalid.")
            result = runner.invoke(cli, ["auth", "refresh"])
        # Exit code 2 per error_handler.py policy for unexpected errors.
        assert result.exit_code == 2
        # The original message is still surfaced verbatim, so cron logs keep
        # the diagnostic content.
        assert "authentication expired" in result.output.lower()
        # No Python traceback in stdout/stderr.
        assert "Traceback (most recent call last)" not in result.output

    def test_auth_refresh_failure_does_not_print_exception_class(self, runner, mock_storage_path):
        """``auth refresh`` no longer leaks ``type(exc).__name__`` into the
        user-facing message. The previous code path produced
        ``Error: ConnectTimeout: `` (with class name), which is implementation
        detail leakage. ``handle_errors`` produces ``Unexpected error: <msg>``
        instead.

        Regression guard for the error-handler polish.
        """
        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = httpx.ConnectTimeout("")  # empty message
            result = runner.invoke(cli, ["auth", "refresh"])
        # Non-zero exit, friendly handler, no traceback.
        assert result.exit_code == 2
        assert "Traceback (most recent call last)" not in result.output
        # Critical: no ``ConnectTimeout`` class name in output.
        assert "ConnectTimeout" not in result.output, (
            f"auth refresh must not leak exception class names; got: {result.output!r}"
        )
        # And no ``Error: <ClassName>:`` leak pattern from the old code path.
        assert "Error: ConnectTimeout" not in result.output
        # A friendly Unexpected-error line should still appear.
        assert "Unexpected error" in result.output

    def test_auth_refresh_browser_cookies_failure_uses_typed_handler(
        self, runner, mock_storage_path
    ):
        """The ``--browser-cookies`` failure path also flows through
        ``handle_errors`` — same polish guarantee as the keepalive path.

        Previously the browser-cookies branch had its own bespoke
        ``except Exception: click.echo(f"Error: {type(exc).__name__}: ...")``
        block; it now relies on the wrapping ``with handle_errors():``.
        """
        with patch_session_login_dual("_refresh_from_browser_cookies") as mock_refresh:
            mock_refresh.side_effect = RuntimeError("rookiepy could not read cookies")
            result = runner.invoke(cli, ["auth", "refresh", "--browser-cookies", "chrome"])
        assert result.exit_code == 2  # unexpected error per error_handler policy
        assert "Traceback (most recent call last)" not in result.output
        # No leaked ``RuntimeError`` class name.
        assert "RuntimeError" not in result.output
        assert "Error: RuntimeError" not in result.output
        # Friendly Unexpected-error message + the original detail.
        assert "Unexpected error" in result.output
        assert "rookiepy could not read cookies" in result.output

    def test_auth_refresh_rejects_env_var_auth(self, runner, monkeypatch, mock_storage_path):
        """NOTEBOOKLM_AUTH_JSON has no writable backing store; refreshing it
        would silently rotate SIDTS but persist nothing. Refuse loudly."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[]}')
        with patch(
            "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            result = runner.invoke(cli, ["auth", "refresh"])
        assert result.exit_code == 1
        assert "NOTEBOOKLM_AUTH_JSON" in result.output
        assert "incompatible" in result.output.lower()
        # Critical: no token fetch should run when the env var is set —
        # otherwise we'd be doing a server-side rotation that gets lost.
        mock_fetch.assert_not_awaited()

    def test_auth_refresh_propagates_global_profile_flag(self, runner, tmp_path):
        """`notebooklm --profile work auth refresh` resolves the work profile.

        Guards against the launchd/cron case where the global -p flag must
        flow through ctx.obj into fetch_tokens_with_domains.
        """
        work_storage = tmp_path / "work_storage_state.json"
        work_storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "y", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )

        def fake_storage_path(profile=None):
            assert profile == "work", f"expected profile='work', got {profile!r}"
            return work_storage

        with (
            patch_session_login_dual("get_storage_path", side_effect=fake_storage_path),
            patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
        ):
            mock_fetch.return_value = ("csrf_ok", "session_ok")
            result = runner.invoke(cli, ["--profile", "work", "auth", "refresh"])

        assert result.exit_code == 0, result.output
        # fetch_tokens_with_domains(path, profile) — verify the work profile
        # was threaded through to the auth layer.
        called_args = mock_fetch.call_args
        assert called_args.args[0] == work_storage
        assert called_args.args[1] == "work"

    def test_auth_refresh_browser_cookies_repairs_account_after_order_change(
        self, runner, tmp_path
    ):
        """If a browser account logs out and indices shift, match by email and
        rewrite context.json with the new internal account index."""
        storage = tmp_path / "profiles" / "bob" / "storage_state.json"
        storage.parent.mkdir(parents=True)
        storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")
        (storage.parent / "context.json").write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@gmail.com"}}),
            encoding="utf-8",
        )
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="bob@gmail.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual("get_storage_path", return_value=storage),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
                return_value=("csrf_ok", "session_ok"),
            ) as mock_fetch,
        ):
            result = runner.invoke(cli, ["auth", "refresh", "--browser-cookies", "chrome"])

        assert result.exit_code == 0, result.output
        assert "bob@gmail.com" in result.output
        assert "authuser" not in result.output
        assert _read_account(storage) == {
            "authuser": 0,
            "email": "bob@gmail.com",
        }
        mock_fetch.assert_awaited_once()

    def test_auth_refresh_browser_cookies_fails_when_profile_email_signed_out(
        self, runner, tmp_path
    ):
        """A stored email is identity; if that account is absent from the browser,
        do not refresh the profile with a different signed-in account."""
        storage = tmp_path / "profiles" / "bob" / "storage_state.json"
        storage.parent.mkdir(parents=True)
        storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")
        (storage.parent / "context.json").write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@gmail.com"}}),
            encoding="utf-8",
        )
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual("get_storage_path", return_value=storage),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
            patch_session_login_dual(
                "fetch_tokens_with_domains",
                new_callable=AsyncMock,
            ) as mock_fetch,
        ):
            result = runner.invoke(cli, ["auth", "refresh", "--browser-cookies", "chrome"])

        assert result.exit_code == 1
        assert "bob@gmail.com" in result.output
        assert "not signed in" in result.output.lower()
        assert "alice@example.com" in result.output
        # In this test the storage file was pre-seeded with a sibling
        # context.json (legacy layout). The reader falls back to that record
        # because no in-band write has occurred — assertion stays unchanged.
        assert _read_account(storage) == {
            "authuser": 1,
            "email": "bob@gmail.com",
        }
        mock_fetch.assert_not_awaited()


# =============================================================================
# AUTH INSPECT + MULTI-ACCOUNT LOGIN TESTS (issue #359)
# =============================================================================


class TestAuthInspect:
    def test_session_run_async_patch_reaches_login_service_helper(self):
        from notebooklm.auth import Account
        from notebooklm.cli.session import _enumerate_one_jar

        raw_cookies = _multiaccount_rookiepy_mock().chrome.return_value
        accounts = [Account(authuser=0, email="alice@example.com", is_default=True)]

        def fake_run_async(awaitable):
            awaitable.close()
            return accounts

        with (
            patch("notebooklm.auth.enumerate_accounts", return_value=object()),
            patch_session_login_dual("run_async", side_effect=fake_run_async) as mock_run_async,
        ):
            result = _enumerate_one_jar(raw_cookies, "chrome", browser_profile=None)

        assert result == accounts
        mock_run_async.assert_called_once()

    def test_select_account_without_marked_default_uses_first_account(self):
        from notebooklm.auth import Account
        from notebooklm.cli.session import _select_account

        accounts = [
            Account(authuser=0, email="alice@example.com", is_default=False),
            Account(authuser=1, email="bob@gmail.com", is_default=False),
        ]

        with patch_session_login_dual("console") as mock_console:
            selected = _select_account(accounts, account_email=None)

        assert selected == accounts[0]
        warning_text = mock_console.print.call_args[0][0]
        assert "default account" in warning_text
        assert "alice@example.com" in warning_text

    def test_inspect_lists_accounts(self, runner):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [
                Account(authuser=0, email="alice@example.com", is_default=True),
                Account(authuser=1, email="bob@gmail.com", is_default=False),
                Account(authuser=2, email="carol@ws.com", is_default=False),
            ]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch_session_login_dual("run_async", side_effect=asyncio.run),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome"])
        assert result.exit_code == 0, result.output
        assert "alice@example.com" in result.output
        assert "bob@gmail.com" in result.output
        assert "carol@ws.com" in result.output
        assert "authuser" not in result.output

    def test_inspect_json_output(self, runner):
        mock_rk = _multiaccount_rookiepy_mock()

        async def _enum(*args, **kwargs):
            from notebooklm.auth import Account

            return [Account(authuser=0, email="alice@example.com", is_default=True)]

        with (
            patch.dict("sys.modules", {"rookiepy": mock_rk}),
            patch("notebooklm.auth.enumerate_accounts", new=_enum),
        ):
            result = runner.invoke(cli, ["auth", "inspect", "--browser", "chrome", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["accounts"][0]["email"] == "alice@example.com"
        assert "authuser" not in data["accounts"][0]
        assert data["accounts"][0]["is_default"] is True
