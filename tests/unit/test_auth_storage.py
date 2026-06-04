"""Tests for auth storage and loader behavior (split from tests/unit/test_auth.py for D1 PR-2).

This file owns one concern from the auth subpackage. The original
``tests/unit/test_auth.py`` (4090 LOC) was split into six concern-aligned
files alongside the deletion of ``_AuthFacadeModule``; see ADR-0003
(superseded) and ADR-0007 (test-monkeypatch policy) for the rationale.
"""

import json

import pytest
from pytest_httpx import HTTPXMock

from notebooklm.auth import (
    AuthTokens,
    load_auth_from_storage,
    load_httpx_cookies,
)


def test_load_auth_from_storage_lives_in_private_module() -> None:
    """Wave 3a of session-decoupling: load_auth_from_storage body lives in
    ``_auth/tokens.py``; ``notebooklm.auth.load_auth_from_storage`` is a
    re-export. Identity pin so a future drift (someone re-introducing a
    distinct body on ``auth.py``) fails at PR time."""
    from notebooklm import auth as auth_module
    from notebooklm._auth import tokens as tokens_module

    assert auth_module.load_auth_from_storage is tokens_module.load_auth_from_storage


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
