"""Tests for authentication module."""

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm import auth as auth_module
from notebooklm.auth import (
    KEEPALIVE_ROTATE_URL,
    NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV,
    Account,
    AuthTokens,
    build_httpx_cookies_from_storage,
    convert_rookiepy_cookies_to_storage_state,
    enumerate_accounts,
    extract_cookies_from_storage,
    extract_cookies_with_domains,
    extract_csrf_from_html,
    extract_email_from_html,
    extract_session_id_from_html,
    fetch_tokens,
    fetch_tokens_with_domains,
    load_auth_from_storage,
    load_httpx_cookies,
    save_cookies_to_storage,
    snapshot_cookie_jar,
)


class TestAuthTokens:
    def test_dataclass_fields(self):
        """Test AuthTokens has required fields."""
        tokens = AuthTokens(
            cookies={"SID": "abc", "__Secure-1PSIDTS": "test_1psidts", "HSID": "def"},
            csrf_token="csrf123",
            session_id="sess456",
        )
        assert tokens.cookies == {
            ("SID", ".google.com", "/"): "abc",
            ("__Secure-1PSIDTS", ".google.com", "/"): "test_1psidts",
            ("HSID", ".google.com", "/"): "def",
        }
        assert tokens.flat_cookies == {
            "SID": "abc",
            "__Secure-1PSIDTS": "test_1psidts",
            "HSID": "def",
        }
        assert tokens.csrf_token == "csrf123"
        assert tokens.session_id == "sess456"

    def test_cookie_header(self):
        """Test generating cookie header string."""
        tokens = AuthTokens(
            cookies={"SID": "abc", "__Secure-1PSIDTS": "test_1psidts", "HSID": "def"},
            csrf_token="csrf123",
            session_id="sess456",
        )
        header = tokens.cookie_header
        assert "SID=abc" in header
        assert "__Secure-1PSIDTS=test_1psidts" in header
        assert "HSID=def" in header

    def test_cookie_header_format(self):
        """Test cookie header uses semicolon separator."""
        tokens = AuthTokens(
            cookies={"A": "1", "B": "2"},
            csrf_token="x",
            session_id="y",
        )
        header = tokens.cookie_header
        assert "; " in header


