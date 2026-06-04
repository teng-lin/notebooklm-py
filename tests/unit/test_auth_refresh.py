"""Tests for auth token refresh and fetch_tokens (split from tests/unit/test_auth.py for D1 PR-2).

This file owns one concern from the auth subpackage. The original
``tests/unit/test_auth.py`` (4090 LOC) was split into six concern-aligned
files alongside the deletion of ``_AuthFacadeModule``; see ADR-0003
(superseded) and ADR-0007 (test-monkeypatch policy) for the rationale.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm._auth import refresh as _auth_refresh
from notebooklm.auth import (
    AuthTokens,
    extract_cookies_with_domains,
    fetch_tokens,
    fetch_tokens_with_domains,
    save_cookies_to_storage,
    snapshot_cookie_jar,
)


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
    async def test_fetch_tokens_redirect_to_login_strips_query_and_fragment(self, monkeypatch):
        """Redirect error must not expose query params or fragments from final_url."""

        async def fake_poke_session(client, storage_path):
            return None

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                self.cookies = httpx.Cookies()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, *args, **kwargs):
                request = httpx.Request(
                    "GET",
                    "https://accounts.google.com/signin?continue=foo&f.sid=bar#access_token=frag",
                )
                return httpx.Response(200, content=b"<html>Login</html>", request=request)

        # Seam-aliased object-attribute patches (ADR-0007): patches the owning
        # ``_auth.refresh`` module so bare-name lookups inside
        # ``_fetch_tokens_with_jar`` observe the fakes.
        monkeypatch.setattr(_auth_refresh, "_poke_session", fake_poke_session)
        monkeypatch.setattr(_auth_refresh.httpx, "AsyncClient", FakeAsyncClient)

        with pytest.raises(ValueError) as excinfo:
            await _auth_refresh._fetch_tokens_with_jar(httpx.Cookies(), storage_path=None)

        message = str(excinfo.value)
        assert "continue=foo" not in message
        assert "f.sid=bar" not in message
        assert "access_token=frag" not in message
        # Path is redacted for Google auth hosts so a future redirect format
        # that embeds a token in the path segment cannot leak. The host
        # itself survives so operators still see the auth-host signal.
        assert "https://accounts.google.com/<redacted>" in message
        assert "/signin" not in message

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
        sid_cookie = next(
            c
            for c in storage_state["cookies"]
            if c["name"] == "SID" and c["domain"] == ".google.com"
        )
        assert sid_cookie["value"] == "new"


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
        # Seam-aliased patch (ADR-0007): ``_auth.refresh`` imports
        # ``get_storage_path`` at module top, so patching the owning module
        # reaches the bare-name call site.
        monkeypatch.setattr(_auth_refresh, "get_storage_path", lambda profile=None: storage_file)

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
        # Seam-aliased patch (ADR-0007): ``_auth.refresh`` imports
        # ``get_storage_path`` at module top, so patching the owning module
        # reaches the bare-name call site.
        monkeypatch.setattr(_auth_refresh, "get_storage_path", lambda profile=None: storage_file)

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
