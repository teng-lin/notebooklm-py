"""Tests for authentication module."""

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm import auth as auth_module
from notebooklm.auth import (
    KEEPALIVE_ROTATE_URL,
    NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV,
    AuthTokens,
    build_httpx_cookies_from_storage,
    convert_rookiepy_cookies_to_storage_state,
    extract_cookies_from_storage,
    extract_cookies_with_domains,
    extract_csrf_from_html,
    extract_session_id_from_html,
    fetch_tokens,
    fetch_tokens_with_domains,
    load_auth_from_storage,
    load_httpx_cookies,
    save_cookies_to_storage,
)


class TestAuthTokens:
    def test_dataclass_fields(self):
        """Test AuthTokens has required fields."""
        tokens = AuthTokens(
            cookies={"SID": "abc", "HSID": "def"},
            csrf_token="csrf123",
            session_id="sess456",
        )
        assert tokens.cookies == {
            ("SID", ".google.com"): "abc",
            ("HSID", ".google.com"): "def",
        }
        assert tokens.flat_cookies == {"SID": "abc", "HSID": "def"}
        assert tokens.csrf_token == "csrf123"
        assert tokens.session_id == "sess456"

    def test_cookie_header(self):
        """Test generating cookie header string."""
        tokens = AuthTokens(
            cookies={"SID": "abc", "HSID": "def"},
            csrf_token="csrf123",
            session_id="sess456",
        )
        header = tokens.cookie_header
        assert "SID=abc" in header
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
                {"name": "SID", "value": "second", "domain": ".google.com"},
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
                {"name": "HSID", "value": "hsid", "domain": ".google.com"},
                {"name": "SSID", "value": "ssid", "domain": ".google.com"},
                {"name": "APISID", "value": "apisid", "domain": ".google.com"},
                {"name": "SAPISID", "value": "sapisid", "domain": ".google.com"},
            ]
        }
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_auth_from_storage(storage_file)

        assert cookies["SID"] == "sid"
        assert len(cookies) == 5

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
        env_storage = {"cookies": [{"name": "SID", "value": "from_env", "domain": ".google.com"}]}
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(env_storage))

        # Create file with different value
        file_storage = {"cookies": [{"name": "SID", "value": "from_file", "domain": ".google.com"}]}
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
        env_storage = {"cookies": [{"name": "SID", "value": "from_env", "domain": ".google.com"}]}
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(env_storage))

        # Set NOTEBOOKLM_HOME to tmp_path and create a file there
        monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
        file_storage = {
            "cookies": [{"name": "SID", "value": "from_home_file", "domain": ".google.com"}]
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
                {"name": "SID", "value": "sid_val", "domain": ".google.com"},  # Valid
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
            ]
        }
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(env_storage))

        # Create file with different value
        file_storage = {
            "cookies": [
                {"name": "SID", "value": "from_file", "domain": ".google.com"},
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
        for cookie in jar.jar:
            if cookie.name == "SID":
                cookie.value = "rotated-sid"
        save_cookies_to_storage(jar, storage_file)

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
        save_cookies_to_storage(jar, storage_file)

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

        for cookie in jar.jar:
            if cookie.name == "__Host-GAPS":
                cookie.value = "rotated-gaps"
        save_cookies_to_storage(jar, storage_file)

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
                }
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
                {"value": "no_name_value", "domain": ".google.com"},  # Missing name
                {"name": "", "value": "empty_name", "domain": ".google.com"},  # Empty name
            ]
        }

        cookies = extract_cookies_from_storage(storage_state)
        assert "SID" in cookies
        assert len(cookies) == 1  # Only SID should be extracted

    def test_handles_cookie_with_empty_value(self):
        """Test handles cookies with empty values."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "", "domain": ".google.com"},
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

        cookies = {"SID": "test_sid"}
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

        cookies = {"SID": "expired_sid"}
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
            json.dumps({"cookies": [{"name": "SID", "value": "sid", "domain": ".google.com"}]})
        )

        jar = httpx.Cookies()
        jar.set("SID", "sid", domain=".google.com")
        jar.set("ACCOUNT_REFRESH", "fresh", domain=".accounts.google.com")

        save_cookies_to_storage(jar, storage_file)

        storage_state = json.loads(storage_file.read_text())
        assert (
            "ACCOUNT_REFRESH",
            ".accounts.google.com",
        ) in extract_cookies_with_domains(storage_state)

    def test_save_cookies_to_storage_preserves_secure_permissions(self, tmp_path):
        """Cookie sync keeps storage_state.json at 0o600 on POSIX."""
        if os.name == "nt":
            pytest.skip("POSIX permission bits are not meaningful on Windows")

        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(
            json.dumps({"cookies": [{"name": "SID", "value": "old", "domain": ".google.com"}]})
        )
        storage_file.chmod(0o600)

        jar = httpx.Cookies()
        jar.set("SID", "new", domain=".google.com")

        save_cookies_to_storage(jar, storage_file)

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
            await fetch_tokens({"SID": "stale"})

    @pytest.mark.asyncio
    async def test_refresh_retries_once_and_succeeds(
        self, tmp_path, monkeypatch, httpx_mock: HTTPXMock
    ):
        """On auth failure, runs refresh cmd, reloads cookies, retries successfully."""
        # Stage 1: write a stale cookie file
        storage_file = tmp_path / "storage_state.json"
        storage_file.write_text(
            json.dumps({"cookies": [{"name": "SID", "value": "stale", "domain": ".google.com"}]})
        )
        monkeypatch.setattr("notebooklm.auth.get_storage_path", lambda profile=None: storage_file)

        # Refresh command rewrites the file with a fresh SID
        fresh_file = tmp_path / "fresh_cookies.json"
        fresh_file.write_text(
            json.dumps({"cookies": [{"name": "SID", "value": "fresh", "domain": ".google.com"}]})
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

        cookies = {"SID": "stale"}
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
            json.dumps({"cookies": [{"name": "SID", "value": "stale", "domain": ".google.com"}]})
        )

        fresh_file = tmp_path / "fresh_cookies.json"
        fresh_file.write_text(
            json.dumps({"cookies": [{"name": "SID", "value": "fresh", "domain": ".google.com"}]})
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

        cookies = {"SID": "stale"}
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
            json.dumps({"cookies": [{"name": "SID", "value": "stale", "domain": ".google.com"}]})
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
            json.dumps({"cookies": [{"name": "SID", "value": "stale", "domain": ".google.com"}]})
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

        cookies = {"SID": "stale"}
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
            json.dumps({"cookies": [{"name": "SID", "value": "fresh", "domain": ".google.com"}]})
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
            json.dumps({"cookies": [{"name": "SID", "value": "stale", "domain": ".google.com"}]})
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
            await fetch_tokens({"SID": "stale"})
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
            await fetch_tokens({"SID": "stale"})
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
            ]
        }
        storage_file.write_text(json.dumps(storage_state))

        # Mock token fetch
        html = '"SNlM0e":"csrf_token" "FdrFJe":"session_id"'
        httpx_mock.add_response(content=html.encode())

        tokens = await AuthTokens.from_storage(storage_file)

        assert tokens.cookies[("SID", ".google.com")] == "sid"
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
    """Test allowed cookie domains constant."""

    def test_allowed_cookie_domains(self):
        """Test ALLOWED_COOKIE_DOMAINS contains expected domains."""
        from notebooklm.auth import ALLOWED_COOKIE_DOMAINS

        # Single set-difference assertion. CodeQL's
        # py/incomplete-url-substring-sanitization heuristic flags per-line
        # ``"<literal>" in ALLOWED_COOKIE_DOMAINS`` patterns as if they were
        # substring sanitization of a URL, even though this is set-membership
        # against a constant. The set-diff form has no string-in-string
        # appearance and reads at least as clearly.
        expected = {
            # Core NotebookLM/Google auth domains
            ".google.com",
            "google.com",
            ".notebooklm.google.com",
            "notebooklm.google.com",
            ".googleusercontent.com",
            "accounts.google.com",
            ".accounts.google.com",
            # Sibling Google product domains added in issue #360
            ".youtube.com",
            "youtube.com",
            "accounts.youtube.com",
            ".accounts.youtube.com",
            "drive.google.com",
            ".drive.google.com",
            "docs.google.com",
            ".docs.google.com",
            "myaccount.google.com",
            ".myaccount.google.com",
            "mail.google.com",
            ".mail.google.com",
        }
        missing = expected - ALLOWED_COOKIE_DOMAINS
        assert not missing, f"ALLOWED_COOKIE_DOMAINS is missing: {missing}"


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

    def test_accepts_sibling_google_products(self):
        """Test accepts sibling Google product domains (issue #360)."""
        from notebooklm.auth import _is_allowed_auth_domain

        # YouTube
        assert _is_allowed_auth_domain(".youtube.com") is True
        assert _is_allowed_auth_domain("youtube.com") is True
        assert _is_allowed_auth_domain("accounts.youtube.com") is True
        assert _is_allowed_auth_domain(".accounts.youtube.com") is True
        # Drive / Docs / myaccount / mail
        assert _is_allowed_auth_domain("drive.google.com") is True
        assert _is_allowed_auth_domain(".drive.google.com") is True
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
            ("notebooklm.google.com", 2),
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

    def test_accepts_sibling_google_products(self):
        """Test accepts sibling Google product domains (issue #360)."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain(".youtube.com") is True
        assert _is_allowed_cookie_domain("youtube.com") is True
        assert _is_allowed_cookie_domain("accounts.youtube.com") is True
        assert _is_allowed_cookie_domain(".accounts.youtube.com") is True
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
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_sg"

        # Test .google.co.XX
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_uk", "domain": ".google.co.uk"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_uk"

        # Test .google.XX
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_de", "domain": ".google.de"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_de"

    def test_extracts_multiple_regional_cookies(self):
        """Test extracts cookies from multiple regional domains."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_au", "domain": ".google.com.au"},
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
                {"name": "SID", "value": "sid_regional", "domain": ".google.com.sg"},
            ]
        }
        cookies = extract_cookies_from_storage(storage_state)
        assert cookies["SID"] == "sid_global", ".google.com should win (base first)"

        # Test case 2: regional domain listed first (this was the bug scenario)
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_regional", "domain": ".google.com.sg"},
                {"name": "SID", "value": "sid_global", "domain": ".google.com"},
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
                {"name": "SID", "value": "regional_sid", "domain": ".google.com.sg"},
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
            {"name": "SID", "value": "sid_sg", "domain": ".google.com.sg"},
            {"name": "SID", "value": "sid_de", "domain": ".google.de"},
        ]

        results = set()
        for ordering in permutations(base_cookies):
            storage_state = {"cookies": list(ordering)}
            cookies = extract_cookies_from_storage(storage_state)
            results.add(cookies["SID"])

        # All permutations should produce the same result: .google.com wins
        assert results == {
            "sid_base"
        }, f"Extraction should be deterministic, but got different results: {results}"

    def test_regional_only_uses_first_encountered(self):
        """Test behavior when only regional domains exist (no .google.com).

        When .google.com is not present, we use whatever cookie we encounter.
        This documents the expected fallback behavior.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "sid_sg", "domain": ".google.com.sg"},
                {"name": "SID", "value": "sid_de", "domain": ".google.de"},
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
            ]
        }
        storage_file = tmp_path / "storage.json"
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_httpx_cookies(path=storage_file)
        assert cookies.get("SID", domain=".google.de") == "sid_de"


class TestSiblingGoogleProductExtraction:
    """Test cookie extraction from sibling Google product domains (issue #360).

    Pre-#360 the auth allowlist was strictly NotebookLM-shaped: cookies on
    ``.youtube.com``, ``drive.google.com``, ``docs.google.com``,
    ``myaccount.google.com``, and ``.mail.google.com`` were dropped at
    extraction. The unified allowlist now keeps them so future flows that
    traverse those domains have the cookies they need.
    """

    SIBLING_DOMAINS = [
        ".youtube.com",
        "youtube.com",
        "accounts.youtube.com",
        ".accounts.youtube.com",
        "drive.google.com",
        ".drive.google.com",
        "docs.google.com",
        ".docs.google.com",
        "myaccount.google.com",
        ".myaccount.google.com",
        "mail.google.com",
        ".mail.google.com",
    ]

    @pytest.mark.parametrize("domain", SIBLING_DOMAINS)
    def test_extract_cookies_with_domains_keeps_sibling_cookies(self, domain):
        """``extract_cookies_with_domains`` retains sibling-product cookies."""
        storage_state = {
            "cookies": [
                # Required SID on .google.com so extraction doesn't fail
                {"name": "SID", "value": "base_sid", "domain": ".google.com"},
                # Sibling-product cookie that pre-#360 would have been dropped
                {"name": "PRODUCT_TOKEN", "value": "sibling", "domain": domain},
            ]
        }
        cookie_map = extract_cookies_with_domains(storage_state)
        assert ("PRODUCT_TOKEN", domain) in cookie_map
        assert cookie_map[("PRODUCT_TOKEN", domain)] == "sibling"

    @pytest.mark.parametrize("domain", SIBLING_DOMAINS)
    def test_load_httpx_cookies_keeps_sibling_cookies(self, tmp_path, domain):
        """``load_httpx_cookies`` (download path) accepts sibling-product cookies."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "base_sid", "domain": ".google.com"},
                {"name": "PRODUCT_TOKEN", "value": "sibling", "domain": domain},
            ]
        }
        storage_file = tmp_path / "storage.json"
        storage_file.write_text(json.dumps(storage_state))

        cookies = load_httpx_cookies(path=storage_file)
        assert cookies.get("PRODUCT_TOKEN", domain=domain) == "sibling"

    @pytest.mark.parametrize("domain", SIBLING_DOMAINS)
    def test_convert_rookiepy_keeps_sibling_cookies(self, domain):
        """rookiepy → storage_state conversion keeps sibling-product cookies."""
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

    def test_strict_allowlisted_domains_still_work(self):
        """Regression: pre-existing strict-allowlisted domains keep working.

        Ensures the unification didn't accidentally drop any of the original
        canonical NotebookLM auth domains.
        """
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "v1", "domain": ".google.com"},
                {"name": "HSID", "value": "v2", "domain": ".google.com"},
                {"name": "OSID", "value": "v3", "domain": "notebooklm.google.com"},
                {"name": "OSID2", "value": "v4", "domain": ".notebooklm.google.com"},
                {"name": "ACC", "value": "v5", "domain": "accounts.google.com"},
                {"name": "ACC2", "value": "v6", "domain": ".accounts.google.com"},
                {"name": "MEDIA", "value": "v7", "domain": ".googleusercontent.com"},
            ]
        }
        cookie_map = extract_cookies_with_domains(storage_state)
        assert ("SID", ".google.com") in cookie_map
        assert ("HSID", ".google.com") in cookie_map
        assert ("OSID", "notebooklm.google.com") in cookie_map
        assert ("OSID2", ".notebooklm.google.com") in cookie_map
        assert ("ACC", "accounts.google.com") in cookie_map
        assert ("ACC2", ".accounts.google.com") in cookie_map
        assert ("MEDIA", ".googleusercontent.com") in cookie_map

    def test_unified_filter_rejects_unrelated_domains(self):
        """Regression: cookies from unrelated domains are still rejected."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "v1", "domain": ".google.com"},
                {"name": "EVIL", "value": "x", "domain": ".evil.com"},
                {"name": "EVIL2", "value": "y", "domain": ".not-google.com"},
                {"name": "EVIL3", "value": "z", "domain": ".evil-google.com"},
                {"name": "EVIL4", "value": "w", "domain": ".not-youtube.com"},
            ]
        }
        cookie_map = extract_cookies_with_domains(storage_state)
        kept_names = {name for name, _ in cookie_map}
        assert kept_names == {"SID"}


class TestRookiepyDomainsCoverage:
    """Confirm ``_login_with_browser_cookies`` would request sibling domains.

    The login path constructs its rookiepy ``domains`` list from
    ``ALLOWED_COOKIE_DOMAINS + regional ccTLDs``, so adding a domain to the
    constant automatically widens what we ask the browser for. This test pins
    that contract — if someone narrows the constant later, the contract here
    flags it.
    """

    def test_allowlist_covers_sibling_products(self):
        from notebooklm.auth import ALLOWED_COOKIE_DOMAINS

        for domain in (
            ".youtube.com",
            "accounts.youtube.com",
            "drive.google.com",
            "docs.google.com",
            "myaccount.google.com",
        ):
            assert domain in ALLOWED_COOKIE_DOMAINS, (
                f"{domain!r} must be in ALLOWED_COOKIE_DOMAINS so "
                "_login_with_browser_cookies asks rookiepy for it (issue #360)"
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
        """Auth conversion keeps sibling Google product cookies (issue #360).

        Pre-#360 the auth allowlist was strict and dropped subdomains like
        ``.mail.google.com``. The unified allowlist now matches the broader
        download policy so cookies from ``.youtube.com``, ``drive.google.com``,
        ``docs.google.com``, ``myaccount.google.com``, and ``.mail.google.com``
        survive extraction.
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
                ".youtube.com",
                ".drive.google.com",
                ".docs.google.com",
                ".myaccount.google.com",
            )
        ]
        result = convert_rookiepy_cookies_to_storage_state(raw)
        kept_domains = {c["domain"] for c in result["cookies"]}
        assert kept_domains == {
            ".mail.google.com",
            ".youtube.com",
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
        assert (
            poke_requests == []
        ), "expected no RotateCookies POST when another process holds the rotation lock"

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
        assert (
            poke_requests == []
        ), "_rotate_cookies must short-circuit when NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1"

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
        assert (
            len(poke_requests) == 2
        ), f"each profile must rotate independently; got {len(poke_requests)} POSTs"

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
            assert (
                post_calls == 1
            ), f"L1 fired during L2's in-flight POST; early-stamp broken (post_calls={post_calls})"
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
            f"infra failure must fail open and let rotation proceed; "
            f"got {len(poke_requests)} POSTs"
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
        assert (
            len(poke_requests) == 1
        ), f"second poke should skip via monotonic timestamp; got {len(poke_requests)} POSTs"


class TestKeepalivePoke:
    """Tests for the proactive ``accounts.google.com/RotateCookies`` poke."""

    @pytest.mark.asyncio
    async def test_poke_made_by_default(self, httpx_mock: HTTPXMock):
        """Token fetch hits RotateCookies before notebooklm.google.com."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens({"SID": "x"})

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        all_urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert (
            len(poke_requests) == 1
        ), f"expected exactly one RotateCookies request, got: {all_urls}"
        assert str(poke_requests[0].url) == KEEPALIVE_ROTATE_URL
        assert poke_requests[0].method == "POST"

    @pytest.mark.asyncio
    async def test_poke_uses_jspb_body_and_origin(self, httpx_mock: HTTPXMock):
        """Body matches the Chrome jspb sentinel; Origin is the accounts surface."""
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens({"SID": "x"})

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

        await fetch_tokens({"SID": "x"})

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
                {"cookies": [{"name": "SID", "value": "x", "domain": ".google.com", "path": "/"}]}
            )
        )
        # storage_state.json was just written — mtime is "now", well inside the 60s window.
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        await fetch_tokens_with_domains(path=storage_path)

        poke_requests = [r for r in httpx_mock.get_requests() if _POKE_URL_RE.match(str(r.url))]
        assert (
            poke_requests == []
        ), "rate-limit guard should skip RotateCookies when storage_state.json is fresh"

    @pytest.mark.asyncio
    async def test_poke_fires_when_storage_older_than_window(self, tmp_path, httpx_mock: HTTPXMock):
        """An older storage_state.json mtime allows the rotation poke through."""
        storage_path = tmp_path / "storage_state.json"
        storage_path.write_text(
            json.dumps(
                {"cookies": [{"name": "SID", "value": "x", "domain": ".google.com", "path": "/"}]}
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

        csrf, session_id = await fetch_tokens({"SID": "x"})

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
        assert sidts_values == [
            "ROTATED"
        ], f"expected rotated SIDTS persisted to disk, got: {sidts_values}"

    @pytest.mark.asyncio
    @pytest.mark.no_default_keepalive_mock
    async def test_token_fetch_succeeds_when_poke_raises_httperror(self, httpx_mock: HTTPXMock):
        """Network-level HTTPError on the poke is swallowed at DEBUG; token fetch proceeds."""
        httpx_mock.add_exception(httpx.ConnectError("simulated DNS failure"), url=_POKE_URL_RE)
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=_NOTEBOOKLM_HOMEPAGE_HTML,
        )

        csrf, session_id = await fetch_tokens({"SID": "x"})

        assert csrf == "csrf_ok"
        assert session_id == "sess_ok"