class TestExtractCookies:
    def test_extracts_all_google_domain_cookies(self):
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_value", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "hsid_value", "domain": ".google.com"},
                {
                    "name": "__Secure-1PSID",
                    "value": "secure_value",
                    "domain": ".google.com",
                },
                {
                    "name": "OSID",
                    "value": "osid_value",
                    "domain": "notebooklm.google.com",
                },
                {"name": "OTHER", "value": "other_value", "domain": "other.com"},
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)

        assert cookies["SID"] == "sid_value"
        assert cookies["HSID"] == "hsid_value"
        assert cookies["__Secure-1PSID"] == "secure_value"
        assert cookies["OSID"] == "osid_value"
        assert "OTHER" not in cookies

    def test_extracts_osid_from_notebooklm_subdomain(self):
        """Test OSID extraction from .notebooklm.google.com (Issue #329)."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_value", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {
                    "name": "OSID",
                    "value": "osid_subdomain",
                    "domain": ".notebooklm.google.com",
                },
                {
                    "name": "__Secure-OSID",
                    "value": "secure_osid_subdomain",
                    "domain": ".notebooklm.google.com",
                },
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)

        assert cookies["SID"] == "sid_value"
        assert cookies["OSID"] == "osid_subdomain"
        assert cookies["__Secure-OSID"] == "secure_osid_subdomain"

    def test_prefers_base_domain_cookie_over_notebooklm_subdomain(self):
        """Test .google.com still wins duplicate names from NotebookLM subdomain."""
        storage_state = {
            "cookies": [
                {
                    "name": "OSID",
                    "value": "osid_subdomain",
                    "domain": ".notebooklm.google.com",
                },
                {"name": "SID", "value": "sid_value", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "OSID", "value": "osid_base", "domain": ".google.com"},
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)

        assert cookies["SID"] == "sid_value"
        assert cookies["OSID"] == "osid_base"

    @pytest.mark.parametrize(
        "notebooklm_domain", [".notebooklm.google.com", "notebooklm.google.com"]
    )
    def test_prefers_notebooklm_subdomain_cookie_over_regional(self, notebooklm_domain):
        """Both NotebookLM subdomain forms win duplicate names from regional domains."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_value", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "OSID", "value": "osid_regional", "domain": ".google.de"},
                {"name": "OSID", "value": "osid_subdomain", "domain": notebooklm_domain},
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)

        assert cookies["SID"] == "sid_value"
        assert cookies["OSID"] == "osid_subdomain"

    def test_prefers_dotted_notebooklm_over_no_dot_variant(self):
        """Playwright canonical (.notebooklm.google.com) wins over the no-dot form."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_value", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "OSID", "value": "osid_no_dot", "domain": "notebooklm.google.com"},
                {"name": "OSID", "value": "osid_dotted", "domain": ".notebooklm.google.com"},
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)

        assert cookies["OSID"] == "osid_dotted"

        # Reverse list order — dotted variant should still win deterministically.
        storage_state["cookies"][1], storage_state["cookies"][2] = (
            storage_state["cookies"][2],
            storage_state["cookies"][1],
        )
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["OSID"] == "osid_dotted"

    def test_prefers_regional_over_googleusercontent(self):
        """Regional Google cookies win over .googleusercontent.com (priority 0)."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_value", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "X", "value": "x_uc", "domain": ".googleusercontent.com"},
                {"name": "X", "value": "x_regional", "domain": ".google.de"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["X"] == "x_regional"

        # Reverse order — regional should still win.
        storage_state["cookies"][1], storage_state["cookies"][2] = (
            storage_state["cookies"][2],
            storage_state["cookies"][1],
        )
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["X"] == "x_regional"

    def test_first_google_com_duplicate_wins(self):
        """Within the .google.com tier, the first occurrence wins; later duplicates are ignored."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "first", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "SID", "value": "second", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "first"

    def test_raises_if_missing_sid(self):
        storage_state = {
            "cookies": [
                {"name": "HSID", "value": "hsid_value", "domain": ".google.com"},
            ]
        }

        with pytest.raises(ValueError, match="Missing required cookies"):
            extract_cookies_from_storage(storage_state)

    def test_handles_empty_cookies_list(self):
        """Test handles empty cookies list."""
        storage_state = {"cookies": []}

        with pytest.raises(ValueError, match="Missing required cookies"):
            extract_cookies_from_storage(storage_state)

    def test_handles_missing_cookies_key(self):
        """Test handles missing cookies key."""
        storage_state = {}

        with pytest.raises(ValueError, match="Missing required cookies"):
            extract_cookies_from_storage(storage_state)


class TestExtractCSRF:
    def test_extracts_csrf_token(self):
        """Test extracting SNlM0e CSRF token from HTML."""
        html = """
        <script>window.WIZ_global_data = {
            "SNlM0e": "AF1_QpN-xyz123",
            "other": "value"
        }</script>
        """

        csrf = extract_csrf_from_html(html)
        assert csrf == "AF1_QpN-xyz123"

    def test_extracts_csrf_with_special_chars(self):
        """Test extracting CSRF token with special characters."""
        html = '"SNlM0e":"AF1_QpN-abc_123/def"'

        csrf = extract_csrf_from_html(html)
        assert csrf == "AF1_QpN-abc_123/def"

    def test_raises_if_not_found(self):
        """Test raises error if CSRF token not found."""
        html = "<html><body>No token here</body></html>"

        with pytest.raises(ValueError, match="CSRF token not found"):
            extract_csrf_from_html(html)

    def test_handles_empty_html(self):
        """Test handles empty HTML."""
        with pytest.raises(ValueError, match="CSRF token not found"):
            extract_csrf_from_html("")


class TestExtractSessionId:
    def test_extracts_session_id(self):
        """Test extracting FdrFJe session ID from HTML."""
        html = """
        <script>window.WIZ_global_data = {
            "FdrFJe": "session_id_abc",
            "other": "value"
        }</script>
        """

        session_id = extract_session_id_from_html(html)
        assert session_id == "session_id_abc"

    def test_extracts_numeric_session_id(self):
        """Test extracting numeric session ID."""
        html = '"FdrFJe":"1234567890123456"'

        session_id = extract_session_id_from_html(html)
        assert session_id == "1234567890123456"

    def test_raises_if_not_found(self):
        """Test raises error if session ID not found."""
        html = "<html><body>No session here</body></html>"

        with pytest.raises(ValueError, match="Session ID not found"):
            extract_session_id_from_html(html)


class TestLoadAuthFromStorage:
    def test_loads_from_file(self, tmp_path):
        """Test loading auth from storage state file."""
        storage_file = tmp_path / "storage_state.json"
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "ssid", "domain": ".google.com"},
                {"name": "APISID", "value": "apisid", "domain": ".google.com"},
                {"name": "SAPISID", "value": "sapisid", "domain": ".google.com"},
            ]
        }
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_auth_from_storage(storage_file)

        assert cookies["SID"] == "sid"
        assert len(cookies) == 6

    def test_raises_if_file_not_found(self, tmp_path):
        """Test raises error if storage file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            load_auth_from_storage(tmp_path / "nonexistent.json")

    def test_raises_if_invalid_json(self, tmp_path):
        """Test raises error if file contains invalid JSON."""
        storage_file = tmp_path / "invalid.json"
        storage_file.write_text("not valid json")

        with pytest.raises(json.JSONDecodeError):
            load_auth_from_storage(storage_file)


class TestLoadAuthFromEnvVar:
    """Test NOTEBOOKLM_AUTH_JSON env var support."""

    def test_loads_from_env_var(self, tmp_path, monkeypatch):
        """Test loading auth from NOTEBOOKLM_AUTH_JSON env var."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_from_env", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "hsid_from_env", "domain": ".google.com"},
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(storage_state))

        cookies = load_auth_from_storage()

        assert cookies["SID"] == "sid_from_env"
        assert cookies["HSID"] == "hsid_from_env"

    def test_explicit_path_takes_precedence_over_env_var(self, tmp_path, monkeypatch):
        """Test that explicit path argument overrides NOTEBOOKLM_AUTH_JSON."""
        # Set env var
        env_storage = {
            "cookies": [
                {"name": "SID", "value": "from_env", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(env_storage))

        # Create file with different value
        file_storage = {
            "cookies": [
                {"name": "SID", "value": "from_file", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(json.dumps(file_storage))

        # Explicit path should win
        cookies = load_auth_from_storage(storage_file)
        assert cookies["SID"] == "from_file"

    def test_env_var_invalid_json_raises_value_error(self, monkeypatch):
        """Test that invalid JSON in env var raises ValueError."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "not valid json")

        with pytest.raises(ValueError, match="Invalid JSON in NOTEBOOKLM_AUTH_JSON"):
            load_auth_from_storage()

    def test_env_var_missing_cookies_raises_value_error(self, monkeypatch):
        """Test that missing required cookies raises ValueError."""
        storage_state = {"cookies": []}  # No SID cookie
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(storage_state))

        with pytest.raises(ValueError, match="Missing required cookies"):
            load_auth_from_storage()

    def test_env_var_takes_precedence_over_file(self, tmp_path, monkeypatch):
        """Test that NOTEBOOKLM_AUTH_JSON takes precedence over default file."""
        # Set env var
        env_storage = {
            "cookies": [
                {"name": "SID", "value": "from_env", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(env_storage))

        # Set NOTEBOOKLM_HOME to tmp_path and create a file there
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        file_storage = {
            "cookies": [
                {"name": "SID", "value": "from_home_file", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(json.dumps(file_storage))

        # Env var should win over file (no explicit path)
        cookies = load_auth_from_storage()
        assert cookies["SID"] == "from_env"

    def test_env_var_empty_string_raises_value_error(self, monkeypatch):
        """Test that empty string NOTEBOOKLM_AUTH_JSON raises ValueError."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "")

        with pytest.raises(
            ValueError, match="NOTEBOOKLM_AUTH_JSON environment variable is set but empty"
        ):
            load_auth_from_storage()

    def test_env_var_whitespace_only_raises_value_error(self, monkeypatch):
        """Test that whitespace-only NOTEBOOKLM_AUTH_JSON raises ValueError."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "   \n\t  ")

        with pytest.raises(
            ValueError, match="NOTEBOOKLM_AUTH_JSON environment variable is set but empty"
        ):
            load_auth_from_storage()

    def test_env_var_missing_cookies_key_raises_value_error(self, monkeypatch):
        """Test that NOTEBOOKLM_AUTH_JSON without 'cookies' key raises ValueError."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"origins": []}')

        with pytest.raises(
            ValueError, match="must contain valid Playwright storage state with a 'cookies' key"
        ):
            load_auth_from_storage()

    def test_env_var_non_dict_raises_value_error(self, monkeypatch):
        """Test that non-dict NOTEBOOKLM_AUTH_JSON raises ValueError."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '["not", "a", "dict"]')

        with pytest.raises(
            ValueError, match="must contain valid Playwright storage state with a 'cookies' key"
        ):
            load_auth_from_storage()


class TestLoadHttpxCookiesWithEnvVar:
    """Test load_httpx_cookies with NOTEBOOKLM_AUTH_JSON env var."""

    def test_loads_cookies_from_env_var(self, monkeypatch):
        """Test loading httpx cookies from NOTEBOOKLM_AUTH_JSON env var."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_val", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "hsid_val", "domain": ".google.com"},
                {"name": "SSID", "value": "ssid_val", "domain": ".google.com"},
                {"name": "APISID", "value": "apisid_val", "domain": ".google.com"},
                {"name": "SAPISID", "value": "sapisid_val", "domain": ".google.com"},
                {"name": "__Secure-1PSID", "value": "psid1_val", "domain": ".google.com"},
                {"name": "__Secure-3PSID", "value": "psid3_val", "domain": ".google.com"},
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(storage_state))

        cookies = load_httpx_cookies()

        # Verify cookies were loaded
        assert cookies.get("SID", domain=".google.com") == "sid_val"
        assert cookies.get("HSID", domain=".google.com") == "hsid_val"
        assert cookies.get("__Secure-1PSID", domain=".google.com") == "psid1_val"

    def test_env_var_invalid_json_raises(self, monkeypatch):
        """Test that invalid JSON in NOTEBOOKLM_AUTH_JSON raises ValueError."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "not valid json")

        with pytest.raises(ValueError, match="Invalid JSON in NOTEBOOKLM_AUTH_JSON"):
            load_httpx_cookies()

    def test_env_var_empty_string_raises(self, monkeypatch):
        """Test that empty string NOTEBOOKLM_AUTH_JSON raises ValueError."""
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", "")

        with pytest.raises(
            ValueError, match="NOTEBOOKLM_AUTH_JSON environment variable is set but empty"
        ):
            load_httpx_cookies()

    def test_env_var_missing_required_cookies_raises(self, monkeypatch):
        """Test that missing required cookies raises ValueError."""
        storage_state = {
            "cookies": [
                # SID is the minimum required cookie - omitting it should raise
                {"name": "HSID", "value": "hsid_val", "domain": ".google.com"},
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(storage_state))

        with pytest.raises(ValueError, match="Missing required cookies for downloads"):
            load_httpx_cookies()

    def test_env_var_filters_non_google_domains(self, monkeypatch):
        """Test that cookies from non-Google domains are filtered out."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_val", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "hsid_val", "domain": ".google.com"},
                {"name": "SSID", "value": "ssid_val", "domain": ".google.com"},
                {"name": "APISID", "value": "apisid_val", "domain": ".google.com"},
                {"name": "SAPISID", "value": "sapisid_val", "domain": ".google.com"},
                {"name": "__Secure-1PSID", "value": "psid1_val", "domain": ".google.com"},
                {"name": "__Secure-3PSID", "value": "psid3_val", "domain": ".google.com"},
                {"name": "evil_cookie", "value": "evil_val", "domain": ".evil.com"},
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(storage_state))

        cookies = load_httpx_cookies()

        # Google cookies should be present
        assert cookies.get("SID", domain=".google.com") == "sid_val"
        # Non-Google cookies should be filtered out
        assert cookies.get("evil_cookie", domain=".evil.com") is None

    def test_env_var_missing_cookies_key_raises(self, monkeypatch):
        """Test that storage state without cookies key raises ValueError."""
        storage_state = {"origins": []}  # Valid JSON but no cookies key
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(storage_state))

        with pytest.raises(ValueError, match="must contain valid Playwright storage state"):
            load_httpx_cookies()

    def test_env_var_malformed_cookie_objects_skipped(self, monkeypatch):
        """Test that malformed cookie objects are skipped gracefully."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_val", "domain": ".google.com"},
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "test_1psidts",
                    "domain": ".google.com",
                },  # Valid
                {"name": "HSID"},  # Missing value and domain - should be skipped
                {"value": "val"},  # Missing name - should be skipped
                {},  # Empty object - should be skipped
                {"name": "", "value": "val", "domain": ".google.com"},  # Empty name - skipped
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(storage_state))

        # Should load successfully but only include valid SID cookie
        cookies = load_httpx_cookies()
        assert cookies.get("SID", domain=".google.com") == "sid_val"

    def test_explicit_path_overrides_env_var(self, tmp_path, monkeypatch):
        """Test that explicit path argument takes precedence over NOTEBOOKLM_AUTH_JSON."""
        # Set env var with one value
        env_storage = {
            "cookies": [
                {"name": "SID", "value": "from_env", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(env_storage))

        # Create file with different value
        file_storage = {
            "cookies": [
                {"name": "SID", "value": "from_file", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(json.dumps(file_storage))

        # Explicit path should win
        cookies = load_httpx_cookies(path=storage_file)
        assert cookies.get("SID", domain=".google.com") == "from_file"


class TestCookieAttributePreservation:
    """Round-trip preservation of path, secure, and httpOnly across load+save (#365)."""

    @staticmethod
    def _find_cookie(jar, name, domain, path=None):
        for cookie in jar.jar:
            if cookie.name == name and cookie.domain == domain:
                if path is None or cookie.path == path:
                    return cookie
        raise AssertionError(f"cookie {name}@{domain} (path={path}) not in jar")

    def _attr_storage_state(self):
        """Storage state with explicit non-default attributes on every cookie."""
        return {
            "cookies": [
                {
                    "name": "SID",
                    "value": "sid-value",
                    "domain": ".google.com",
                    "path": "/u/0/",
                    "expires": 1893456000,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                },
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "test_1psidts",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": 1893456000,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                },
                {
                    "name": "__Host-GAPS",
                    "value": "host-only-value",
                    "domain": "accounts.google.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Strict",
                },
            ]
        }

    def test_load_httpx_cookies_preserves_attributes(self, tmp_path):
        """``load_httpx_cookies`` should carry path/secure/httpOnly into the jar."""
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(json.dumps(self._attr_storage_state()))

        jar = load_httpx_cookies(path=storage_file)

        sid = self._find_cookie(jar, "SID", ".google.com")
        assert sid.path == "/u/0/"
        assert sid.secure is True
        assert sid.has_nonstandard_attr("HttpOnly")

        gaps = self._find_cookie(jar, "__Host-GAPS", "accounts.google.com")
        assert gaps.path == "/"
        assert gaps.secure is True
        assert gaps.has_nonstandard_attr("HttpOnly")

    def test_build_httpx_cookies_from_storage_preserves_attributes(self, tmp_path):
        """``build_httpx_cookies_from_storage`` should preserve the same attrs."""
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(json.dumps(self._attr_storage_state()))

        jar = build_httpx_cookies_from_storage(storage_file)

        sid = self._find_cookie(jar, "SID", ".google.com")
        assert sid.path == "/u/0/"
        assert sid.secure is True
        assert sid.has_nonstandard_attr("HttpOnly")

        gaps = self._find_cookie(jar, "__Host-GAPS", "accounts.google.com")
        assert gaps.path == "/"
        assert gaps.secure is True
        assert gaps.has_nonstandard_attr("HttpOnly")

    def test_round_trip_with_value_change_preserves_attributes(self, tmp_path):
        """Load → bump value → save → reload preserves path/secure/httpOnly.

        Mutating the value forces ``save_cookies_to_storage`` into the
        "changed" branch that overwrites stored attrs from the live jar — the
        path that previously eroded attributes to defaults.
        """
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(json.dumps(self._attr_storage_state()))

        jar = build_httpx_cookies_from_storage(storage_file)
        snapshot = snapshot_cookie_jar(jar)
        for cookie in jar.jar:
            if cookie.name == "SID":
                cookie.value = "rotated-sid"
        save_cookies_to_storage(jar, storage_file, original_snapshot=snapshot)

        on_disk = json.loads(storage_file.read_text())
        sid_entry = next(c for c in on_disk["cookies"] if c["name"] == "SID")
        assert sid_entry["path"] == "/u/0/"
        assert sid_entry["secure"] is True
        assert sid_entry["httpOnly"] is True

        gaps_entry = next(c for c in on_disk["cookies"] if c["name"] == "__Host-GAPS")
        assert gaps_entry["path"] == "/"
        assert gaps_entry["secure"] is True
        assert gaps_entry["httpOnly"] is True

    def test_round_trip_without_value_change_preserves_attributes(self, tmp_path):
        """Load → save (no mutation) → reload preserves attrs.

        This is the silent-erosion path users hit on idle calls: nothing
        changes, but the save side appends fresh entries from the in-memory
        jar (auth.py:1095). Without the load-side fix, those appended entries
        would carry default ``path=/``, ``secure=False``, ``httpOnly=False``.
        """
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(json.dumps(self._attr_storage_state()))

        jar = build_httpx_cookies_from_storage(storage_file)
        save_cookies_to_storage(jar, storage_file, original_snapshot=snapshot_cookie_jar(jar))

        reloaded = build_httpx_cookies_from_storage(storage_file)
        sid = self._find_cookie(reloaded, "SID", ".google.com")
        assert sid.path == "/u/0/"
        assert sid.secure is True
        assert sid.has_nonstandard_attr("HttpOnly")

    def test_session_cookie_round_trips_as_minus_one(self, tmp_path):
        """Session cookies (expires=-1) survive without becoming a real timestamp."""
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(json.dumps(self._attr_storage_state()))

        jar = build_httpx_cookies_from_storage(storage_file)
        gaps = self._find_cookie(jar, "__Host-GAPS", "accounts.google.com")
        assert gaps.expires is None

        snapshot = snapshot_cookie_jar(jar)
        for cookie in jar.jar:
            if cookie.name == "__Host-GAPS":
                cookie.value = "rotated-gaps"
        save_cookies_to_storage(jar, storage_file, original_snapshot=snapshot)

        on_disk = json.loads(storage_file.read_text())
        gaps_entry = next(c for c in on_disk["cookies"] if c["name"] == "__Host-GAPS")
        assert gaps_entry["expires"] == -1

    def test_expires_zero_round_trips(self, tmp_path):
        """``expires=0`` (Unix epoch) is a legitimate timestamp, not a sentinel.

        Some Playwright variants emit ``0`` for cookies that expired at the
        epoch. The load helper must distinguish ``0`` from ``-1`` / ``None``.
        """
        state = {
            "cookies": [
                {
                    "name": "SID",
                    "value": "v",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": 0,
                    "httpOnly": True,
                    "secure": True,
                },
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "test_1psidts",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": 1893456000,
                    "httpOnly": True,
                    "secure": True,
                },
            ]
        }
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(json.dumps(state))

        jar = build_httpx_cookies_from_storage(storage_file)
        sid = self._find_cookie(jar, "SID", ".google.com")
        # 0 is preserved as 0 — not collapsed to None (session) or -1.
        assert sid.expires == 0


class TestExtractCSRFRedirect:
    """Test CSRF extraction redirect detection."""

    def test_raises_on_redirect_to_accounts_in_url(self):
        """Test raises error when redirected to accounts.google.com (URL)."""
        html = "<html><body>Login page</body></html>"
        final_url = "https://accounts.google.com/signin"

        with pytest.raises(ValueError, match="Authentication expired"):
            extract_csrf_from_html(html, final_url)

    def test_raises_on_redirect_to_accounts_in_html(self):
        """Test raises error when redirected to accounts.google.com (HTML content)."""
        html = '<html><body><a href="https://accounts.google.com/signin">Sign in</a></body></html>'

        with pytest.raises(ValueError, match="Authentication expired"):
            extract_csrf_from_html(html)


class TestExtractSessionIdRedirect:
    """Test session ID extraction redirect detection."""

    def test_raises_on_redirect_to_accounts_in_url(self):
        """Test raises error when redirected to accounts.google.com (URL)."""
        html = "<html><body>Login page</body></html>"
        final_url = "https://accounts.google.com/signin"

        with pytest.raises(ValueError, match="Authentication expired"):
            extract_session_id_from_html(html, final_url)

    def test_raises_on_redirect_to_accounts_in_html(self):
        """Test raises error when redirected to accounts.google.com (HTML content)."""
        html = '<html><body><a href="https://accounts.google.com/signin">Sign in</a></body></html>'

        with pytest.raises(ValueError, match="Authentication expired"):
            extract_session_id_from_html(html)


class TestExtractCookiesEdgeCases:
    """Test cookie extraction edge cases."""

    def test_skips_cookies_without_name(self):
        """Test skips cookies without a name field."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_value", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"value": "no_name_value", "domain": ".google.com"},  # Missing name
                {"name": "", "value": "empty_name", "domain": ".google.com"},  # Empty name
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)
        assert "SID" in cookies
        assert "__Secure-1PSIDTS" in cookies
        # SID + __Secure-1PSIDTS extracted; nameless and empty-name entries skipped
        assert len(cookies) == 2

    def test_handles_cookie_with_empty_value(self):
        """Test handles cookies with empty values."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == ""


class TestFetchTokens:
    """Test fetch_tokens function with mocked HTTP."""

    @pytest.mark.asyncio
    async def test_fetch_tokens_success(self, httpx_mock: HTTPXMock):
        """Test successful token fetch."""
        html = """
        <html>
        <script>
            window.WIZ_global_data = {
                "SNlM0e": "AF1_QpN-csrf_token_123",
                "FdrFJe": "session_id_456"
            };
        </script>
        </html>
        """
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=html.encode(),
        )

        cookies = {"SID": "test_sid", "__Secure-1PSIDTS": "test_1psidts"}
        csrf, session_id = await fetch_tokens(cookies)

        assert csrf == "AF1_QpN-csrf_token_123"
        assert session_id == "session_id_456"

    @pytest.mark.asyncio
    async def test_fetch_tokens_success_preserves_input_without_refresh(
        self, httpx_mock: HTTPXMock
    ):
        """Successful fetch without refresh does not rewrite caller cookies."""
        html = '"SNlM0e":"csrf_ok" "FdrFJe":"sess_ok"'
        httpx_mock.add_response(url="https://notebooklm.google.com/", content=html.encode())

        cookies = {("SID", ".google.com"): "test_sid", ("APP_COOKIE", "example.com"): "keep"}
        original = cookies.copy()

        csrf, session_id = await fetch_tokens(cookies)

        assert csrf == "csrf_ok"
        assert session_id == "sess_ok"
        assert cookies == original

    @pytest.mark.asyncio
    async def test_fetch_tokens_redirect_to_login(self, httpx_mock: HTTPXMock):
        """Test raises error when redirected to login page."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            status_code=302,
            headers={"Location": "https://accounts.google.com/signin"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/signin",
            content=b"<html>Login</html>",
        )

        cookies = {"SID": "expired_sid", "__Secure-1PSIDTS": "test_1psidts"}
        with pytest.raises(ValueError, match="Authentication expired"):
            await fetch_tokens(cookies)

    @pytest.mark.asyncio
    async def test_fetch_tokens_sends_cookies_on_account_redirect(self, httpx_mock: HTTPXMock):
        """Redirected accounts.google.com requests receive matching domain cookies."""
        html = '"SNlM0e":"csrf" "FdrFJe":"sess"'
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            status_code=302,
            headers={"Location": "https://accounts.google.com/start"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/start",
            status_code=302,
            headers={
                "Location": "https://accounts.google.com/continue",
                "Set-Cookie": "ACCOUNT_REFRESH=fresh; Domain=accounts.google.com; Path=/",
            },
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/continue",
            status_code=302,
            headers={"Location": "https://notebooklm.google.com/"},
        )
        httpx_mock.add_response(url="https://notebooklm.google.com/", content=html.encode())

        cookies = {
            ("SID", ".google.com"): "sid_value",
            ("ACCOUNT_CHOOSER", "accounts.google.com"): "chooser_value",
        }
        await fetch_tokens(cookies)

        account_requests = [
            request
            for request in httpx_mock.get_requests()
            if request.url.host == "accounts.google.com"
            and not request.url.path.startswith("/RotateCookies")
        ]
        assert len(account_requests) == 2

        first_cookie_header = account_requests[0].headers.get("cookie", "")
        assert "SID=sid_value" in first_cookie_header
        assert "ACCOUNT_CHOOSER=chooser_value" in first_cookie_header

        second_cookie_header = account_requests[1].headers.get("cookie", "")
        assert "ACCOUNT_REFRESH=fresh" in second_cookie_header

    @pytest.mark.asyncio
    async def test_fetch_tokens_with_domains_persists_refreshed_accounts_cookie(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """Refreshed accounts.google.com cookies are written back to storage."""
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "sid_value", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                        {
                            "name": "ACCOUNT_REFRESH",
                            "value": "stale",
                            "domain": "accounts.google.com",
                            "path": "/",
                            "expires": -1,
                            "httpOnly": True,
                            "secure": True,
                            "sameSite": "None",
                        },
                    ]
                }
            )
        )

        html = '"SNlM0e":"csrf" "FdrFJe":"sess"'
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            status_code=302,
            headers={"Location": "https://accounts.google.com/start"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/start",
            status_code=302,
            headers={
                "Location": "https://notebooklm.google.com/",
                "Set-Cookie": "ACCOUNT_REFRESH=fresh; Domain=accounts.google.com; Path=/",
            },
        )
        httpx_mock.add_response(url="https://notebooklm.google.com/", content=html.encode())

        await fetch_tokens_with_domains(storage_file)

        storage_state = json.loads(storage_file.read_text())
        account_cookie = next(
            cookie
            for cookie in storage_state["cookies"]
            if cookie["name"] == "ACCOUNT_REFRESH" and cookie["domain"] == "accounts.google.com"
        )
        assert account_cookie["value"] == "fresh"

    def test_appended_dot_accounts_cookie_round_trips(self, tmp_path):
        """New accounts.google.com cookies keep their normalized cookiejar domain."""
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "sid", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )

        jar = httpx.Cookies()
        empty_snapshot = snapshot_cookie_jar(jar)
        jar.set("SID", "sid", domain=".google.com")
        jar.set("ACCOUNT_REFRESH", "fresh", domain=".accounts.google.com")

        save_cookies_to_storage(jar, storage_file, original_snapshot=empty_snapshot)

        storage_state = json.loads(storage_file.read_text())
        assert (
            "ACCOUNT_REFRESH",
            ".accounts.google.com",
            "/",
        ) in extract_cookies_with_domains(storage_state)

    def test_save_cookies_to_storage_preserves_secure_permissions(self, tmp_path):
        """Cookie sync keeps storage_state.json at 0o600 on POSIX."""
        if os.name == "nt":
            pytest.skip("POSIX permission bits are not meaningful on Windows")

        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "SID",
                            "value": "old",
                            "domain": ".google.com",
                            "path": "/",
                            "httpOnly": True,
                            "secure": False,
                        },
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )
        storage_file.chmod(0o600)

        jar = httpx.Cookies()
        jar.set("SID", "old", domain=".google.com")
        snapshot = snapshot_cookie_jar(jar)
        jar.set("SID", "new", domain=".google.com")

        save_cookies_to_storage(jar, storage_file, original_snapshot=snapshot)

        assert storage_file.stat().st_mode & 0o777 == 0o600
        storage_state = json.loads(storage_file.read_text())
        assert storage_state["cookies"][0]["value"] == "new"


class TestFetchTokensAutoRefresh:
    """Test NOTEBOOKLM_REFRESH_CMD auto-refresh behavior in fetch_tokens."""

    @pytest.fixture(autouse=True)
    def _clear_refresh_flag(self, monkeypatch):
        # Ensure each test starts with no prior attempt flag
        monkeypatch.delenv("_NOTEBOOKLM_REFRESH_ATTEMPTED", raising=False)
        monkeypatch.delenv("NOTEBOOKLM_REFRESH_CMD", raising=False)

    @staticmethod
    def _python_refresh_cmd(script: Path) -> str:
        if os.name != "nt":
            return shlex.join([sys.executable, str(script)])
        return subprocess.list2cmdline([sys.executable, str(script)])

    @pytest.mark.asyncio
    async def test_no_refresh_when_env_unset(self, httpx_mock: HTTPXMock):
        """Auth error propagates unchanged when NOTEBOOKLM_REFRESH_CMD is not set."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            status_code=302,
            headers={"Location": "https://accounts.google.com/signin"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/signin",
            content=b"<html>Login</html>",
        )

        with pytest.raises(ValueError, match="Authentication expired"):
            await fetch_tokens({"SID": "stale", "__Secure-1PSIDTS": "test_1psidts"})

    @pytest.mark.asyncio
    async def test_refresh_retries_once_and_succeeds(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """On auth failure, runs refresh cmd, reloads cookies, retries successfully."""
        # Stage 1: write a stale cookie file
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "stale", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )
        monkeypatch.setattr("notebooklm.auth.get_storage_path", lambda profile=None: storage_file)

        # Refresh command rewrites the file with a fresh SID
        fresh_file = tmp_path / "fresh_cookies.json"
        fresh_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "fresh", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )
        refresh_script = tmp_path / "refresh.py"
        refresh_script.write_text(
            "\n".join(
                [
                    "import shutil",
                    f"shutil.copyfile({str(fresh_file)!r}, {str(storage_file)!r})",
                ]
            )
        )
        monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", self._python_refresh_cmd(refresh_script))

        # First HTTP call: auth redirect
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            status_code=302,
            headers={"Location": "https://accounts.google.com/signin"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/signin",
            content=b"<html>Login</html>",
        )
        # Second HTTP call (after refresh): success
        html = '"SNlM0e":"csrf_ok" "FdrFJe":"sess_ok"'
        httpx_mock.add_response(url="https://notebooklm.google.com/", content=html.encode())

        cookies = {"SID": "stale", "__Secure-1PSIDTS": "test_1psidts"}
        csrf, session_id = await fetch_tokens(cookies)

        assert csrf == "csrf_ok"
        assert session_id == "sess_ok"
        # Cookies dict was mutated in place with fresh values
        assert cookies["SID"] == "fresh"

    @pytest.mark.asyncio
    async def test_refresh_reloads_explicit_storage_path(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Refresh reloads from the caller's explicit storage path."""
        storage_file = tmp_path / "custom_storage_state.json"
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "stale", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )

        fresh_file = tmp_path / "fresh_cookies.json"
        fresh_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "fresh", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )
        refresh_script = tmp_path / "refresh.py"
        refresh_script.write_text(
            "\n".join(
                [
                    "import shutil",
                    f"shutil.copyfile({str(fresh_file)!r}, {str(storage_file)!r})",
                ]
            )
        )
        monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", self._python_refresh_cmd(refresh_script))

        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            status_code=302,
            headers={"Location": "https://accounts.google.com/signin"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/signin",
            content=b"<html>Login</html>",
        )
        html = '"SNlM0e":"csrf_ok" "FdrFJe":"sess_ok"'
        httpx_mock.add_response(url="https://notebooklm.google.com/", content=html.encode())

        cookies = {"SID": "stale", "__Secure-1PSIDTS": "test_1psidts"}
        csrf, session_id = await fetch_tokens(cookies, storage_file)

        assert csrf == "csrf_ok"
        assert session_id == "sess_ok"
        assert cookies["SID"] == "fresh"

    @pytest.mark.asyncio
    async def test_refresh_command_receives_profile_storage_path(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Profile-based auth exposes the profile storage path to refresh commands."""
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        storage_file = tmp_path / "profiles" / "work" / "storage_state.json"
        storage_file.parent.mkdir(parents=True)
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "stale", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )

        refresh_script = tmp_path / "refresh.py"
        refresh_script.write_text(
            "\n".join(
                [
                    "import json",
                    "import os",
                    "from pathlib import Path",
                    "assert os.environ['_NOTEBOOKLM_REFRESH_ATTEMPTED'] == '1'",
                    "assert os.environ['NOTEBOOKLM_REFRESH_PROFILE'] == 'work'",
                    "storage = Path(os.environ['NOTEBOOKLM_REFRESH_STORAGE_PATH'])",
                    f"assert storage == Path({str(storage_file)!r})",
                    "storage.write_text(json.dumps({'cookies': [",
                    "    {'name': 'SID', 'value': 'fresh', 'domain': '.google.com'},",
                    "    {'name': '__Secure-1PSIDTS', 'value': 'fresh_1psidts', 'domain': '.google.com'},",
                    "]}))",
                ]
            )
        )
        monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", self._python_refresh_cmd(refresh_script))

        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            status_code=302,
            headers={"Location": "https://accounts.google.com/signin"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/signin",
            content=b"<html>Login</html>",
        )
        html = '"SNlM0e":"csrf_ok" "FdrFJe":"sess_ok"'
        httpx_mock.add_response(url="https://notebooklm.google.com/", content=html.encode())

        tokens = await AuthTokens.from_storage(profile="work")

        assert tokens.flat_cookies["SID"] == "fresh"
        assert tokens.csrf_token == "csrf_ok"
        assert tokens.session_id == "sess_ok"
        assert "_NOTEBOOKLM_REFRESH_ATTEMPTED" not in os.environ

    @pytest.mark.asyncio
    async def test_fetch_tokens_with_profile_reloads_profile_storage_path(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """fetch_tokens(profile=...) reloads from that profile's storage after refresh."""
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        storage_file = tmp_path / "profiles" / "work" / "storage_state.json"
        storage_file.parent.mkdir(parents=True)
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "stale", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )

        refresh_script = tmp_path / "refresh.py"
        refresh_script.write_text(
            "\n".join(
                [
                    "import json",
                    "import os",
                    "from pathlib import Path",
                    "assert os.environ['_NOTEBOOKLM_REFRESH_ATTEMPTED'] == '1'",
                    "assert os.environ['NOTEBOOKLM_REFRESH_PROFILE'] == 'work'",
                    "storage = Path(os.environ['NOTEBOOKLM_REFRESH_STORAGE_PATH'])",
                    f"assert storage == Path({str(storage_file)!r})",
                    "storage.write_text(json.dumps({'cookies': [",
                    "    {'name': 'SID', 'value': 'fresh', 'domain': '.google.com'},",
                    "    {'name': '__Secure-1PSIDTS', 'value': 'fresh_1psidts', 'domain': '.google.com'},",
                    "]}))",
                ]
            )
        )
        monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", self._python_refresh_cmd(refresh_script))

        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            status_code=302,
            headers={"Location": "https://accounts.google.com/signin"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/signin",
            content=b"<html>Login</html>",
        )
        html = '"SNlM0e":"csrf_ok" "FdrFJe":"sess_ok"'
        httpx_mock.add_response(url="https://notebooklm.google.com/", content=html.encode())

        cookies = {"SID": "stale", "__Secure-1PSIDTS": "test_1psidts"}
        csrf, session_id = await fetch_tokens(cookies, profile="work")

        assert csrf == "csrf_ok"
        assert session_id == "sess_ok"
        assert cookies["SID"] == "fresh"
        assert "_NOTEBOOKLM_REFRESH_ATTEMPTED" not in os.environ

    @pytest.mark.asyncio
    async def test_fetch_tokens_with_domains_loads_profile_storage_path(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """fetch_tokens_with_domains(profile=...) loads that profile's storage."""
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        storage_file = tmp_path / "profiles" / "work" / "storage_state.json"
        storage_file.parent.mkdir(parents=True)
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "fresh", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )

        html = '"SNlM0e":"csrf_ok" "FdrFJe":"sess_ok"'
        httpx_mock.add_response(url="https://notebooklm.google.com/", content=html.encode())

        csrf, session_id = await fetch_tokens_with_domains(profile="work")

        assert csrf == "csrf_ok"
        assert session_id == "sess_ok"

    @pytest.mark.asyncio
    async def test_refresh_does_not_loop(self, tmp_path, monkeypatch, httpx_mock: HTTPXMock):
        """If refresh fails to fix auth, second failure propagates (no infinite loop)."""
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "stale", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            )
        )
        monkeypatch.setattr("notebooklm.auth.get_storage_path", lambda profile=None: storage_file)

        # Refresh is a no-op (still stale after)
        refresh_script = tmp_path / "refresh.py"
        refresh_script.write_text("")
        monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", self._python_refresh_cmd(refresh_script))

        # Both attempts hit the same redirect
        for _ in range(2):
            httpx_mock.add_response(
                url="https://notebooklm.google.com/",
                status_code=302,
                headers={"Location": "https://accounts.google.com/signin"},
            )
            httpx_mock.add_response(
                url="https://accounts.google.com/signin",
                content=b"<html>Login</html>",
            )

        with pytest.raises(ValueError, match="Authentication expired"):
            await fetch_tokens({"SID": "stale", "__Secure-1PSIDTS": "test_1psidts"})
        assert "_NOTEBOOKLM_REFRESH_ATTEMPTED" not in os.environ

    @pytest.mark.asyncio
    async def test_refresh_cmd_nonzero_exit_becomes_runtime_error(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """Refresh command failure surfaces as RuntimeError, not silent auth error."""
        refresh_script = tmp_path / "refresh.py"
        refresh_script.write_text(
            "import sys\nprint('vault unavailable', file=sys.stderr)\nsys.exit(1)\n"
        )
        monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", self._python_refresh_cmd(refresh_script))

        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            status_code=302,
            headers={"Location": "https://accounts.google.com/signin"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/signin",
            content=b"<html>Login</html>",
        )

        with pytest.raises(RuntimeError, match="exited 1"):
            await fetch_tokens({"SID": "stale", "__Secure-1PSIDTS": "test_1psidts"})
        assert "_NOTEBOOKLM_REFRESH_ATTEMPTED" not in os.environ


class TestAuthTokensFromStorage:
    """Test AuthTokens.from_storage class method."""

    @pytest.mark.asyncio
    async def test_from_storage_success(self, tmp_path, httpx_mock: HTTPXMock):
        """Test loading AuthTokens from storage file."""
        # Create storage file
        storage_file = tmp_path / "storage_state.json"
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        storage_file.write_text(json.dumps(storage_state))

        # Mock token fetch
        html = '"SNlM0e":"csrf_token" "FdrFJe":"session_id"'
        httpx_mock.add_response(content=html.encode())

        tokens = await AuthTokens.from_storage(storage_file)

        assert tokens.cookies[("SID", ".google.com", "/")] == "sid"
        assert tokens.flat_cookies["SID"] == "sid"
        assert tokens.csrf_token == "csrf_token"
        assert tokens.session_id == "session_id"

    @pytest.mark.asyncio
    async def test_from_storage_file_not_found(self, tmp_path):
        """Test raises error when storage file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            await AuthTokens.from_storage(tmp_path / "nonexistent.json")

    @pytest.mark.asyncio
    async def test_from_storage_preserves_cookie_attributes(self, tmp_path, httpx_mock: HTTPXMock):
        """``AuthTokens.from_storage`` builds the jar via the lossless loader.

        The recommended programmatic entry point must not erode path/secure/
        httpOnly on its way to the live jar — otherwise #365's fix only covers
        the direct loaders. See review feedback on PR #368.
        """
        storage_file = tmp_path / "storage_state.json"
        storage_state = {
            "cookies": [
                {
                    "name": "SID",
                    "value": "sid",
                    "domain": ".google.com",
                    "path": "/u/0/",
                    "expires": 1893456000,
                    "httpOnly": True,
                    "secure": True,
                },
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "test_1psidts",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": 1893456000,
                    "httpOnly": True,
                    "secure": True,
                },
            ]
        }
        storage_file.write_text(json.dumps(storage_state))

        html = '"SNlM0e":"csrf_token" "FdrFJe":"session_id"'
        httpx_mock.add_response(content=html.encode())

        tokens = await AuthTokens.from_storage(storage_file)

        sid = next(c for c in tokens.cookie_jar.jar if c.name == "SID")
        assert sid.path == "/u/0/"
        assert sid.secure is True
        assert sid.has_nonstandard_attr("HttpOnly")


class TestLoaderFlatCookieParity:
    """Regression tests for #375.

    ``load_auth_from_storage`` (CLI helper) and ``AuthTokens.from_storage``
    (library entry point) must agree on the flat name→value mapping for the
    same storage_state — otherwise out-of-tree scripts that read
    ``auth.cookie_header`` see different cookies depending on which loader
    produced them.
    """

    @pytest.mark.asyncio
    async def test_osid_on_non_base_domains_matches(self, tmp_path, httpx_mock: HTTPXMock) -> None:
        """OSID lives on myaccount.google.com and notebooklm.google.com only.

        The deterministic priority order (``_auth_domain_priority``) ranks
        ``notebooklm.google.com`` (2) above unranked allowlisted hosts (0),
        so both loaders must surface the notebooklm.google.com value.
        """
        storage_file = tmp_path / "storage_state.json"
        storage_state = {
            "cookies": [
                # Tier 1 required cookies on .google.com so both loaders accept.
                {"name": "SID", "value": "sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "ssid", "domain": ".google.com"},
                # OSID only exists on non-base hosts; myaccount comes first so a
                # naive first-wins flattener would pick it, but the priority
                # rules say notebooklm.google.com must win.
                {
                    "name": "OSID",
                    "value": "from-myaccount",
                    "domain": "myaccount.google.com",
                },
                {
                    "name": "OSID",
                    "value": "from-notebooklm",
                    "domain": "notebooklm.google.com",
                },
            ]
        }
        storage_file.write_text(json.dumps(storage_state))

        html = '"SNlM0e":"csrf_token" "FdrFJe":"session_id"'
        httpx_mock.add_response(content=html.encode())

        cli_cookies = load_auth_from_storage(storage_file)
        lib_tokens = await AuthTokens.from_storage(storage_file)

        assert cli_cookies["OSID"] == lib_tokens.flat_cookies["OSID"]
        assert cli_cookies["OSID"] == "from-notebooklm"

    @pytest.mark.asyncio
    async def test_base_domain_still_wins_on_both_paths(
        self, tmp_path, httpx_mock: HTTPXMock
    ) -> None:
        """When .google.com is present it must win on both loaders."""
        storage_file = tmp_path / "storage_state.json"
        storage_state = {
            "cookies": [
                {
                    "name": "SID",
                    "value": "from-regional",
                    "domain": ".google.com.sg",
                },
                {"name": "SID", "value": "from-base", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "ssid", "domain": ".google.com"},
                # Tier 2 binding — silences the secondary-binding warning that
                # would otherwise log on every load (issue #372).
                {"name": "OSID", "value": "osid", "domain": "notebooklm.google.com"},
            ]
        }
        storage_file.write_text(json.dumps(storage_state))

        html = '"SNlM0e":"csrf_token" "FdrFJe":"session_id"'
        httpx_mock.add_response(content=html.encode())

        cli_cookies = load_auth_from_storage(storage_file)
        lib_tokens = await AuthTokens.from_storage(storage_file)

        assert cli_cookies["SID"] == "from-base"
        assert lib_tokens.flat_cookies["SID"] == "from-base"


# =============================================================================
# COOKIE DOMAIN VALIDATION TESTS
# =============================================================================


class TestIsAllowedCookieDomain:
    """Test cookie domain validation security."""

    def test_accepts_exact_matches_from_allowlist(self):
        """Test accepts domains in ALLOWED_COOKIE_DOMAINS."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain(".google.com") is True
        assert _is_allowed_cookie_domain("notebooklm.google.com") is True
        assert _is_allowed_cookie_domain("notebooklm.cloud.google.com") is True
        assert _is_allowed_cookie_domain(".notebooklm.cloud.google.com") is True
        assert _is_allowed_cookie_domain(".googleusercontent.com") is True
        assert _is_allowed_cookie_domain(".accounts.google.com") is True

    def test_accepts_valid_google_subdomains(self):
        """Test accepts legitimate Google subdomains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("lh3.google.com") is True
        assert _is_allowed_cookie_domain("accounts.google.com") is True
        assert _is_allowed_cookie_domain("www.google.com") is True

    def test_accepts_googleusercontent_subdomains(self):
        """Test accepts googleusercontent.com subdomains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("lh3.googleusercontent.com") is True
        assert _is_allowed_cookie_domain("drum.usercontent.google.com") is True

    def test_rejects_malicious_lookalike_domains(self):
        """Test rejects domains like 'evil-google.com' that end with google.com."""
        from notebooklm.auth import _is_allowed_cookie_domain

        # These domains end with ".google.com" but are NOT subdomains
        assert _is_allowed_cookie_domain("evil-google.com") is False
        assert _is_allowed_cookie_domain("malicious-google.com") is False
        assert _is_allowed_cookie_domain("fakegoogle.com") is False

    def test_rejects_fake_googleusercontent_domains(self):
        """Test rejects fake googleusercontent domains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("evil-googleusercontent.com") is False
        assert _is_allowed_cookie_domain("fakegoogleusercontent.com") is False

    def test_rejects_unrelated_domains(self):
        """Test rejects completely unrelated domains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("example.com") is False
        assert _is_allowed_cookie_domain("evil.com") is False
        assert _is_allowed_cookie_domain("google.evil.com") is False


# =============================================================================
# CONSTANT TESTS
# =============================================================================


class TestDefaultStoragePath:
    """Test default storage path constant (deprecated, now via __getattr__)."""

    def test_default_storage_path_via_package(self):
        """Test DEFAULT_STORAGE_PATH is available via notebooklm package with deprecation warning."""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from notebooklm import DEFAULT_STORAGE_PATH

            assert DEFAULT_STORAGE_PATH is not None
            assert isinstance(DEFAULT_STORAGE_PATH, Path)
            assert DEFAULT_STORAGE_PATH.name == "storage_state.json"
            # Should have emitted a deprecation warning
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            assert len(deprecation_warnings) >= 1
            assert "deprecated" in str(deprecation_warnings[0].message).lower()


class TestMinimumRequiredCookies:
    """Test minimum required cookies constant."""

    def test_minimum_required_cookies_contains_sid(self):
        """Test MINIMUM_REQUIRED_COOKIES contains SID."""
        from notebooklm.auth import MINIMUM_REQUIRED_COOKIES

        assert "SID" in MINIMUM_REQUIRED_COOKIES


class TestAllowedCookieDomains:
    """Test cookie-domain constants (REQUIRED, OPTIONAL, ALLOWED union)."""

    def test_allowed_cookie_domains_union(self):
        """``ALLOWED_COOKIE_DOMAINS`` is the union of REQUIRED + OPTIONAL.

        Pins the union so external code that still imports the old constant
        keeps working. Internal callers should prefer the explicit
        REQUIRED/OPTIONAL constants — see the T5.G migration note in
        ``src/notebooklm/auth.py``.
        """
        from notebooklm.auth import (
            ALLOWED_COOKIE_DOMAINS,
            OPTIONAL_COOKIE_DOMAINS,
            REQUIRED_COOKIE_DOMAINS,
        )

        assert ALLOWED_COOKIE_DOMAINS == REQUIRED_COOKIE_DOMAINS | OPTIONAL_COOKIE_DOMAINS

    def test_required_cookie_domains_preserve_normalization_variants(self):
        """REQUIRED keeps both host and dotted variants of each domain.

        Codex caution (T5.G plan): http.cookiejar may normalize
        ``Domain=accounts.google.com`` to ``.accounts.google.com``. If
        REQUIRED only contained one variant, the next extraction would
        silently drop the cookie. Pin both forms here.
        """
        from notebooklm.auth import REQUIRED_COOKIE_DOMAINS

        # Required core auth + drive ingest set.
        expected = {
            ".google.com",
            "google.com",
            ".notebooklm.google.com",
            "notebooklm.google.com",
            ".notebooklm.cloud.google.com",
            "notebooklm.cloud.google.com",
            ".googleusercontent.com",
            "accounts.google.com",
            ".accounts.google.com",
            "drive.google.com",
            ".drive.google.com",
        }
        missing = expected - REQUIRED_COOKIE_DOMAINS
        assert not missing, f"REQUIRED_COOKIE_DOMAINS is missing: {missing}"

    def test_required_is_frozenset(self):
        """REQUIRED must be a frozenset so it cannot be mutated at runtime."""
        from notebooklm.auth import (
            ALLOWED_COOKIE_DOMAINS,
            OPTIONAL_COOKIE_DOMAINS,
            REQUIRED_COOKIE_DOMAINS,
        )

        assert isinstance(REQUIRED_COOKIE_DOMAINS, frozenset)
        assert isinstance(OPTIONAL_COOKIE_DOMAINS, frozenset)
        assert isinstance(ALLOWED_COOKIE_DOMAINS, frozenset)

    def test_optional_cookie_domains_label_partition(self):
        """OPTIONAL labels partition the OPTIONAL domain set exactly."""
        from notebooklm.auth import (
            OPTIONAL_COOKIE_DOMAINS,
            OPTIONAL_COOKIE_DOMAINS_BY_LABEL,
        )

        union = frozenset().union(*OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values())
        assert union == OPTIONAL_COOKIE_DOMAINS

    def test_required_and_optional_are_disjoint(self):
        """No domain appears in both REQUIRED and OPTIONAL.

        Otherwise the runtime gate would be ambiguous and the
        ``--include-domains`` opt-in would have no observable effect for
        the overlapping domain.
        """
        from notebooklm.auth import OPTIONAL_COOKIE_DOMAINS, REQUIRED_COOKIE_DOMAINS

        assert REQUIRED_COOKIE_DOMAINS.isdisjoint(OPTIONAL_COOKIE_DOMAINS)


# =============================================================================
# REGIONAL GOOGLE DOMAIN TESTS (Issue #20 fix)
# =============================================================================


class TestIsGoogleDomain:
    """Test the unified _is_google_domain function (whitelist approach)."""

    @pytest.mark.parametrize(
        "domain,expected",
        [
            # Base Google domain
            (".google.com", True),
            # .google.com.XX pattern (country-code second-level domains)
            (".google.com.sg", True),  # Singapore
            (".google.com.au", True),  # Australia
            (".google.com.br", True),  # Brazil
            (".google.com.hk", True),  # Hong Kong
            (".google.com.tw", True),  # Taiwan
            (".google.com.mx", True),  # Mexico
            (".google.com.ar", True),  # Argentina
            (".google.com.tr", True),  # Turkey
            (".google.com.ua", True),  # Ukraine
            # .google.co.XX pattern (countries using .co)
            (".google.co.uk", True),  # United Kingdom
            (".google.co.jp", True),  # Japan
            (".google.co.in", True),  # India
            (".google.co.kr", True),  # South Korea
            (".google.co.za", True),  # South Africa
            (".google.co.nz", True),  # New Zealand
            (".google.co.id", True),  # Indonesia
            (".google.co.th", True),  # Thailand
            # .google.XX pattern (single ccTLD)
            (".google.cn", True),  # China
            (".google.de", True),  # Germany
            (".google.fr", True),  # France
            (".google.it", True),  # Italy
            (".google.es", True),  # Spain
            (".google.nl", True),  # Netherlands
            (".google.pl", True),  # Poland
            (".google.ru", True),  # Russia
            (".google.ca", True),  # Canada
            (".google.cat", True),  # Catalonia (3-letter special case)
            # Invalid domains that should be rejected
            (".google.zz", False),  # Invalid ccTLD
            (".google.xyz", False),  # Not in whitelist
            (".google.com.fake", False),  # Not in whitelist
            (".notebooklm.google.com", False),  # Accepted by auth allowlist, not here
            (".mail.google.com", False),
            (".drive.google.com", False),
            (".evilnotebooklm.google.com", False),
            (".notebooklm.google.com.evil", False),
            (".notgoogle.com", False),
            (".evil-google.com", False),
            ("google.com", False),  # Missing leading dot
            ("google.com.sg", False),  # Missing leading dot
            (".youtube.com", False),
            (".google.", False),  # Incomplete
            ("", False),  # Empty
        ],
    )
    def test_is_google_domain(self, domain, expected):
        """Test _is_google_domain with various domain patterns."""
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is expected

    @pytest.mark.parametrize(
        "domain",
        [
            # Case sensitivity - cookie domains per RFC should be lowercase
            ".GOOGLE.COM",
            ".Google.Com",
            ".google.COM.SG",
            ".GOOGLE.CO.UK",
            ".GOOGLE.DE",
        ],
    )
    def test_rejects_uppercase_domains(self, domain):
        """Test that uppercase domains are rejected (case-sensitive matching).

        Per RFC 6265, cookie domains SHOULD be lowercase. Playwright and browsers
        normalize domains to lowercase, so we don't need case-insensitive matching.
        """
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is False

    @pytest.mark.parametrize(
        "domain",
        [
            " .google.com",  # Leading space
            ".google.com ",  # Trailing space
            "\t.google.com",  # Tab
            ".google.com\n",  # Newline
        ],
    )
    def test_rejects_domains_with_whitespace(self, domain):
        """Test that domains with whitespace are rejected."""
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is False

    @pytest.mark.parametrize(
        "domain",
        [
            ".google..com",  # Double dot
            "..google.com",  # Leading double dot
            ".google.com.",  # Trailing dot
        ],
    )
    def test_rejects_malformed_domains(self, domain):
        """Test that malformed domains with extra dots are rejected."""
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is False

    @pytest.mark.parametrize(
        "domain",
        [
            # Subdomains without leading dot are still rejected
            "accounts.google.com",
            "lh3.google.com",
            # Subdomains of regional domains are still rejected (not whitelisted)
            "accounts.google.de",
            "lh3.google.co.uk",
            ".accounts.google.de",  # Leading dot but regional subdomain
        ],
    )
    def test_rejects_subdomains(self, domain):
        """Test that non-canonical subdomains are rejected by _is_google_domain.

        _is_google_domain only accepts .google.com and regional root domains
        (.google.com.sg, etc). Auth extraction uses ALLOWED_COOKIE_DOMAINS for
        auth-specific subdomains.
        """
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is False

    @pytest.mark.parametrize(
        "domain",
        [
            ".google.com.sg.fake",  # Extra suffix
            ".fake.google.com.sg",  # Prefix on regional
            ".google.com.sgx",  # Extended TLD
            ".google.co.ukx",  # Extended co.XX
            ".google.dex",  # Extended ccTLD
        ],
    )
    def test_rejects_suffix_exploits(self, domain):
        """Test that attempts to exploit suffix matching are rejected."""
        from notebooklm.auth import _is_google_domain

        assert _is_google_domain(domain) is False


class TestIsAllowedAuthDomain:
    """Test auth cookie domain validation including regional Google domains."""

    def test_accepts_primary_google_domains(self):
        """Test accepts domains in ALLOWED_COOKIE_DOMAINS."""
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain(".google.com") is True
        assert _is_allowed_auth_domain("notebooklm.google.com") is True
        assert _is_allowed_auth_domain(".notebooklm.google.com") is True  # Issue #329
        assert _is_allowed_auth_domain(".googleusercontent.com") is True

    def test_accepts_all_regional_patterns(self):
        """Test accepts all three regional Google domain patterns (Issue #20)."""
        from notebooklm.auth import _is_allowed_auth_domain

        # .google.com.XX pattern
        assert _is_allowed_auth_domain(".google.com.sg") is True  # Singapore
        assert _is_allowed_auth_domain(".google.com.au") is True  # Australia

        # .google.co.XX pattern
        assert _is_allowed_auth_domain(".google.co.uk") is True  # UK
        assert _is_allowed_auth_domain(".google.co.jp") is True  # Japan

        # .google.XX pattern
        assert _is_allowed_auth_domain(".google.de") is True  # Germany
        assert _is_allowed_auth_domain(".google.fr") is True  # France

    def test_accepts_youtube_for_opt_in_post_t5g(self):
        """YouTube cookies pass the runtime gate so ``--include-domains=youtube``
        works end-to-end.

        T5.G enforces blast-radius reduction at *extraction* time
        (rookiepy is asked for :data:`REQUIRED_COOKIE_DOMAINS` only).
        The runtime gate stays permissive over the full union
        (:data:`ALLOWED_COOKIE_DOMAINS`) so opted-in YouTube cookies
        survive ``convert_rookiepy_cookies_to_storage_state``,
        ``extract_cookies_with_domains``, and
        ``build_httpx_cookies_from_storage`` — all of which delegate to
        this gate.
        """
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain(".youtube.com") is True
        assert _is_allowed_auth_domain("youtube.com") is True
        assert _is_allowed_auth_domain("accounts.youtube.com") is True
        assert _is_allowed_auth_domain(".accounts.youtube.com") is True

    def test_accepts_sibling_google_subdomains(self):
        """Sibling Google subdomains pass via the ``.google.com`` suffix.

        Drive / Docs / myaccount / Mail are subdomains of ``.google.com``,
        so the runtime gate accepts them via the suffix branch (tier 3 of
        :func:`_is_allowed_cookie_domain`) even though only ``drive.*`` is
        in :data:`REQUIRED_COOKIE_DOMAINS`.
        """
        from notebooklm.auth import _is_allowed_auth_domain

        # Drive (in REQUIRED) — exact-match tier
        assert _is_allowed_auth_domain("drive.google.com") is True
        assert _is_allowed_auth_domain(".drive.google.com") is True
        # Docs / myaccount / mail — accepted via .google.com suffix tier
        # (storage_state.json may still contain these from legacy logins).
        assert _is_allowed_auth_domain("docs.google.com") is True
        assert _is_allowed_auth_domain(".docs.google.com") is True
        assert _is_allowed_auth_domain("myaccount.google.com") is True
        assert _is_allowed_auth_domain(".myaccount.google.com") is True
        assert _is_allowed_auth_domain("mail.google.com") is True

    def test_rejects_unrelated_domains(self):
        """Test rejects non-Google domains."""
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain("evil.com") is False
        assert _is_allowed_auth_domain(".evil-google.com") is False
        assert _is_allowed_auth_domain(".not-youtube.com") is False
        assert _is_allowed_auth_domain("notyoutube.com") is False

    def test_rejects_malicious_google_lookalikes(self):
        """Test rejects domains that look like Google but aren't."""
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain("google.com.evil.sg") is False
        # Note: post-#360 unification, .mail.google.com is accepted (it's a
        # legitimate Google-owned subdomain). Only foreign suffixes are rejected.
        assert _is_allowed_auth_domain(".google.com.evil") is False
        assert _is_allowed_auth_domain(".evilnotebooklm.google.com.evil") is False
        assert _is_allowed_auth_domain(".not-google.com.sg") is False
        assert _is_allowed_auth_domain(".google.zz") is False  # Invalid ccTLD

    def test_requires_leading_dot_for_regional(self):
        """Test regional domains require leading dot.

        Regional ccTLDs like ``google.com.sg`` (no leading dot) are not in
        ALLOWED_COOKIE_DOMAINS and are not accepted by ``_is_google_domain``
        (which requires the leading dot for regional patterns) or by the
        suffix paths (which require the leading-dot suffix).
        """
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain("google.com.sg") is False
        assert _is_allowed_auth_domain("google.co.uk") is False
        assert _is_allowed_auth_domain("google.de") is False


class TestAuthDomainPriority:
    """Test `_auth_domain_priority` tier mapping for duplicate-cookie resolution."""

    @pytest.mark.parametrize(
        "domain,expected",
        [
            (".google.com", 4),
            (".notebooklm.google.com", 3),
            (".notebooklm.cloud.google.com", 3),
            ("notebooklm.google.com", 2),
            ("notebooklm.cloud.google.com", 2),
            (".google.de", 1),
            (".google.com.sg", 1),
            (".google.co.uk", 1),
            (".googleusercontent.com", 0),
            ("evil.com", 0),
            ("", 0),
        ],
    )
    def test_priority_tiers(self, domain, expected):
        from notebooklm.auth import _auth_domain_priority

        assert _auth_domain_priority(domain) == expected

    def test_priority_strict_ordering(self):
        """Higher tiers strictly outrank lower tiers — no ties between named tiers."""
        from notebooklm.auth import _auth_domain_priority

        priorities = [
            _auth_domain_priority(".google.com"),
            _auth_domain_priority(".notebooklm.google.com"),
            _auth_domain_priority("notebooklm.google.com"),
            _auth_domain_priority(".google.de"),
            _auth_domain_priority(".googleusercontent.com"),
        ]
        assert priorities == sorted(priorities, reverse=True)
        assert len(set(priorities)) == len(priorities)


class TestIsAllowedCookieDomainRegional:
    """Test _is_allowed_cookie_domain with regional Google domains."""

    def test_accepts_regional_google_domains_for_downloads(self):
        """Test that download cookie validation accepts regional domains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        # .google.com.XX pattern
        assert _is_allowed_cookie_domain(".google.com.sg") is True
        assert _is_allowed_cookie_domain(".google.com.au") is True

        # .google.co.XX pattern
        assert _is_allowed_cookie_domain(".google.co.uk") is True
        assert _is_allowed_cookie_domain(".google.co.jp") is True

        # .google.XX pattern
        assert _is_allowed_cookie_domain(".google.de") is True
        assert _is_allowed_cookie_domain(".google.fr") is True

    def test_still_accepts_subdomains(self):
        """Test that subdomain suffix matching still works."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("lh3.google.com") is True
        assert _is_allowed_cookie_domain("accounts.google.com") is True
        assert _is_allowed_cookie_domain("lh3.googleusercontent.com") is True

    def test_youtube_accepted_for_opt_in_post_t5g(self):
        """YouTube remains in the runtime allowlist (T5.G).

        Blast-radius reduction is enforced at extraction time, not by the
        runtime gate. See :class:`TestIsAllowedAuthDomain` for the
        rationale and :class:`TestSiblingGoogleProductExtraction` for the
        extraction-time contracts.
        """
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain(".youtube.com") is True
        assert _is_allowed_cookie_domain("youtube.com") is True
        assert _is_allowed_cookie_domain("accounts.youtube.com") is True
        assert _is_allowed_cookie_domain(".accounts.youtube.com") is True

    def test_accepts_sibling_google_subdomains(self):
        """Sibling Google subdomains still pass via the ``.google.com`` suffix tier."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain("drive.google.com") is True
        assert _is_allowed_cookie_domain("docs.google.com") is True
        assert _is_allowed_cookie_domain("myaccount.google.com") is True
        assert _is_allowed_cookie_domain("mail.google.com") is True

    def test_rejects_invalid_domains(self):
        """Test rejects invalid domains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain(".google.zz") is False
        assert _is_allowed_cookie_domain("evil-google.com") is False
        assert _is_allowed_cookie_domain(".not-youtube.com") is False
        assert _is_allowed_cookie_domain("notyoutube.com") is False


class TestExtractCookiesRegionalDomains:
    """Test cookie extraction from regional Google domains (Issue #20, #27)."""

    @pytest.mark.parametrize(
        "domain,sid_value,description",
        [
            (".google.com.sg", "sid_from_singapore", "Issue #20 - Singapore"),
            (".google.cn", "sid_from_china", "Issue #27 - China"),
            (".google.co.uk", "sid_from_uk", "UK regional domain"),
            (".google.de", "sid_from_de", "Germany regional domain"),
        ],
    )
    def test_extracts_sid_from_regional_domain(self, domain, sid_value, description):
        """Test extracts SID cookie from regional Google domains."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": sid_value, "domain": domain},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": domain},
                {"name": "OSID", "value": "osid_value", "domain": "notebooklm.google.com"},
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)

        assert cookies["SID"] == sid_value
        assert cookies["OSID"] == "osid_value"

    def test_extracts_sid_from_all_regional_patterns(self):
        """Test extracts SID from all three regional domain patterns."""
        # Test .google.com.XX
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_sg", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_sg"

        # Test .google.co.XX
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_uk", "domain": ".google.co.uk"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.co.uk"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_uk"

        # Test .google.XX
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_de", "domain": ".google.de"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.de"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_de"

    def test_extracts_multiple_regional_cookies(self):
        """Test extracts cookies from multiple regional domains."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_au", "domain": ".google.com.au"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.au"},
                {"name": "HSID", "value": "hsid_jp", "domain": ".google.co.jp"},
                {"name": "SSID", "value": "ssid_de", "domain": ".google.de"},
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)

        assert cookies["SID"] == "sid_au"
        assert cookies["HSID"] == "hsid_jp"
        assert cookies["SSID"] == "ssid_de"

    def test_prefers_primary_domain_over_regional(self):
        """Test that .google.com cookie wins over regional domains.

        Regression test for PR #34: When the same cookie name exists on both
        .google.com and a regional domain (e.g., .google.com.sg), the .google.com
        value should ALWAYS be used regardless of cookie order in the list.
        """
        # Test case 1: base domain listed first
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_global", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "SID", "value": "sid_regional", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_global", ".google.com should win (base first)"

        # Test case 2: regional domain listed first (this was the bug scenario)
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_regional", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
                {"name": "SID", "value": "sid_global", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_global", ".google.com should win (regional first)"

    def test_regional_google_sid_outranks_sibling_product_sid(self):
        """Regional Google SID outranks YouTube SID when both are present.

        Post-#360 the allowlist accepts sibling-product cookies, but the
        priority ladder still prefers a regional Google SID over a YouTube
        SID when both are seen, because YouTube falls into the unranked tier
        (priority 0) and regional Google domains sit at priority 1.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "youtube_sid", "domain": ".youtube.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".youtube.com"},
                {"name": "SID", "value": "regional_sid", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "regional_sid"

    def test_cookie_extraction_order_independent(self):
        """Test that cookie extraction is deterministic regardless of list order.

        Regression test for PR #34: The original bug caused non-deterministic
        behavior because Python dict's "last one wins" behavior meant the result
        depended on cookie iteration order.

        This test verifies that all permutations produce the same result.
        """
        from itertools import permutations

        base_cookies = [
            {"name": "SID", "value": "sid_base", "domain": ".google.com"},
            {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            {"name": "SID", "value": "sid_sg", "domain": ".google.com.sg"},
            {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
            {"name": "SID", "value": "sid_de", "domain": ".google.de"},
            {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.de"},
        ]

        results = set()
        for ordering in permutations(base_cookies):
            storage_state = {"cookies": list(ordering)}
            cookies = extract_cookies_from_storage(storage_state)
            results.add(cookies["SID"])

        # All permutations should produce the same result: .google.com wins
        assert results == {"sid_base"}, (
            f"Extraction should be deterministic, but got different results: {results}"
        )

    def test_regional_only_uses_first_encountered(self):
        """Test behavior when only regional domains exist (no .google.com).

        When .google.com is not present, we use whatever cookie we encounter.
        This documents the expected fallback behavior.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_sg", "domain": ".google.com.sg"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com.sg"},
                {"name": "SID", "value": "sid_de", "domain": ".google.de"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.de"},
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)

        # Without .google.com, first encountered wins
        assert cookies["SID"] == "sid_sg"


class TestLoadHttpxCookiesRegional:
    """Test load_httpx_cookies with regional Google domains."""

    def test_loads_cookies_from_regional_domain(self, tmp_path):
        """Test loading httpx cookies from regional Google domain."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_from_uk", "domain": ".google.co.uk"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.co.uk"},
                {"name": "HSID", "value": "hsid_val", "domain": ".google.co.uk"},
            ]
        }

        storage_file = tmp_path / "storage.json"
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_httpx_cookies(path=storage_file)
        assert cookies.get("SID", domain=".google.co.uk") == "sid_from_uk"

    def test_loads_cookies_from_all_regional_patterns(self, tmp_path):
        """Test loading httpx cookies from all regional patterns."""
        # Test with .google.de (single ccTLD)
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_de", "domain": ".google.de"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.de"},
            ]
        }
        storage_file = tmp_path / "storage.json"
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_httpx_cookies(path=storage_file)
        assert cookies.get("SID", domain=".google.de") == "sid_de"


class TestSiblingGoogleProductExtraction:
    """Cookie extraction behavior for sibling Google product domains.

    T5.G enforces blast-radius reduction at *extraction* time
    (``_build_google_cookie_domains`` defaults to
    :data:`REQUIRED_COOKIE_DOMAINS`, so rookiepy never returns YouTube
    cookies unless the user opts in). The runtime gate stays permissive
    over the full union so that opted-in cookies survive every downstream
    filter. This class pins the contract that the downstream
    storage-state filters do *not* drop sibling-product cookies once they
    have been extracted — neither for Google subdomains nor for opted-in
    YouTube cookies.
    """

    GOOGLE_SUBDOMAIN_SIBLINGS = [
        "drive.google.com",
        ".drive.google.com",
        "docs.google.com",
        ".docs.google.com",
        "myaccount.google.com",
        ".myaccount.google.com",
        "mail.google.com",
        ".mail.google.com",
    ]
    YOUTUBE_DOMAINS = [
        ".youtube.com",
        "youtube.com",
        "accounts.youtube.com",
        ".accounts.youtube.com",
    ]

    @pytest.mark.parametrize("domain", GOOGLE_SUBDOMAIN_SIBLINGS)
    def test_extract_cookies_with_domains_keeps_google_subdomain_siblings(self, domain):
        """``extract_cookies_with_domains`` keeps Drive/Docs/myaccount/Mail cookies."""
        storage_state = {
            "cookies": [
                # Required SID on .google.com so extraction doesn't fail
                {"name": "SID", "value": "base_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "PRODUCT_TOKEN", "value": "sibling", "domain": domain},
            ]
        }
        cookie_map = extract_cookies_with_domains(storage_state)
        assert ("PRODUCT_TOKEN", domain, "/") in cookie_map
        assert cookie_map[("PRODUCT_TOKEN", domain, "/")] == "sibling"

    @pytest.mark.parametrize("domain", YOUTUBE_DOMAINS)
    def test_extract_cookies_with_domains_keeps_opted_in_youtube(self, domain):
        """T5.G contract: ``extract_cookies_with_domains`` keeps YouTube cookies.

        Blast radius is reduced at extraction time (rookiepy does not
        return YouTube cookies by default). When a user has opted in via
        ``--include-domains=youtube`` and the storage_state contains a
        YouTube cookie, this filter must keep it so the opt-in is
        observable end-to-end.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "base_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "YT_TOKEN", "value": "yt", "domain": domain},
            ]
        }
        cookie_map = extract_cookies_with_domains(storage_state)
        assert ("YT_TOKEN", domain, "/") in cookie_map
        assert cookie_map[("YT_TOKEN", domain, "/")] == "yt"

    @pytest.mark.parametrize("domain", GOOGLE_SUBDOMAIN_SIBLINGS)
    def test_load_httpx_cookies_keeps_google_subdomain_siblings(self, tmp_path, domain):
        """``load_httpx_cookies`` accepts Google-subdomain sibling cookies."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "base_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "PRODUCT_TOKEN", "value": "sibling", "domain": domain},
            ]
        }
        storage_file = tmp_path / "storage.json"
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_httpx_cookies(path=storage_file)
        assert cookies.get("PRODUCT_TOKEN", domain=domain) == "sibling"

    @pytest.mark.parametrize("domain", YOUTUBE_DOMAINS)
    def test_load_httpx_cookies_keeps_opted_in_youtube(self, tmp_path, domain):
        """``load_httpx_cookies`` keeps opted-in YouTube cookies (T5.G).

        Blast radius is reduced at extraction time. The runtime gate
        (and therefore this loader) is permissive over the union so
        ``--include-domains=youtube`` cookies survive download/refresh
        operations.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "base_sid", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "YT_TOKEN", "value": "yt", "domain": domain},
            ]
        }
        storage_file = tmp_path / "storage.json"
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_httpx_cookies(path=storage_file)
        assert cookies.get("YT_TOKEN", domain=domain) == "yt"

    @pytest.mark.parametrize("domain", GOOGLE_SUBDOMAIN_SIBLINGS)
    def test_convert_rookiepy_keeps_google_subdomain_siblings(self, domain):
        """rookiepy → storage_state conversion keeps Google-subdomain siblings."""
        raw = [
            {
                "domain": domain,
                "name": "PRODUCT_TOKEN",
                "value": "sibling",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert len(result["cookies"]) == 1
        assert result["cookies"][0]["domain"] == domain

    @pytest.mark.parametrize("domain", YOUTUBE_DOMAINS)
    def test_convert_rookiepy_keeps_opted_in_youtube(self, domain):
        """rookiepy → storage_state keeps opted-in YouTube cookies (T5.G).

        Blast radius is reduced at extraction time by
        ``_build_google_cookie_domains`` (the rookiepy domain filter).
        Once a YouTube cookie has been extracted (because the user
        opted in), this converter must keep it so the opt-in is
        observable end-to-end.
        """
        raw = [
            {
                "domain": domain,
                "name": "YT_TOKEN",
                "value": "yt",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert len(result["cookies"]) == 1
        assert result["cookies"][0]["domain"] == domain
        assert result["cookies"][0]["name"] == "YT_TOKEN"

    def test_strict_allowlisted_domains_still_work(self):
        """Regression: pre-existing strict-allowlisted domains keep working.

        Ensures the unification didn't accidentally drop any of the original
        canonical NotebookLM auth domains.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "v1", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "HSID", "value": "v2", "domain": ".google.com"},
                {"name": "OSID", "value": "v3", "domain": "notebooklm.google.com"},
                {"name": "OSID2", "value": "v4", "domain": ".notebooklm.google.com"},
                {"name": "ACC", "value": "v5", "domain": "accounts.google.com"},
                {"name": "ACC2", "value": "v6", "domain": ".accounts.google.com"},
                {"name": "MEDIA", "value": "v7", "domain": ".googleusercontent.com"},
            ]
        }
        cookie_map = extract_cookies_with_domains(storage_state)
        assert ("SID", ".google.com", "/") in cookie_map
        assert ("HSID", ".google.com", "/") in cookie_map
        assert ("OSID", "notebooklm.google.com", "/") in cookie_map
        assert ("OSID2", ".notebooklm.google.com", "/") in cookie_map
        assert ("ACC", "accounts.google.com", "/") in cookie_map
        assert ("ACC2", ".accounts.google.com", "/") in cookie_map
        assert ("MEDIA", ".googleusercontent.com", "/") in cookie_map

    def test_unified_filter_rejects_unrelated_domains(self):
        """Regression: cookies from unrelated domains are still rejected."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "v1", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
                {"name": "EVIL", "value": "x", "domain": ".evil.com"},
                {"name": "EVIL2", "value": "y", "domain": ".not-google.com"},
                {"name": "EVIL3", "value": "z", "domain": ".evil-google.com"},
                {"name": "EVIL4", "value": "w", "domain": ".not-youtube.com"},
            ]
        }
        cookie_map = extract_cookies_with_domains(storage_state)
        kept_names = {name for name, _, _ in cookie_map}
        assert kept_names == {"SID", "__Secure-1PSIDTS"}


class TestPathAwareCookieIdentity:
    """Issue #369: ``(name, domain, path)`` is the cookie identity per RFC 6265
    §5.3. Two cookies sharing ``(name, domain)`` at different paths must coexist
    end-to-end across the load/save APIs, and ``_find_cookie_for_storage`` must
    not return a sibling on a different path.
    """

    # Shared fixtures: the OSID path-sibling pair under test is identical
    # across cases except for the values and any extra Playwright-shaped
    # fields (httpOnly/secure/expires) the load/save round trip needs.

    _OSID_DOMAIN = "accounts.google.com"

    def _required_cookies(self) -> list[dict[str, Any]]:
        return [
            {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "tts",
                "domain": ".google.com",
                "path": "/",
            },
        ]

    def _osid_siblings(
        self,
        *,
        root_value: str = "root",
        u0_value: str = "u0",
        extras: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return an ``OSID@/`` + ``OSID@/u/0/`` pair sharing optional fields."""
        base = extras or {}
        return [
            {
                "name": "OSID",
                "value": root_value,
                "domain": self._OSID_DOMAIN,
                "path": "/",
                **base,
            },
            {
                "name": "OSID",
                "value": u0_value,
                "domain": self._OSID_DOMAIN,
                "path": "/u/0/",
                **base,
            },
        ]

    def _storage_state(self, osids: list[dict[str, Any]]) -> dict[str, Any]:
        return {"cookies": [*self._required_cookies(), *osids]}

    def test_extract_cookies_with_domains_keeps_path_siblings(self):
        """Two cookies sharing name+domain at distinct paths both survive
        extraction (pre-#369 the second one silently shadowed the first)."""
        cookie_map = extract_cookies_with_domains(self._storage_state(self._osid_siblings()))
        assert cookie_map[("OSID", self._OSID_DOMAIN, "/")] == "root"
        assert cookie_map[("OSID", self._OSID_DOMAIN, "/u/0/")] == "u0"

    def test_build_httpx_cookies_keeps_path_siblings(self, tmp_path):
        """The load-side dedup in ``build_httpx_cookies_from_storage`` keys
        on ``(name, domain, path)`` so both path-siblings land in the jar."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps(self._storage_state(self._osid_siblings())))

        jar = build_httpx_cookies_from_storage(storage)
        observed = {(c.name, c.domain, c.path): c.value for c in jar.jar if c.name == "OSID"}
        assert observed == {
            ("OSID", self._OSID_DOMAIN, "/"): "root",
            ("OSID", self._OSID_DOMAIN, "/u/0/"): "u0",
        }

    def test_round_trip_load_fetch_save_preserves_siblings(self, tmp_path):
        """A synthetic ``OSID@/`` + ``OSID@/u/0/`` storage_state survives a
        load → fetch (no-op mutation) → save round trip with **both** entries
        intact. Closes the issue's acceptance criterion."""
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                self._storage_state(
                    self._osid_siblings(
                        extras={"httpOnly": False, "secure": False, "expires": -1},
                    )
                )
            )
        )

        jar = build_httpx_cookies_from_storage(storage)
        snapshot = snapshot_cookie_jar(jar)
        # No mutation; save_cookies_to_storage is a no-op delta.
        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        reloaded = json.loads(storage.read_text())
        osids = [c for c in reloaded["cookies"] if c["name"] == "OSID"]
        assert {(c["domain"], c["path"], c["value"]) for c in osids} == {
            (self._OSID_DOMAIN, "/", "root"),
            (self._OSID_DOMAIN, "/u/0/", "u0"),
        }

    def test_legacy_full_merge_preserves_path_siblings(self, tmp_path):
        """The legacy ``original_snapshot=None`` merge keys ``cookies_by_key`` /
        ``stored_keys`` on ``(name, domain, path)`` so a refreshed cookie at
        path ``/`` does not overwrite a sibling at ``/u/0/`` and vice versa."""
        import warnings

        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                self._storage_state(
                    self._osid_siblings(
                        root_value="root_old",
                        u0_value="u0_old",
                        extras={"expires": -1},
                    )
                )
            )
        )

        jar = httpx.Cookies()
        jar.set("SID", "sid", domain=".google.com", path="/")
        jar.set("__Secure-1PSIDTS", "tts", domain=".google.com", path="/")
        jar.set("OSID", "root_new", domain="accounts.google.com", path="/")
        jar.set("OSID", "u0_new", domain="accounts.google.com", path="/u/0/")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            save_cookies_to_storage(jar, storage)

        reloaded = json.loads(storage.read_text())
        osids = [c for c in reloaded["cookies"] if c["name"] == "OSID"]
        observed = {(c["domain"], c["path"], c["value"]) for c in osids}
        # Both siblings refreshed independently — pre-#369 the legacy merge
        # would have shadowed one with the other because cookies_by_key was
        # keyed on (name, domain).
        assert observed == {
            (self._OSID_DOMAIN, "/", "root_new"),
            (self._OSID_DOMAIN, "/u/0/", "u0_new"),
        }

    def test_normalize_cookie_map_widens_legacy_2tuple_input(self):
        """Pre-#369 callers built ``{(name, domain): value}`` dicts by hand;
        those keep working — :func:`normalize_cookie_map` widens the missing
        path to ``/`` so the rest of the pipeline sees the canonical shape."""
        from notebooklm.auth import normalize_cookie_map

        result = normalize_cookie_map(
            {
                ("SID", ".google.com"): "abc",
                ("OSID", "accounts.google.com"): "xyz",
            }
        )
        assert result == {
            ("SID", ".google.com", "/"): "abc",
            ("OSID", "accounts.google.com", "/"): "xyz",
        }

    def test_normalize_cookie_map_warns_on_malformed_tuple(self, caplog):
        """A malformed tuple key would otherwise vanish into a downstream
        'missing required cookies' error. Surface it via ``logger.warning``."""
        import logging

        from notebooklm.auth import normalize_cookie_map

        with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
            result = normalize_cookie_map(
                {("SID", ".google.com", "/", "extra"): "abc"}  # type: ignore[dict-item]
            )
        assert result == {}
        assert any("malformed cookie key" in rec.message for rec in caplog.records)

    def test_update_cookie_input_preserves_legacy_2tuple_shape(self):
        """A caller that originally passed a legacy 2-tuple dict must observe
        the same shape after an in-place refresh — internal widening should
        not bleed 3-tuple keys into their data structure."""
        from notebooklm.auth import _update_cookie_input

        target: dict[Any, str] = {
            ("SID", ".google.com"): "old_sid",
            ("OSID", "accounts.google.com"): "old_osid",
        }
        fresh: dict[tuple[str, str, str], str] = {
            ("SID", ".google.com", "/"): "new_sid",
            ("OSID", "accounts.google.com", "/"): "new_osid",
        }

        _update_cookie_input(target, fresh)

        assert target == {
            ("SID", ".google.com"): "new_sid",
            ("OSID", "accounts.google.com"): "new_osid",
        }

    def test_find_cookie_for_storage_picks_path_sibling(self):
        """``_find_cookie_for_storage`` must discriminate on path: a stored
        ``(OSID, accounts.google.com, /u/0/)`` row must NOT be refreshed with
        the ``OSID`` value from path ``/``. Pre-#369 the path was ignored and
        either sibling could be returned."""
        from notebooklm.auth import _find_cookie_for_storage

        # Two in-memory cookies sharing (name, domain) at distinct paths
        # coexist in a single ``http.cookiejar`` because its internal index is
        # ``(domain, path, name)`` — exactly the identity #369 makes first-class.
        jar = httpx.Cookies()
        jar.set("OSID", "root", domain=self._OSID_DOMAIN, path="/")
        jar.set("OSID", "u0", domain=self._OSID_DOMAIN, path="/u/0/")
        cookies_by_key = {(c.name, c.domain, c.path or "/"): c for c in jar.jar}

        # Looking up the /u/0/ key must return the /u/0/ cookie, not the / one.
        found = _find_cookie_for_storage(
            cookies_by_key,
            ("OSID", self._OSID_DOMAIN, "/u/0/"),
            stored_value="stale_u0",
        )
        assert found is not None
        assert found.value == "u0"
        assert found.path == "/u/0/"


