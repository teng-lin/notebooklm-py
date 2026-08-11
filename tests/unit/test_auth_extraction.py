"""Tests for auth cookie/token extraction and AuthTokens dataclass (split in D1 PR-2).

This file owns one concern from the auth subpackage. The original
monolithic auth test module was split into six concern-aligned files
alongside the deletion of ``_AuthFacadeModule``; see ADR-0003
(superseded) and ADR-0007 (test-monkeypatch policy) for the rationale.
"""

import json

import pytest

from notebooklm._auth.cookies import load_httpx_cookies
from notebooklm._auth.extraction import (
    _safe_url,
    extract_csrf_from_html,
    extract_session_id_from_html,
)
from notebooklm._auth.storage import save_cookies_to_storage, snapshot_cookie_jar
from notebooklm.auth import (
    AuthTokens,
    build_httpx_cookies_from_storage,
    extract_cookies_from_storage,
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

        # Reverse the duplicate pair (indices 2/3 — both OSID entries) so the
        # no-dot variant precedes the dotted form in storage order. The dotted
        # variant must still win deterministically. Earlier versions of this
        # swap touched indices 1/2 which moved the ``__Secure-1PSIDTS``
        # sentinel instead of flipping the OSID duplicates — that left the
        # OSID order unchanged and silently weakened the regression.
        storage_state["cookies"][2], storage_state["cookies"][3] = (
            storage_state["cookies"][3],
            storage_state["cookies"][2],
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

        # Reverse the duplicate pair (indices 2/3 — both ``X`` entries) so the
        # googleusercontent entry precedes the regional one. The regional
        # cookie must still win deterministically. (See sibling-test comment
        # for why indices 1/2 was the wrong swap.)
        storage_state["cookies"][2], storage_state["cookies"][3] = (
            storage_state["cookies"][3],
            storage_state["cookies"][2],
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
        changes, but the save side appends fresh entries from the in-memory jar
        (``notebooklm._auth.storage.save_cookies_to_storage``). Without the load-side fix, those appended entries
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


class TestFinalUrlScrubbing:
    """Auth-error messages must strip query + fragment from final_url interpolations.

    Auth-handshake URLs frequently carry credential-shaped query params
    (``f.sid=...``, ``continue=...``, ``access_token=...``). Without
    sanitization these would leak into every drift-error message and the
    associated log line.
    """

    def test_final_url_stripped(self):
        """CSRF drift error must NOT include query params from final_url."""
        # No WIZ_global_data → drift path; URL is not an accounts.google.com
        # redirect so we get the "shape changed" raise that interpolates
        # final_url into the message.
        html = "<html><body>not a notebooklm page</body></html>"
        final_url = "https://x.example/y?continue=foo&f.sid=bar#access_token=frag"

        with pytest.raises(ValueError) as excinfo:
            extract_csrf_from_html(html, final_url)

        message = str(excinfo.value)
        assert "continue=foo" not in message
        assert "f.sid=bar" not in message
        assert "access_token=frag" not in message
        # The scheme/netloc/path triple should still appear so operators can
        # identify the failing endpoint.
        assert "https://x.example/y" in message

    def test_final_url_stripped_session_id_path(self):
        """Session-ID drift error must NOT include query params from final_url."""
        html = "<html><body>not a notebooklm page</body></html>"
        final_url = "https://x.example/y?continue=foo&f.sid=bar#access_token=frag"

        with pytest.raises(ValueError) as excinfo:
            extract_session_id_from_html(html, final_url)

        message = str(excinfo.value)
        assert "continue=foo" not in message
        assert "f.sid=bar" not in message
        assert "access_token=frag" not in message
        assert "https://x.example/y" in message

    def test_final_url_stripped_userinfo(self):
        """URL userinfo (``https://TOKEN@host/...``) must NOT leak.

        ``urlparse(...).netloc`` preserves the userinfo component, so a naive
        reconstruction from ``scheme://netloc/path`` would surface tokens
        carried in the ``user[:password]@`` position. ``_safe_url`` rebuilds
        from ``hostname`` + port instead.
        """
        html = "<html><body>not a notebooklm page</body></html>"
        # Token embedded as userinfo — the most adversarial leak vector.
        final_url = "https://SECRET_TOKEN_USERINFO@x.example:8443/y?q=1"

        with pytest.raises(ValueError) as excinfo:
            extract_csrf_from_html(html, final_url)

        message = str(excinfo.value)
        assert "SECRET_TOKEN_USERINFO" not in message
        # Port is preserved so operators can still identify the endpoint.
        assert "https://x.example:8443/y" in message


class TestSafeUrlGoogleAuthHosts:
    """``_safe_url`` must drop the path component for Google OAuth hosts.

    Background: Google's OAuth endpoints have historically embedded opaque
    grant codes / tokens in the URL **path** (e.g. ``/o/oauth2/auth/<token>``
    on ``accounts.google.com``). The original ``_safe_url`` stripped only
    userinfo / query / fragment — a future redirect format change that put
    a credential in the path segment would have leaked through
    ``ValueError("... Redirected to: %s" % final_url)`` (see
    ``_auth/refresh.py`` ``_fetch_tokens_with_jar``).

    These tests pin the host-restricted path-redaction so non-auth endpoints
    retain enough operator signal (host + path) to be diagnosable.
    """

    def test_accounts_google_com_path_redacted(self):
        """accounts.google.com paths get replaced with /<redacted>."""
        out = _safe_url("https://accounts.google.com/o/oauth2/auth/SECRET_GRANT_CODE?q=1")
        assert "SECRET_GRANT_CODE" not in out
        assert "/o/oauth2/auth" not in out
        assert out == "https://accounts.google.com/<redacted>"

    def test_oauth2_googleapis_com_path_redacted(self):
        """oauth2.googleapis.com paths get replaced with /<redacted>."""
        out = _safe_url("https://oauth2.googleapis.com/token/SECRET_PATH_TOKEN")
        assert "SECRET_PATH_TOKEN" not in out
        assert out == "https://oauth2.googleapis.com/<redacted>"

    def test_oauth2_googleusercontent_com_path_redacted(self):
        """oauth2.googleusercontent.com paths get replaced with /<redacted>."""
        out = _safe_url("https://oauth2.googleusercontent.com/auth/SECRET_GRANT")
        assert "SECRET_GRANT" not in out
        assert out == "https://oauth2.googleusercontent.com/<redacted>"

    def test_unrelated_googleusercontent_subdomain_path_preserved(self):
        """Non-``oauth2`` subdomains of googleusercontent.com keep their path.

        The wider ``.googleusercontent.com`` family hosts artifact downloads
        (slide decks, audio) — operator signal there is more useful than the
        narrow leak risk. Only the ``oauth2`` subdomain is in the redact set.
        """
        out = _safe_url("https://lh3.googleusercontent.com/a/AVATAR_PATH")
        assert "AVATAR_PATH" in out, f"non-auth googleusercontent path lost: {out!r}"

    def test_www_googleapis_com_path_preserved(self):
        """www.googleapis.com is NOT in the redact set (hosts many non-auth APIs)."""
        out = _safe_url("https://www.googleapis.com/drive/v3/files/foo")
        assert "/drive/v3/files/foo" in out

    def test_accounts_google_com_root_path_preserved(self):
        """Root path ``/`` on Google auth hosts is NOT redacted (no signal to leak)."""
        assert _safe_url("https://accounts.google.com/") == "https://accounts.google.com/"

    def test_accounts_google_com_subdomain_path_redacted(self):
        """Subdomains of accounts.google.com also get path-redacted."""
        out = _safe_url("https://x.accounts.google.com/o/oauth2/auth/TOKEN")
        assert "TOKEN" not in out
        assert out == "https://x.accounts.google.com/<redacted>"

    def test_non_google_host_path_preserved(self):
        """Non-auth hosts keep their path so operators can identify endpoints."""
        # Regression for ``test_final_url_stripped`` — non-Google host must
        # round-trip the path through the safe-url pipe.
        assert _safe_url("https://x.example/y") == "https://x.example/y"

    def test_google_auth_host_case_insensitive(self):
        """Host comparison must be case-insensitive (RFC 3986 §3.2.2)."""
        out = _safe_url("https://Accounts.Google.COM/o/oauth2/auth/TOK")
        assert "TOK" not in out
        # Hostname casing is preserved verbatim from ``urlparse(...).hostname``
        # which lowercases the host; the assertion targets the absence of
        # ``TOK`` regardless of host-casing rendering.

    def test_google_auth_host_query_and_path_both_stripped(self):
        """Query is dropped by the existing pipe; path is dropped by the new
        host-restricted rule. A URL with both must lose both."""
        out = _safe_url(
            "https://accounts.google.com/o/oauth2/auth/PATH_TOK?continue=https%3A%2F%2Fx.example"
        )
        assert "PATH_TOK" not in out
        assert "continue=" not in out
        assert out == "https://accounts.google.com/<redacted>"

    def test_empty_url_still_empty(self):
        """Empty input degenerates cleanly (regression for the existing pipe)."""
        assert _safe_url("") == ""


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
        """Empty required values are skipped and produce the typed failure."""
        storage_state = {
            "cookies": [
                {"name": "SID", "value": "", "domain": ".google.com"},
                {"name": "__Secure-1PSIDTS", "value": "test_1psidts", "domain": ".google.com"},
            ]
        }

        with pytest.raises(ValueError, match="Missing required cookies: SID"):
            extract_cookies_from_storage(storage_state)


class TestExtractCSRFRedirect:
    """Test CSRF extraction redirect detection."""

    def test_raises_on_redirect_to_accounts_in_url(self):
        """Test raises error when redirected to accounts.google.com (URL)."""
        html = "<html><body>Login page</body></html>"
        final_url = "https://accounts.google.com/signin"

        with pytest.raises(ValueError, match="Authentication expired"):
            extract_csrf_from_html(html, final_url)

    def test_accounts_link_in_body_alone_is_not_an_expiry(self):
        """An accounts.google.com link in the BODY must not be read as expiry (#2038).

        This previously raised "Authentication expired or invalid" via the
        ``contains_google_auth_redirect(html)`` fallback. Nearly every
        Google-served page carries such a link, so the fallback made a
        wrong-page failure indistinguishable from a real expiry. Without a
        final URL saying otherwise, the honest answer is "token not found".
        """
        html = '<html><body><a href="https://accounts.google.com/signin">Sign in</a></body></html>'

        with pytest.raises(ValueError, match="CSRF token not found") as exc:
            extract_csrf_from_html(html)
        assert "Authentication expired" not in str(exc.value)


class TestExtractSessionIdRedirect:
    """Test session ID extraction redirect detection."""

    def test_raises_on_redirect_to_accounts_in_url(self):
        """Test raises error when redirected to accounts.google.com (URL)."""
        html = "<html><body>Login page</body></html>"
        final_url = "https://accounts.google.com/signin"

        with pytest.raises(ValueError, match="Authentication expired"):
            extract_session_id_from_html(html, final_url)

    def test_accounts_link_in_body_alone_is_not_an_expiry(self):
        """Body-scan symmetry with the CSRF sibling — see that test's rationale."""
        html = '<html><body><a href="https://accounts.google.com/signin">Sign in</a></body></html>'

        with pytest.raises(ValueError, match="Session ID not found") as exc:
            extract_session_id_from_html(html)
        assert "Authentication expired" not in str(exc.value)


class TestUnavailableRedirectClassification:
    """A redirect to notebooklm.google is the region/anti-abuse gate, not a drift (#1630)."""

    _MARKETING_HTML = "<html><body>NotebookLM marketing splash — no WIZ_global_data.</body></html>"

    def test_csrf_classifies_location_unsupported_redirect(self):
        with pytest.raises(ValueError) as exc:
            extract_csrf_from_html(
                self._MARKETING_HTML, "https://notebooklm.google/?location=unsupported"
            )
        msg = str(exc.value)
        assert "region / anti-abuse access gate" in msg
        assert "location=unsupported" in msg  # the diagnostic is surfaced, not swallowed
        assert "page structure" not in msg  # NOT the misleading file-a-bug message

    def test_session_id_classifies_redirect(self):
        with pytest.raises(ValueError) as exc:
            extract_session_id_from_html(self._MARKETING_HTML, "https://notebooklm.google")
        msg = str(exc.value)
        assert "region / anti-abuse access gate" in msg
        assert "page structure" not in msg

    def test_app_host_drift_still_says_page_structure(self):
        # A token-less response from the real APP host (not the gate) keeps the
        # original "page structure" message — the gate branch must not capture it.
        with pytest.raises(ValueError, match="page structure"):
            extract_csrf_from_html(self._MARKETING_HTML, "https://notebooklm.google.com/")

    # The real notebooklm.google gate page carries an accounts.google.com sign-in
    # link; the authoritative final-URL gate check must win over the body scan,
    # otherwise it mis-routes to "Authentication expired" (the codex finding).
    _GATE_HTML_WITH_SIGNIN = (
        '<html><body>NotebookLM <a href="https://accounts.google.com/ServiceLogin">'
        "Sign in</a></body></html>"
    )

    def test_csrf_gate_wins_over_accounts_link_in_body(self):
        with pytest.raises(ValueError) as exc:
            extract_csrf_from_html(
                self._GATE_HTML_WITH_SIGNIN, "https://notebooklm.google/?location=unsupported"
            )
        msg = str(exc.value)
        assert "region / anti-abuse access gate" in msg
        assert "Authentication expired" not in msg

    def test_session_id_gate_wins_over_accounts_link_in_body(self):
        with pytest.raises(ValueError) as exc:
            extract_session_id_from_html(
                self._GATE_HTML_WITH_SIGNIN, "https://notebooklm.google/?location=unsupported"
            )
        msg = str(exc.value)
        assert "region / anti-abuse access gate" in msg
        assert "Authentication expired" not in msg

    def test_gate_message_does_not_trigger_auto_refresh(self):
        # An environmental gate must NOT match the auth-error signals that drive
        # NOTEBOOKLM_REFRESH_CMD — re-auth can't fix it, so refreshing is futile.
        # Pin it so a careless reword of the message can't silently re-enable it.
        from notebooklm._auth.refresh import _AUTH_ERROR_SIGNALS

        with pytest.raises(ValueError) as exc:
            extract_csrf_from_html(
                self._GATE_HTML_WITH_SIGNIN, "https://notebooklm.google/?location=unsupported"
            )
        lowered = str(exc.value).lower()
        assert not any(signal in lowered for signal in _AUTH_ERROR_SIGNALS)


class TestExtractionFailureTaxonomy:
    """Each distinct extraction failure must produce a distinguishable message (#2038).

    Before this taxonomy existed, the drift path fell back to
    ``contains_google_auth_redirect(html)`` — a scan that returns True for any
    ``accounts.google.com`` URL anywhere in the body. Practically every
    Google-served page qualifies, so a wrong-page failure was reported as
    "Authentication expired or invalid. Run 'notebooklm login' to
    re-authenticate." with no final URL attached.

    That is not hypothetical: the scheduled ``rpc-health`` workflow failed with
    exactly that message every day from 2026-07-28 to 2026-08-03 (#2019) while
    the credentials were valid the whole time — proven by a passing nightly
    Windows E2E run on the same ``NOTEBOOKLM_AUTH_JSON``. Three users piled onto
    the auto-filed issue assuming an unrelated login bug.

    The four fixtures below are the four real conditions. They must never
    collapse into one another again.
    """

    # 1. Genuine expiry — the chain actually landed on the login host.
    _LOGIN_HTML = "<html><body>Sign in to continue</body></html>"
    _LOGIN_URL = "https://accounts.google.com/ServiceLogin"

    # 2. Cookie mismatch — the interstitial 302s onward, so it is visible ONLY
    #    in the redirect history. This is the real #2019 chain.
    _MISMATCH_HOP = "https://accounts.google.com/CookieMismatch?continue=https%3A%2F%2Fx"
    _SUPPORT_URL = "https://support.google.com/accounts/answer/32050"

    # 3. False positive — an ordinary HTTP 200 Google help article. Its body is
    #    full of accounts.google.com links; the session is fine.
    _HELP_HTML = (
        "<html><body><h1>Why Google signed you out</h1>"
        '<a href="https://accounts.google.com/signin">Sign in</a>'
        '<a href="https://accounts.google.com/b/0/AddMailService">Add account</a>'
        "</body></html>"
    )

    # 4. Real structure change — served BY the app host, but no WIZ_global_data.
    _APP_HTML = "<html><body><div id='app'>NotebookLM</div></body></html>"
    _APP_URL = "https://notebooklm.google.com/"

    @staticmethod
    def _message(html: str, final_url: str, *redirect_urls: str) -> str:
        """Run one fixture through the classifier and return the message it raised."""
        with pytest.raises(ValueError) as exc:
            extract_csrf_from_html(html, final_url, redirect_urls=redirect_urls)
        return str(exc.value)

    def _messages(self) -> dict[str, str]:
        """Raise all four fixtures and collect their messages, keyed by condition."""
        return {
            "expiry": self._message(self._LOGIN_HTML, self._LOGIN_URL),
            "mismatch": self._message(self._HELP_HTML, self._SUPPORT_URL, self._MISMATCH_HOP),
            "false_positive": self._message(self._HELP_HTML, self._SUPPORT_URL),
            "structure": self._message(self._APP_HTML, self._APP_URL),
        }

    def test_all_four_messages_are_distinct(self):
        """The whole point: four conditions, four messages."""
        messages = self._messages()
        assert len(set(messages.values())) == 4, messages

    def test_every_message_carries_the_final_url(self):
        """Requirement 1 — the final URL is the single most useful evidence."""
        messages = self._messages()
        expected_host = {
            "expiry": "accounts.google.com",
            "mismatch": "support.google.com",
            "false_positive": "support.google.com",
            "structure": "notebooklm.google.com",
        }
        for name, message in messages.items():
            assert expected_host[name] in message, f"{name} lost the final URL: {message!r}"

    def test_genuine_expiry_still_says_expired(self):
        """The legacy substring contract survives, now with the URL attached."""
        message = self._messages()["expiry"]
        assert "Authentication expired" in message
        assert "notebooklm login" in message
        # accounts.google.com paths are credential-shaped and stay redacted.
        assert "ServiceLogin" not in message

    def test_cookie_mismatch_is_its_own_diagnostic(self):
        """Requirement 2 — a CookieMismatch hop is NOT folded into "expired"."""
        message = self._messages()["mismatch"]
        assert "CookieMismatch" in message
        assert "Authentication expired" not in message
        assert "cookie-scoping" in message
        # The ``continue=`` target is credential-shaped and must not leak.
        assert "continue=" not in message

    def test_false_positive_is_not_reported_as_expiry(self):
        """Regression for #2019: a help article is not an expired session.

        Written to FAIL against the pre-#2038 implementation, which raised
        "Authentication expired or invalid" here.
        """
        message = self._messages()["false_positive"]
        assert "Authentication expired" not in message
        assert "CSRF token not found" in message
        assert "never reached the app" in message

    def test_structure_change_still_says_page_structure(self):
        """A token-less page served BY the app host is a real drift signal."""
        message = self._messages()["structure"]
        assert "page structure has changed" in message
        assert "Authentication expired" not in message
        # ...and must NOT be mistaken for the off-host case.
        assert "never reached the app" not in message

    def test_taxonomy_is_symmetric_for_session_id(self):
        """``extract_session_id_from_html`` shares the classifier, not a copy."""
        with pytest.raises(ValueError) as exc:
            extract_session_id_from_html(
                self._HELP_HTML, self._SUPPORT_URL, redirect_urls=(self._MISMATCH_HOP,)
            )
        assert "CookieMismatch" in str(exc.value)

        with pytest.raises(ValueError) as exc:
            extract_session_id_from_html(self._HELP_HTML, self._SUPPORT_URL)
        message = str(exc.value)
        assert "Authentication expired" not in message
        assert "Session ID not found" in message

    def test_cookie_mismatch_as_final_url_also_classified(self):
        """An unfollowed chain that ENDS on the interstitial must not say "expired".

        ``final_url`` is checked alongside the history, so a caller that stopped
        at the interstitial (or a transport that reports no history) still gets
        the cookie-scoping diagnosis rather than the auth-redirect one.
        """
        with pytest.raises(ValueError) as exc:
            extract_csrf_from_html(self._LOGIN_HTML, "https://accounts.google.com/CookieMismatch")
        message = str(exc.value)
        assert "CookieMismatch" in message
        assert "Authentication expired" not in message

    def test_cookie_mismatch_still_drives_refresh_cmd(self):
        """Behaviour preservation: this case triggered NOTEBOOKLM_REFRESH_CMD before.

        Today the same chain raises "Authentication expired ... run 'notebooklm
        login'", which matches ``_AUTH_ERROR_SIGNALS`` and fires the refresh
        command. Re-extracting cookies genuinely can fix a flattened-domain jar,
        so the reworded message must keep matching — unlike the environmental
        gate message, which deliberately does not (see
        ``test_gate_message_does_not_trigger_auto_refresh``).
        """
        from notebooklm._auth.refresh import _AUTH_ERROR_SIGNALS

        message = self._messages()["mismatch"].lower()
        assert any(signal in message for signal in _AUTH_ERROR_SIGNALS)

    def test_only_confirmed_login_redirect_has_private_recovery_type(self):
        """L3/L4 can key on type without widening the public ValueError API."""
        from notebooklm._auth import extraction

        login = extraction._url_only_extraction_failure(self._LOGIN_URL, ())
        mismatch = extraction._url_only_extraction_failure(
            "https://accounts.google.com/CookieMismatch", ()
        )

        assert isinstance(login, ValueError)
        assert isinstance(login, extraction._LoginRedirectError)
        assert isinstance(mismatch, ValueError)
        assert not isinstance(mismatch, extraction._LoginRedirectError)

    def test_gate_still_wins_over_cookie_mismatch_ordering(self):
        """#1630 must not regress: the region gate is classified first."""
        with pytest.raises(ValueError) as exc:
            extract_csrf_from_html(
                self._HELP_HTML,
                "https://notebooklm.google/?location=unsupported",
                redirect_urls=(self._MISMATCH_HOP,),
            )
        assert "region / anti-abuse access gate" in str(exc.value)

    def test_redirect_urls_is_keyword_only_and_optional(self):
        """The added parameter must not break positional callers."""
        with pytest.raises(ValueError, match="CSRF token not found"):
            extract_csrf_from_html(self._APP_HTML, self._APP_URL)
        with pytest.raises(TypeError):
            extract_csrf_from_html(self._APP_HTML, self._APP_URL, (self._MISMATCH_HOP,))  # type: ignore[misc]
