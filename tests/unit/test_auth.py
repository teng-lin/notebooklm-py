"""Tests for authentication module."""

import json
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from notebooklm.auth import (
    AuthTokens,
    extract_cookies_from_storage,
    extract_csrf_from_html,
    extract_session_id_from_html,
    fetch_tokens,
    load_auth_from_storage,
    load_httpx_cookies,
)


class TestAuthTokens:
    def test_dataclass_fields(self):
        """Test AuthTokens has required fields."""
        tokens = AuthTokens(
            cookies={"SID": "abc", "HSID": "def"},
            csrf_token="csrf123",
            session_id="sess456",
        )
        assert tokens.cookies == {"SID": "abc", "HSID": "def"}
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
    async def test_fetch_tokens_includes_cookie_header(self, httpx_mock: HTTPXMock):
        """Test that fetch_tokens includes cookie header."""
        html = '"SNlM0e":"csrf" "FdrFJe":"sess"'
        httpx_mock.add_response(content=html.encode())

        cookies = {"SID": "sid_value", "HSID": "hsid_value"}
        await fetch_tokens(cookies)

        request = httpx_mock.get_request()
        cookie_header = request.headers.get("cookie", "")
        assert "SID=sid_value" in cookie_header
        assert "HSID=hsid_value" in cookie_header


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

        assert tokens.cookies["SID"] == "sid"
        assert tokens.csrf_token == "csrf_token"
        assert tokens.session_id == "session_id"

    @pytest.mark.asyncio
    async def test_from_storage_file_not_found(self, tmp_path):
        """Test raises error when storage file doesn't exist."""
        with pytest.raises(FileNotFoundError):
            await AuthTokens.from_storage(tmp_path / "nonexistent.json")


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
    """Test default storage path constant."""

    def test_default_storage_path_is_correct(self):
        """Test DEFAULT_STORAGE_PATH constant is defined correctly."""
        from notebooklm.auth import DEFAULT_STORAGE_PATH

        assert DEFAULT_STORAGE_PATH is not None
        assert isinstance(DEFAULT_STORAGE_PATH, Path)
        # Note: Don't check for ".notebooklm" since NOTEBOOKLM_HOME may be set
        # Just verify it ends with the expected filename
        assert DEFAULT_STORAGE_PATH.name == "storage_state.json"


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

        assert ".google.com" in ALLOWED_COOKIE_DOMAINS
        assert "notebooklm.google.com" in ALLOWED_COOKIE_DOMAINS


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
            # Subdomains are not matched by _is_google_domain
            "accounts.google.com",
            "lh3.google.com",
            "accounts.google.de",  # Subdomain of regional
            "lh3.google.co.uk",  # Subdomain of regional
            ".accounts.google.de",  # With leading dot
        ],
    )
    def test_rejects_subdomains(self, domain):
        """Test that subdomains are rejected by _is_google_domain.

        _is_google_domain only matches domain-level cookies (with leading dot).
        Subdomain matching is handled by _is_allowed_cookie_domain's suffix matching.
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

    def test_rejects_unrelated_domains(self):
        """Test rejects non-Google domains."""
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain(".youtube.com") is False
        assert _is_allowed_auth_domain("evil.com") is False
        assert _is_allowed_auth_domain(".evil-google.com") is False

    def test_rejects_malicious_google_lookalikes(self):
        """Test rejects domains that look like Google but aren't."""
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain("google.com.evil.sg") is False
        assert _is_allowed_auth_domain(".not-google.com.sg") is False
        assert _is_allowed_auth_domain(".google.zz") is False  # Invalid ccTLD

    def test_requires_leading_dot_for_regional(self):
        """Test regional domains must have leading dot."""
        from notebooklm.auth import _is_allowed_auth_domain

        assert _is_allowed_auth_domain("google.com.sg") is False
        assert _is_allowed_auth_domain("google.co.uk") is False
        assert _is_allowed_auth_domain("google.de") is False


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

    def test_rejects_invalid_domains(self):
        """Test rejects invalid domains."""
        from notebooklm.auth import _is_allowed_cookie_domain

        assert _is_allowed_cookie_domain(".google.zz") is False
        assert _is_allowed_cookie_domain("evil-google.com") is False
        assert _is_allowed_cookie_domain(".youtube.com") is False


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

    def test_rejects_youtube_sid_but_accepts_regional_sid(self):
        """Test rejects SID from youtube but accepts from regional Google domain."""
        storage_state = {
            "cookies": [
                # YouTube SID should be rejected (not a Google auth domain)
                {"name": "SID", "value": "youtube_sid", "domain": ".youtube.com"},
            ]
        }

        # Should fail because no valid SID from allowed domain
        with pytest.raises(ValueError, match="Missing required cookies"):
            extract_cookies_from_storage(storage_state)

        # But if we add a regional Google SID, it should work
        storage_state["cookies"].append(
            {"name": "SID", "value": "regional_sid", "domain": ".google.com.sg"}
        )
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