class TestRookiepyDomainsCoverage:
    """Confirm ``_login_with_browser_cookies`` knows about every sibling product.

    Post-T5.G the rookiepy ``domains`` list defaults to REQUIRED only, but
    the ``--include-domains`` opt-in still has to cover every sibling
    product. Pin that every known sibling label resolves to at least one
    domain, so future contributors can't forget to wire up a new label.
    """

    def test_every_optional_label_has_domains(self):
        from notebooklm.auth import OPTIONAL_COOKIE_DOMAINS_BY_LABEL

        for label, domains in OPTIONAL_COOKIE_DOMAINS_BY_LABEL.items():
            assert domains, f"label {label!r} maps to an empty domain set"

    def test_allowlist_union_still_covers_legacy_siblings(self):
        """ALLOWED_COOKIE_DOMAINS (union) covers every legacy sibling domain.

        External callers that read the union constant for documentation /
        validation purposes (e.g. ``cli.session`` historically) keep
        seeing the same domains. Only the runtime gate has tightened.
        """
        from notebooklm.auth import ALLOWED_COOKIE_DOMAINS

        for domain in (
            ".youtube.com",
            "accounts.youtube.com",
            "drive.google.com",
            "docs.google.com",
            "myaccount.google.com",
        ):
            assert domain in ALLOWED_COOKIE_DOMAINS, (
                f"{domain!r} must be in ALLOWED_COOKIE_DOMAINS (union) so "
                "callers that read the legacy constant still see it (T5.G)"
            )


