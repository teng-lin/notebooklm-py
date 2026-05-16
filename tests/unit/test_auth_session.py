"""Tests for the internal auth session refresh collaborator."""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from notebooklm._auth.session import refresh_auth_session
from notebooklm._core import ClientCore
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

REFRESH_HTML = '"SNlM0e":"new_csrf_token_123" "FdrFJe":"new_session_id_456"'


def _auth(**overrides: object) -> AuthTokens:
    values = {
        "cookies": {
            "SID": "test_sid",
            "__Secure-1PSIDTS": "test_1psidts",
            "HSID": "test_hsid",
        },
        "csrf_token": "old_csrf",
        "session_id": "old_session",
    }
    values.update(overrides)
    return AuthTokens(**values)


@dataclass
class RecordingRefreshCore:
    auth: AuthTokens
    http_client: httpx.AsyncClient
    operations: list[str] = field(default_factory=list)
    saved_jars: list[httpx.Cookies] = field(default_factory=list)

    def get_http_client(self) -> httpx.AsyncClient:
        return self.http_client

    async def update_auth_tokens(self, csrf: str, session_id: str) -> None:
        self.operations.append("update_auth_tokens")
        self.auth.csrf_token = csrf
        self.auth.session_id = session_id

    def update_auth_headers(self) -> None:
        self.operations.append("update_auth_headers")

    async def save_cookies(self, jar: httpx.Cookies, path: Path | None = None) -> None:
        assert path is None
        self.operations.append("save_cookies")
        self.saved_jars.append(jar)


def _client(handler: httpx.MockTransport | httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, follow_redirects=True)


@pytest.mark.asyncio
async def test_refresh_auth_session_default_account_uses_bare_base_url() -> None:
    requests: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(200, text=REFRESH_HTML, request=request)

    async with _client(httpx.MockTransport(handler)) as http_client:
        core = RecordingRefreshCore(_auth(), http_client)

        refreshed_auth = await refresh_auth_session(core)

    assert requests == [httpx.URL("https://notebooklm.google.com/")]
    assert refreshed_auth is core.auth
    assert refreshed_auth.csrf_token == "new_csrf_token_123"
    assert refreshed_auth.session_id == "new_session_id_456"
    assert core.operations == ["update_auth_tokens", "update_auth_headers", "save_cookies"]
    assert core.saved_jars == [http_client.cookies]


@pytest.mark.asyncio
async def test_refresh_auth_session_selected_account_uses_account_email_url() -> None:
    requests: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(200, text=REFRESH_HTML, request=request)

    auth = _auth(authuser=2, account_email="bob@example.com")
    async with _client(httpx.MockTransport(handler)) as http_client:
        core = RecordingRefreshCore(auth, http_client)

        await refresh_auth_session(core)

    assert requests == [httpx.URL("https://notebooklm.google.com/?authuser=bob%40example.com")]


@pytest.mark.asyncio
async def test_refresh_auth_session_selected_account_uses_authuser_url() -> None:
    requests: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(200, text=REFRESH_HTML, request=request)

    auth = _auth(authuser=2)
    async with _client(httpx.MockTransport(handler)) as http_client:
        core = RecordingRefreshCore(auth, http_client)

        await refresh_auth_session(core)

    assert requests == [httpx.URL("https://notebooklm.google.com/?authuser=2")]


@pytest.mark.asyncio
async def test_refresh_auth_session_detects_login_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "notebooklm.google.com":
            return httpx.Response(
                302,
                headers={"Location": "https://accounts.google.com/signin/v2/identifier"},
                request=request,
            )
        return httpx.Response(200, text="<html>Please sign in</html>", request=request)

    async with _client(httpx.MockTransport(handler)) as http_client:
        core = RecordingRefreshCore(_auth(), http_client)

        with pytest.raises(ValueError, match="Authentication expired"):
            await refresh_auth_session(core)

    assert core.operations == []