class TestConvertRookiepyCookies:
    """Test conversion from rookiepy cookie dicts to storage_state.json format."""

    def test_converts_basic_cookie(self):
        """Single cookie is converted to storage_state format."""
        raw = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "abc",
                "path": "/",
                "secure": True,
                "expires": 1234567890,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert result["cookies"][0] == {
            "name": "SID",
            "value": "abc",
            "domain": ".google.com",
            "path": "/",
            "expires": 1234567890,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        }
        assert result["origins"] == []

    def test_none_expires_becomes_minus_one(self):
        """rookiepy uses None for session cookies; storage_state uses -1."""
        raw = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert result["cookies"][0]["expires"] == -1

    def test_filters_non_google_domains(self):
        """Non-Google domains are dropped."""
        raw = [
            {
                "domain": ".google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
            {
                "domain": "evil.com",
                "name": "TRACK",
                "value": "y",
                "path": "/",
                "secure": False,
                "expires": None,
                "http_only": False,
            },
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert len(result["cookies"]) == 1
        assert result["cookies"][0]["name"] == "SID"

    def test_snake_to_camel_case(self):
        """http_only (rookiepy) → httpOnly (storage_state)."""
        raw = [
            {
                "domain": ".google.com",
                "name": "X",
                "value": "y",
                "path": "/",
                "secure": False,
                "expires": None,
                "http_only": True,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert "http_only" not in result["cookies"][0]
        assert result["cookies"][0]["httpOnly"] is True

    def test_empty_list(self):
        """Empty cookie list returns empty structure."""
        assert convert_rookiepy_cookies_to_storage_state([]) == {
            "cookies": [],
            "origins": [],
        }

    def test_regional_google_domain_included(self):
        """Regional domains like .google.co.uk are kept."""
        raw = [
            {
                "domain": ".google.co.uk",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert len(result["cookies"]) == 1

    def test_notebooklm_subdomain_included(self):
        """Playwright-style NotebookLM subdomain cookies are kept."""
        raw = [
            {
                "domain": ".notebooklm.google.com",
                "name": "OSID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert len(result["cookies"]) == 1
        assert result["cookies"][0]["domain"] == ".notebooklm.google.com"
        assert result["cookies"][0]["name"] == "OSID"

    def test_sibling_google_product_subdomains_kept(self):
        """Auth conversion keeps sibling Google subdomain cookies (post-T5.G).

        T5.G narrowed the runtime gate from the union to REQUIRED, but the
        ``.google.com`` suffix branch of :func:`_is_allowed_cookie_domain`
        still accepts cookies on Drive / Docs / myaccount / Mail. Only
        ``youtube.com`` (which is not a subdomain of ``.google.com``) is
        now dropped by default — see
        :class:`TestSiblingGoogleProductExtraction` for that contract.
        """
        raw = [
            {
                "domain": domain,
                "name": "SID",
                "value": "v",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            }
            for domain in (
                ".mail.google.com",
                ".drive.google.com",
                ".docs.google.com",
                ".myaccount.google.com",
            )
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        kept_domains = {c["domain"] for c in result["cookies"]}
        assert kept_domains == {
            ".mail.google.com",
            ".drive.google.com",
            ".docs.google.com",
            ".myaccount.google.com",
        }

    def test_unrelated_domains_still_filtered(self):
        """Cookies from non-Google domains are still dropped."""
        raw = [
            {
                "domain": ".evil.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
            {
                "domain": ".not-google.com",
                "name": "SID",
                "value": "x",
                "path": "/",
                "secure": True,
                "expires": None,
                "http_only": False,
            },
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        assert result == {"cookies": [], "origins": []}


_POKE_URL_RE = re.compile(r"^https://accounts\.google\.com/RotateCookies$")
_NOTEBOOKLM_HOMEPAGE_HTML = (
    b'<html><script>window.WIZ_global_data={"SNlM0e":"csrf_ok","FdrFJe":"sess_ok"};</script></html>'
)


def _stale_storage(path: Path, *, age_seconds: float) -> None:
    """Backdate ``path``'s mtime so the L1 rate-limit guard does not skip the poke."""
    target = path.stat().st_mtime - age_seconds
    os.utime(path, (target, target))


class TestIsRecentlyRotated:
    """Direct boundary coverage for ``_is_recently_rotated``."""

    def test_none_path_is_not_recent(self):
        assert auth_module._is_recently_rotated(None) is False

    def test_missing_file_is_not_recent(self, tmp_path):
        assert auth_module._is_recently_rotated(tmp_path / "nope.json") is False

    def test_just_written_file_is_recent(self, tmp_path):
        path = tmp_path / "storage_state.json"
        path.write_text("{}")
        assert auth_module._is_recently_rotated(path) is True

    def test_age_just_inside_window_is_recent(self, tmp_path):
        path = tmp_path / "storage_state.json"
        path.write_text("{}")
        _stale_storage(path, age_seconds=auth_module._KEEPALIVE_RATE_LIMIT_SECONDS - 1.0)
        assert auth_module._is_recently_rotated(path) is True

    def test_age_just_past_window_is_not_recent(self, tmp_path):
        path = tmp_path / "storage_state.json"
        path.write_text("{}")
        _stale_storage(path, age_seconds=auth_module._KEEPALIVE_RATE_LIMIT_SECONDS + 1.0)
        assert auth_module._is_recently_rotated(path) is False

    def test_future_mtime_is_not_recent(self, tmp_path):
        """A future mtime (clock skew, NTP step) must not wedge the guard."""
        path = tmp_path / "storage_state.json"
        path.write_text("{}")
        future = path.stat().st_mtime + 3600
        os.utime(path, (future, future))
        assert auth_module._is_recently_rotated(path) is False


class TestPokeConcurrencyThrottling:
    """In-process and cross-process throttling of ``_poke_session``."""

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_concurrent_async_callers_share_single_post(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """``asyncio.gather`` over 10 fresh callers must fire exactly one POST.

        The disk mtime guard alone can't do this — none of the callers have
        written storage_state.json yet, so they all see the same stale mtime.
        The in-process ``asyncio.Lock`` + monotonic timestamp is what dedupes
        them.
        """
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        # Backdate so the disk mtime fast path doesn't pre-empt the poke.
        _stale_storage(storage_path, age_seconds=120)

        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=200,
            is_reusable=True,
        )

        async with httpx.AsyncClient() as client:
            await asyncio.gather(
                *(auth_module._poke_session(client, storage_path) for _ in range(10))
            )

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1, (
            f"expected exactly one RotateCookies POST across 10 concurrent callers, "
            f"got {len(poke_requests)}"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_skips_when_external_process_holds_flock(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """If another process holds the ``.rotate.lock`` flock, skip silently."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        # Simulate an external process holding the flock by making the
        # non-blocking acquire raise the real contention errno. Generic
        # ``OSError`` would be treated as "lock infrastructure unavailable"
        # and fail open instead — this test must mimic actual contention.
        import errno as _errno

        if sys.platform == "win32":
            import msvcrt

            def fail_lock(*_args, **_kwargs):
                raise OSError(_errno.EWOULDBLOCK, "simulated external lock holder")

            monkeypatch.setattr(msvcrt, "locking", fail_lock)
        else:
            import fcntl

            original_flock = fcntl.flock

            def maybe_fail(fd, op):
                if op & fcntl.LOCK_NB:
                    raise OSError(_errno.EWOULDBLOCK, "simulated external lock holder")
                return original_flock(fd, op)

            monkeypatch.setattr(fcntl, "flock", maybe_fail)

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert poke_requests == [], (
            "expected no RotateCookies POST when another process holds the rotation lock"
        )

    def test_rotation_lock_path_is_sibling_of_storage(self, tmp_path):
        """Lock sentinel sits next to the storage file with a ``.rotate.lock`` suffix."""
        storage_path = tmp_path / "storage_state.json"
        lock_path = auth_module._rotation_lock_path(storage_path)
        assert lock_path == tmp_path / ".storage_state.json.rotate.lock"

    def test_rotation_lock_path_returns_none_for_no_storage(self):
        assert auth_module._rotation_lock_path(None) is None

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_flock_released_after_poke(self, tmp_path, httpx_mock: HTTPXMock):
        """A successful poke releases the rotation flock so the next call can acquire."""
        if sys.platform == "win32":
            pytest.skip("POSIX-specific test; Windows uses msvcrt.locking")

        import fcntl

        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=200,
            is_reusable=True,
        )

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, storage_path)

        # After the poke, an external attempt to acquire LOCK_EX | LOCK_NB
        # should succeed — proving we released our hold.
        lock_path = auth_module._rotation_lock_path(storage_path)
        assert lock_path is not None and lock_path.exists()
        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_rotate_cookies_honours_disable_env(self, monkeypatch, httpx_mock: HTTPXMock):
        """``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` short-circuits the bare path too.

        Layer-1 ``_poke_session`` already honoured the env var, but the
        layer-2 keepalive loop bypasses ``_poke_session`` and calls
        ``_rotate_cookies`` directly. Without the env-var check on the bare
        function, setting the variable would silently fail to disable the
        background loop.
        """
        monkeypatch.setenv(NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV, "1")

        async with httpx.AsyncClient() as client:
            await auth_module._rotate_cookies(client)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert poke_requests == [], (
            "_rotate_cookies must short-circuit when NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_failed_poke_blocks_in_process_retries_within_window(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """A failed POST must still consume the rate-limit window.

        Otherwise 10 fanned-out callers would each wait the full 15 s timeout
        against a hung accounts.google.com — sequential failure stampede.
        """
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=503,
            is_reusable=True,
        )

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, storage_path)
            _stale_storage(storage_path, age_seconds=120)
            await auth_module._poke_session(client, storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1, (
            f"failed poke must still bump the in-process attempt timestamp; "
            f"got {len(poke_requests)} POSTs (the second should have skipped)"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_per_profile_timestamp_does_not_cross_profiles(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """A poke against profile A must not suppress profile B for the window.

        Multi-profile setups (``~/.notebooklm/profiles/<name>/storage_state.json``)
        are first-class. With a single global timestamp, a CLI invocation under
        profile ``work`` would silence rotation for profile ``personal`` for
        the next minute.
        """
        profile_a = tmp_path / "a" / "storage_state.json"
        profile_b = tmp_path / "b" / "storage_state.json"
        for path in (profile_a, profile_b):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"cookies": []}))
            _stale_storage(path, age_seconds=120)

        httpx_mock.add_response(url=_POKE_URL_RE, status_code=200, is_reusable=True)

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, profile_a)
            await auth_module._poke_session(client, profile_b)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 2, (
            f"each profile must rotate independently; got {len(poke_requests)} POSTs"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_timestamp_stamped_before_post_completes(self, tmp_path, httpx_mock: HTTPXMock):
        """A layer-1 caller arriving while a layer-2 POST is in flight must skip.

        L2 keepalive calls ``_rotate_cookies`` directly (no async lock); if
        the timestamp were only stamped in a ``finally`` after the await, an
        L1 caller arriving mid-flight would see a stale timestamp and fire
        its own POST. Stamping *before* the await closes that overlap.
        """
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        gate = asyncio.Event()
        entered = asyncio.Event()
        post_calls = 0

        async def slow_post(*_args, **_kwargs):
            nonlocal post_calls
            post_calls += 1
            entered.set()
            await gate.wait()
            return httpx.Response(
                200,
                request=httpx.Request("POST", auth_module.KEEPALIVE_ROTATE_URL),
            )

        async with httpx.AsyncClient() as client:
            client.post = slow_post  # type: ignore[method-assign]
            # L2-style: bare ``_rotate_cookies``, no per-profile async lock.
            task_l2 = asyncio.create_task(auth_module._rotate_cookies(client, storage_path))
            # Wait for slow_post to enter via an event rather than a timed
            # poll — busy-waits in the 100s of ms range can flake on loaded
            # CI runners (notably Windows) where the first task switch after
            # ``create_task`` doesn't always land in time.
            await asyncio.wait_for(entered.wait(), timeout=2.0)
            assert post_calls == 1, "L2 task should be parked inside slow_post"
            # L1-style: ``_poke_session`` acquires the per-profile async lock
            # (uncontended because L2 didn't take it) and reads the per-profile
            # timestamp. Claimed early, this short-circuits without a 2nd POST.
            await auth_module._poke_session(client, storage_path)
            assert post_calls == 1, (
                f"L1 fired during L2's in-flight POST; early-stamp broken (post_calls={post_calls})"
            )
            gate.set()
            await task_l2

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_concurrent_rotate_cookies_same_profile_share_single_post(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """Two layer-2-style direct ``_rotate_cookies`` calls on the same profile
        must share a single POST — verifies the atomic check-and-claim, not
        just the layer-1 async lock.
        """
        storage_path = tmp_path / "storage_state.json"

        httpx_mock.add_response(url=_POKE_URL_RE, status_code=200, is_reusable=True)

        async with httpx.AsyncClient() as client:
            # Two L2-style direct callers. Neither holds the layer-1 async
            # lock; the dedup must come from ``_try_claim_rotation``.
            await asyncio.gather(
                auth_module._rotate_cookies(client, storage_path),
                auth_module._rotate_cookies(client, storage_path),
            )

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1, (
            f"two L2 callers on the same profile must coordinate via the atomic "
            f"claim; got {len(poke_requests)} POSTs"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_lock_unavailable_fails_open(self, tmp_path, monkeypatch, httpx_mock: HTTPXMock):
        """Lock infrastructure failure must NOT permanently suppress rotation.

        On read-only auth dirs, NFS without flock support, or permission
        errors opening the sentinel, rotation should fall through to a
        best-effort POST instead of being silenced for the lifetime of the
        process.
        """
        import errno as _errno

        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        original_open = os.open
        rotate_lock = auth_module._rotation_lock_path(storage_path)

        def selective_open(path, *args, **kwargs):
            if str(path) == str(rotate_lock):
                raise OSError(_errno.EACCES, "simulated read-only auth dir")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", selective_open)
        httpx_mock.add_response(url=_POKE_URL_RE, status_code=200, is_reusable=True)

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1, (
            f"infra failure must fail open and let rotation proceed; got {len(poke_requests)} POSTs"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_in_process_timestamp_blocks_within_window(self, tmp_path, httpx_mock: HTTPXMock):
        """A second call before storage save lands still skips via the monotonic timestamp.

        Storage save happens in the caller (``_fetch_tokens_with_jar``) after
        ``_poke_session`` returns, so two successive direct calls would both
        see stale mtime. The monotonic timestamp inside the async lock catches
        the second one.
        """
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(json.dumps({"cookies": []}))
        _stale_storage(storage_path, age_seconds=120)

        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=200,
            is_reusable=True,
        )

        async with httpx.AsyncClient() as client:
            await auth_module._poke_session(client, storage_path)
            # storage_state.json mtime is intentionally NOT refreshed between
            # calls — proving the in-memory timestamp is what gates this.
            _stale_storage(storage_path, age_seconds=120)
            await auth_module._poke_session(client, storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1, (
            f"second poke should skip via monotonic timestamp; got {len(poke_requests)} POSTs"
        )


class TestKeepalivePoke:
    """Tests for the proactive ``accounts.google.com/RotateCookies`` poke."""

    @pytest.mark.asyncio
    async def test_poke_made_by_default(self, httpx_mock: HTTPXMock):
        """Token fetch hits RotateCookies before notebooklm.google.com."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens({"SID": "x", "__Secure-1PSIDTS": "test_1psidts"})

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        all_urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert len(poke_requests) == 1, (
            f"expected exactly one RotateCookies request, got: {all_urls}"
        )
        assert str(poke_requests[0].url) == KEEPALIVE_ROTATE_URL
        assert poke_requests[0].method == "POST"

    @pytest.mark.asyncio
    async def test_poke_uses_jspb_body_and_origin(self, httpx_mock: HTTPXMock):
        """Body matches the Chrome jspb sentinel; Origin is the accounts surface."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens({"SID": "x", "__Secure-1PSIDTS": "test_1psidts"})

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1
        request = poke_requests[0]
        assert request.content == b'[000,"-0000000000000000000"]'
        assert request.headers.get("content-type") == "application/json"
        assert request.headers.get("origin") == "https://accounts.google.com"

    @pytest.mark.asyncio
    async def test_poke_skipped_when_disabled(self, monkeypatch, httpx_mock: HTTPXMock):
        """``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` suppresses the poke."""
        monkeypatch.setenv(NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV, "1")
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens({"SID": "x", "__Secure-1PSIDTS": "test_1psidts"})

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert poke_requests == []

    @pytest.mark.asyncio
    async def test_poke_skipped_when_storage_recently_rotated(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        """Storage_state.json mtime within the rate-limit window suppresses the poke."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com", "path": "/"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ]
                }
            )
        )
        # storage_state.json was just written — mtime is "now", well inside the 60s window.
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens_with_domains(path=storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert poke_requests == [], (
            "rate-limit guard should skip RotateCookies when storage_state.json is fresh"
        )

    @pytest.mark.asyncio
    async def test_poke_fires_when_storage_older_than_window(self, tmp_path, httpx_mock: HTTPXMock):
        """An older storage_state.json mtime allows the rotation poke through."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com", "path": "/"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ]
                }
            )
        )
        _stale_storage(storage_path, age_seconds=120)
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens_with_domains(path=storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert len(poke_requests) == 1, "expected RotateCookies poke when storage is stale"

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_token_fetch_succeeds_when_poke_5xx(self, httpx_mock: HTTPXMock):
        """A failing poke is best-effort and never aborts token fetch."""
        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=503,
            is_reusable=True,
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        csrf, session_id = await fetch_tokens({"SID": "x", "__Secure-1PSIDTS": "test_1psidts"})

        assert csrf == "csrf_ok"
        assert session_id == "sess_ok"

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_poke_rotated_sidts_lands_in_jar(self, tmp_path, httpx_mock: HTTPXMock):
        """Set-Cookie from RotateCookies response is persisted to storage_state.json."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "SID",
                            "value": "old_sid",
                            "domain": ".google.com",
                            "path": "/",
                        },
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "stale_sidts",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ]
                }
            )
        )
        # Backdate so the rate-limit guard doesn't pre-empt the poke.
        _stale_storage(storage_path, age_seconds=120)
        httpx_mock.add_response(
            url=_POKE_URL_RE,
            status_code=200,
            headers={
                "Set-Cookie": (
                    "__Secure-1PSIDTS=ROTATED; Domain=.google.com; Path=/; Secure; HttpOnly"
                )
            },
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens_with_domains(path=storage_path)

        rewritten = json.loads(storage_path.read_text())
        sidts_values = [c["value"] for c in rewritten["cookies"] if c["name"] == "__Secure-1PSIDTS"]
        assert sidts_values == ["ROTATED"], (
            f"expected rotated SIDTS persisted to disk, got: {sidts_values}"
        )

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_token_fetch_succeeds_when_poke_raises_httperror(self, httpx_mock: HTTPXMock):
        """Network-level HTTPError on the poke is swallowed at DEBUG; token fetch proceeds."""
        httpx_mock.add_exception(httpx.ConnectError("simulated DNS failure"), url=_POKE_URL_RE)
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        csrf, session_id = await fetch_tokens({"SID": "x", "__Secure-1PSIDTS": "test_1psidts"})

        assert csrf == "csrf_ok"
        assert session_id == "sess_ok"


# Helpers shared by enumerate_accounts tests below.
def _wiz_html_with_email(email: str) -> str:
    """Build a minimal NotebookLM-style page that embeds a user email."""
    return f'<script>window.WIZ_global_data = {{"oM1Kwf":"{email}"}};</script>'


class TestExtractEmailFromHtml:
    def test_extracts_first_email(self):
        html = _wiz_html_with_email("alice@example.com")
        assert extract_email_from_html(html) == "alice@example.com"

    def test_skips_known_non_user_addresses(self):
        # Footer-style emails come first, but the active user follows.
        html = (
            '"support@google.com" "noreply@accounts.google.com" '
            '"privacy@gmail.com" "alice@example.com"'
        )
        assert extract_email_from_html(html) == "alice@example.com"

    def test_returns_none_when_absent(self):
        assert extract_email_from_html("<html>no emails here</html>") is None

    def test_returns_none_when_only_blacklisted(self):
        html = '"support@google.com" "noreply@google.com"'
        assert extract_email_from_html(html) is None

    def test_blacklist_does_not_drop_workspace_local_parts(self):
        # ``support@customer.com`` is a legitimate Workspace user. Local-part
        # blacklist must be scoped to google-owned domains. Regression for
        # PR #364 review feedback.
        html = '"support@customer.com"'
        assert extract_email_from_html(html) == "support@customer.com"


class TestEnumerateAccounts:
    """Test multi-account discovery via authuser=N probing."""

    @pytest.mark.asyncio
    async def test_single_account(self, httpx_mock: HTTPXMock):
        """One signed-in account: authuser=0 returns it, authuser=1 falls back to it."""
        default_html = _wiz_html_with_email("alice@example.com")
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=0", content=default_html.encode()
        )
        # Silent fallback: authuser=1 returns the same email; loop must stop.
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=1", content=default_html.encode()
        )

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=3)

        assert accounts == [Account(authuser=0, email="alice@example.com", is_default=True)]

    @pytest.mark.asyncio
    async def test_multiple_accounts_stops_on_silent_fallback(self, httpx_mock: HTTPXMock):
        """Three real accounts; authuser=3 silently returns account 0's email."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=0",
            content=_wiz_html_with_email("alice@example.com").encode(),
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=1",
            content=_wiz_html_with_email("bob@gmail.com").encode(),
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=2",
            content=_wiz_html_with_email("carol@workspace.com").encode(),
        )
        # Silent fallback at index 3: matches default email → enumeration stops.
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=3",
            content=_wiz_html_with_email("alice@example.com").encode(),
        )

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=5)

        assert accounts == [
            Account(authuser=0, email="alice@example.com", is_default=True),
            Account(authuser=1, email="bob@gmail.com", is_default=False),
            Account(authuser=2, email="carol@workspace.com", is_default=False),
        ]

    @pytest.mark.asyncio
    async def test_raises_when_authuser_zero_unauthenticated(self, httpx_mock: HTTPXMock):
        """Bare cookies → authuser=0 redirects to login → ValueError."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=0",
            status_code=302,
            headers={"Location": "https://accounts.google.com/signin"},
        )
        httpx_mock.add_response(
            url="https://accounts.google.com/signin", content=b"<html>Login</html>"
        )

        jar = httpx.Cookies()
        with pytest.raises(ValueError, match="Authentication expired or invalid"):
            await enumerate_accounts(jar)

    @pytest.mark.asyncio
    async def test_stops_when_subsequent_index_unparseable(self, httpx_mock: HTTPXMock):
        """authuser=N>0 with no parseable email → end-of-list, not error."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=0",
            content=_wiz_html_with_email("alice@example.com").encode(),
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=1", content=b"<html>nothing</html>"
        )

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=3)

        assert len(accounts) == 1
        assert accounts[0].email == "alice@example.com"

    @pytest.mark.asyncio
    async def test_respects_max_authuser_cap(self, httpx_mock: HTTPXMock):
        """Caller-provided ``max_authuser`` bounds the probe loop."""
        # Each index has a unique email so the silent-fallback check never trips.
        for n in range(0, 3):
            httpx_mock.add_response(
                url=f"https://notebooklm.google.com/?authuser={n}",
                content=_wiz_html_with_email(f"user{n}@example.com").encode(),
            )

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=2)

        assert [a.authuser for a in accounts] == [0, 1, 2]


class TestAccountMetadata:
    """Persist authuser index in context.json next to storage_state.json."""

    def test_read_returns_empty_when_missing(self, tmp_path):
        from notebooklm.auth import read_account_metadata

        storage = tmp_path / "storage_state.json"
        assert read_account_metadata(storage) == {}

    def test_read_returns_empty_when_storage_path_is_none(self):
        from notebooklm.auth import read_account_metadata

        assert read_account_metadata(None) == {}

    def test_get_authuser_defaults_to_zero(self, tmp_path):
        from notebooklm.auth import get_authuser_for_storage

        storage = tmp_path / "storage_state.json"
        assert get_authuser_for_storage(storage) == 0

    def test_get_authuser_reads_persisted_value(self, tmp_path):
        from notebooklm.auth import get_authuser_for_storage, write_account_metadata

        storage = tmp_path / "storage_state.json"
        write_account_metadata(storage, authuser=2, email="bob@gmail.com")
        assert get_authuser_for_storage(storage) == 2

    def test_write_updates_context_file_with_metadata(self, tmp_path):
        from notebooklm.auth import read_account_metadata, write_account_metadata

        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps({"notebook_id": "nb_existing"}), encoding="utf-8"
        )
        write_account_metadata(storage, authuser=1, email="alice@example.com")
        meta = read_account_metadata(storage)
        assert meta == {"authuser": 1, "email": "alice@example.com"}
        assert json.loads((tmp_path / "context.json").read_text()) == {
            "notebook_id": "nb_existing",
            "account": {"authuser": 1, "email": "alice@example.com"},
        }

    def test_get_authuser_ignores_negative(self, tmp_path):
        from notebooklm.auth import get_authuser_for_storage

        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": -1}}), encoding="utf-8"
        )
        assert get_authuser_for_storage(storage) == 0

    def test_get_authuser_ignores_non_int(self, tmp_path):
        from notebooklm.auth import get_authuser_for_storage

        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": "1"}}), encoding="utf-8"
        )
        assert get_authuser_for_storage(storage) == 0

    def test_read_returns_empty_for_malformed_json(self, tmp_path):
        from notebooklm.auth import read_account_metadata

        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text("not json", encoding="utf-8")
        assert read_account_metadata(storage) == {}

    def test_clear_account_metadata_preserves_notebook_context(self, tmp_path):
        from notebooklm.auth import clear_account_metadata

        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps(
                {
                    "notebook_id": "nb_existing",
                    "account": {"authuser": 1, "email": "alice@example.com"},
                }
            ),
            encoding="utf-8",
        )

        clear_account_metadata(storage)

        assert json.loads((tmp_path / "context.json").read_text()) == {"notebook_id": "nb_existing"}


class TestAuthuserPlumbing:
    """fetch_tokens_with_domains must honor account routing in context.json."""

    @pytest.mark.asyncio
    async def test_fetch_tokens_with_domains_prefers_persisted_email(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        from notebooklm.auth import fetch_tokens_with_domains, write_account_metadata

        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {"name": "HSID", "value": "x", "domain": ".google.com"},
                        {"name": "SSID", "value": "x", "domain": ".google.com"},
                        {"name": "APISID", "value": "x", "domain": ".google.com"},
                        {"name": "SAPISID", "value": "x", "domain": ".google.com"},
                        {"name": "__Secure-1PSIDTS", "value": "x", "domain": ".google.com"},
                    ]
                }
            )
        )
        write_account_metadata(storage, authuser=2, email="bob@example.com")

        # Token fetch must use the stable email route, not the reorder-prone
        # integer index.
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=bob%40example.com",
            content=b'"SNlM0e":"csrf_v2" "FdrFJe":"sess_v2"',
        )

        csrf, session_id = await fetch_tokens_with_domains(storage)
        assert csrf == "csrf_v2"
        assert session_id == "sess_v2"
        # If pytest-httpx had to fall back to a default match, the assert above
        # would fail; the explicit URL match is the contract.

    @pytest.mark.asyncio
    async def test_fetch_tokens_with_domains_uses_explicit_authuser_without_email(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        from notebooklm.auth import fetch_tokens_with_domains

        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {"name": "HSID", "value": "x", "domain": ".google.com"},
                        {"name": "SSID", "value": "x", "domain": ".google.com"},
                        {"name": "APISID", "value": "x", "domain": ".google.com"},
                        {"name": "SAPISID", "value": "x", "domain": ".google.com"},
                        {"name": "__Secure-1PSIDTS", "value": "x", "domain": ".google.com"},
                    ]
                }
            )
        )
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=2",
            content=b'"SNlM0e":"csrf_v2" "FdrFJe":"sess_v2"',
        )

        csrf, session_id = await fetch_tokens_with_domains(storage, authuser=2)
        assert csrf == "csrf_v2"
        assert session_id == "sess_v2"

    @pytest.mark.asyncio
    async def test_explicit_authuser_overrides_persisted_email(
        self, tmp_path, httpx_mock: HTTPXMock
    ):
        from notebooklm.auth import fetch_tokens_with_domains, write_account_metadata

        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {"name": "HSID", "value": "x", "domain": ".google.com"},
                        {"name": "SSID", "value": "x", "domain": ".google.com"},
                        {"name": "APISID", "value": "x", "domain": ".google.com"},
                        {"name": "SAPISID", "value": "x", "domain": ".google.com"},
                        {"name": "__Secure-1PSIDTS", "value": "x", "domain": ".google.com"},
                    ]
                }
            )
        )
        write_account_metadata(storage, authuser=2, email="bob@example.com")
        httpx_mock.add_response(
            url="https://notebooklm.google.com/?authuser=0",
            content=b'"SNlM0e":"csrf_v2" "FdrFJe":"sess_v2"',
        )

        csrf, session_id = await fetch_tokens_with_domains(storage, authuser=0)
        assert csrf == "csrf_v2"
        assert session_id == "sess_v2"