@pytest.mark.asyncio
async def test_refresh_auth_session_missing_csrf_wraps_extraction_error() -> None:
    html = '\n    <html>\n      "FdrFJe":"session_only"\n    </html>\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    async with _client(httpx.MockTransport(handler)) as http_client:
        core = RecordingRefreshCore(_auth(), http_client)

        with pytest.raises(ValueError) as exc_info:
            await refresh_auth_session(core)

    message = str(exc_info.value)
    assert "Failed to extract CSRF token (SNlM0e)." in message
    assert "Preview:" in message
    assert "\n" not in message
    assert core.operations == []


@pytest.mark.asyncio
async def test_refresh_auth_session_missing_session_id_wraps_extraction_error() -> None:
    html = '\n    <html>\n      "SNlM0e":"csrf_only"\n    </html>\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    async with _client(httpx.MockTransport(handler)) as http_client:
        core = RecordingRefreshCore(_auth(), http_client)

        with pytest.raises(ValueError) as exc_info:
            await refresh_auth_session(core)

    message = str(exc_info.value)
    assert "Failed to extract session ID (FdrFJe)." in message
    assert "Preview:" in message
    assert "\n" not in message
    assert core.operations == []


@pytest.mark.asyncio
async def test_refresh_auth_session_persists_through_client_core_save_cookies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_path = tmp_path / "storage_state.json"
    auth = _auth(storage_path=storage_path)
    core = ClientCore(auth)
    calls: list[tuple[Path, bool, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=REFRESH_HTML,
            headers={"Set-Cookie": "SID=fresh_sid; Domain=.google.com; Path=/"},
            request=request,
        )

    def fake_save_cookies_to_storage(
        jar: httpx.Cookies,
        path: Path,
        *,
        original_snapshot: object = None,
        return_result: bool = False,
    ) -> bool:
        assert jar.get("SID") == "fresh_sid"
        calls.append((path, return_result, original_snapshot))
        return True

    monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", fake_save_cookies_to_storage)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=auth.cookie_jar,
        follow_redirects=True,
    )
    core._http_client = http_client
    core.cookie_persistence.capture_open_snapshot(http_client.cookies)
    try:
        await refresh_auth_session(core)
    finally:
        await http_client.aclose()
        core._http_client = None

    assert len(calls) == 1
    path, return_result, original_snapshot = calls[0]
    assert path == storage_path
    assert return_result is True
    assert original_snapshot is not None


def test_client_refresh_auth_is_facade_only() -> None:
    source = textwrap.dedent(inspect.getsource(NotebookLMClient.refresh_auth))
    tree = ast.parse(source)
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef))

    calls_refresh_session = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "refresh_auth_session"
        for node in ast.walk(function)
    )
    forbidden_names = {
        "extract_wiz_field",
        "is_google_auth_redirect",
        "_get_auth_snapshot_lock",
    }
    forbidden_attrs = {
        "update_auth_tokens",
        "update_auth_headers",
        "save_cookies",
        "csrf_token",
        "session_id",
    }
    violations: list[tuple[int, str]] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and node.id in forbidden_names:
            violations.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
            violations.append((node.lineno, node.attr))

    assert calls_refresh_session
    assert violations == []


def test_auth_session_has_no_runtime_client_or_core_imports() -> None:
    path = Path(__file__).parents[2] / "src/notebooklm/_auth/session.py"
    tree = ast.parse(path.read_text())
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def inside_type_checking(node: ast.AST) -> bool:
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
                return True
        return False

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if inside_type_checking(node):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"notebooklm.client", "notebooklm._core"}:
                    violations.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_names = {alias.name for alias in node.names}
            if module in {"notebooklm.client", "notebooklm._core", "client", "_core"}:
                violations.append((node.lineno, f"from {module} import ..."))
            if (module == "notebooklm" or (node.level > 0 and module == "")) and imported_names & {
                "client",
                "_core",
            }:
                imported = ", ".join(sorted(imported_names & {"client", "_core"}))
                violations.append(
                    (node.lineno, f"from {'.' * node.level}{module} import {imported}")
                )
            if node.level > 0 and module in {"client", "_core"}:
                violations.append((node.lineno, f"from {'.' * node.level}{module} import ..."))
            forbidden_type_names = {"NotebookLMClient", "ClientCore"}
            for name in sorted(imported_names & forbidden_type_names):
                violations.append((node.lineno, name))

    assert violations == []
