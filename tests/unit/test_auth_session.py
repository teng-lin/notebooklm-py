"""Tests for the internal auth session refresh collaborator."""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._auth.session import refresh_auth_session
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from tests._fixtures.kernel_test_helpers import install_http_client_for_test
from tests._helpers.client_factory import build_client_shell_for_tests

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


class _RecordingKernel:
    """Stub kernel exposing :meth:`get_http_client`.

    Wave 2 of plan ``host-protocol-removal`` narrowed
    :func:`refresh_auth_session` to take ``kernel`` as an explicit
    keyword-only argument; this stub mirrors the production
    :class:`Kernel` shape that satisfies ``kernel.get_http_client()``
    so tests can drive the refresh path without standing up the full
    transport stack.
    """

    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._http_client = http_client

    def get_http_client(self) -> httpx.AsyncClient:
        return self._http_client


class _RecordingLifecycle:
    """Lifecycle stub matching the ``ClientLifecycle.save_cookies`` shape.

    Wave 2 of plan ``host-protocol-removal`` narrowed
    :meth:`ClientLifecycle.save_cookies` to take the
    :class:`CookiePersistence` collaborator directly as its first
    positional argument (was: a Session-shaped ``host``). The recorded
    ``cookie_persistence`` argument is forwarded as-is — for these unit
    tests it's the bundle's own ``_RecordingCookiePersistence`` stub.
    """

    def __init__(self) -> None:
        self.operations: list[str] = []
        self.saved_jars: list[httpx.Cookies] = []

    async def save_cookies(
        self,
        cookie_persistence: Any,
        jar: httpx.Cookies,
        path: Path | None = None,
    ) -> None:
        assert path is None
        self.operations.append("save_cookies")
        self.saved_jars.append(jar)


class _RecordingAuthCoord:
    """Coordinator stub recording ``update_auth_tokens`` /
    ``update_auth_headers`` calls.

    Wave 2 of plan ``host-protocol-removal`` lifted
    :func:`refresh_auth_session` off the legacy Session-shaped ``core``
    Protocol; the function now invokes
    ``auth_coord.update_auth_tokens(auth=..., csrf=..., session_id=...)``
    and ``auth_coord.update_auth_headers(auth=..., kernel=...)``
    directly. The recording stub mirrors those new keyword-only
    signatures and routes the token mutation through the shared
    ``AuthTokens`` instance, so post-refresh assertions on
    ``auth.csrf_token`` / ``.session_id`` still work.
    """

    def __init__(self, operations: list[str]) -> None:
        self._operations = operations

    async def update_auth_tokens(self, *, auth: AuthTokens, csrf: str, session_id: str) -> None:
        self._operations.append("update_auth_tokens")
        auth.csrf_token = csrf
        auth.session_id = session_id

    def update_auth_headers(self, *, auth: AuthTokens, kernel: Any) -> None:
        self._operations.append("update_auth_headers")


@dataclass
class RecordingRefreshBundle:
    """Aggregates the five collaborators :func:`refresh_auth_session`
    now takes, with a shared operations log so tests can assert
    end-to-end ordering across the auth-coord and lifecycle stubs.

    Wave 2 of plan ``host-protocol-removal`` decoupled
    :func:`refresh_auth_session` from a Session-shaped core; this
    bundle replaces the prior ``RecordingRefreshCore`` shell with a
    typed collection of the new explicit kwargs.
    """

    auth: AuthTokens
    http_client: httpx.AsyncClient
    operations: list[str] = field(default_factory=list)
    lifecycle: _RecordingLifecycle = field(default_factory=_RecordingLifecycle)

    def __post_init__(self) -> None:
        self.kernel = _RecordingKernel(self.http_client)
        # Share storage so the legacy ordering
        # (``[..., "save_cookies"]``) still resolves through a single
        # log without re-aggregating two test-side lists.
        self.lifecycle.operations = self.operations
        self.auth_coord = _RecordingAuthCoord(self.operations)
        # ``refresh_auth_session`` invokes
        # ``lifecycle.save_cookies(cookie_persistence, jar)`` — the
        # stub forwards the collaborator unchanged into its operations
        # log, so a sentinel object is enough here.
        self.cookie_persistence: Any = object()

    @property
    def saved_jars(self) -> list[httpx.Cookies]:
        """Back-compat passthrough so existing assertions still read the recorded jars."""
        return self.lifecycle.saved_jars


def _client(handler: httpx.MockTransport | httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, follow_redirects=True)


def _invoke(bundle: RecordingRefreshBundle):
    """Forward a :class:`RecordingRefreshBundle` into the new
    :func:`refresh_auth_session` kwarg shape.

    Wave 2 of plan ``host-protocol-removal`` made the five
    collaborators (``auth`` / ``kernel`` / ``auth_coord`` /
    ``lifecycle`` / ``cookie_persistence``) keyword-only on the
    refresh entry point; this helper keeps the per-test call sites a
    single readable expression.
    """
    return refresh_auth_session(
        auth=bundle.auth,
        kernel=bundle.kernel,  # type: ignore[arg-type]
        auth_coord=bundle.auth_coord,  # type: ignore[arg-type]
        lifecycle=bundle.lifecycle,  # type: ignore[arg-type]
        cookie_persistence=bundle.cookie_persistence,
    )


@pytest.mark.asyncio
async def test_refresh_auth_session_default_account_uses_bare_base_url() -> None:
    requests: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(200, text=REFRESH_HTML, request=request)

    async with _client(httpx.MockTransport(handler)) as http_client:
        bundle = RecordingRefreshBundle(_auth(), http_client)

        refreshed_auth = await _invoke(bundle)

    assert requests == [httpx.URL("https://notebooklm.google.com/")]
    assert refreshed_auth is bundle.auth
    assert refreshed_auth.csrf_token == "new_csrf_token_123"
    assert refreshed_auth.session_id == "new_session_id_456"
    assert bundle.operations == ["update_auth_tokens", "update_auth_headers", "save_cookies"]
    assert bundle.saved_jars == [http_client.cookies]


@pytest.mark.asyncio
async def test_refresh_auth_session_selected_account_uses_account_email_url() -> None:
    requests: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(200, text=REFRESH_HTML, request=request)

    auth = _auth(authuser=2, account_email="bob@example.com")
    async with _client(httpx.MockTransport(handler)) as http_client:
        bundle = RecordingRefreshBundle(auth, http_client)

        await _invoke(bundle)

    assert requests == [httpx.URL("https://notebooklm.google.com/?authuser=bob%40example.com")]


@pytest.mark.asyncio
async def test_refresh_auth_session_selected_account_uses_authuser_url() -> None:
    requests: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(200, text=REFRESH_HTML, request=request)

    auth = _auth(authuser=2)
    async with _client(httpx.MockTransport(handler)) as http_client:
        bundle = RecordingRefreshBundle(auth, http_client)

        await _invoke(bundle)

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
        bundle = RecordingRefreshBundle(_auth(), http_client)

        with pytest.raises(ValueError, match="Authentication expired"):
            await _invoke(bundle)

    assert bundle.operations == []


@pytest.mark.asyncio
async def test_refresh_auth_session_missing_csrf_wraps_extraction_error() -> None:
    html = '\n    <html>\n      "FdrFJe":"session_only"\n    </html>\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    async with _client(httpx.MockTransport(handler)) as http_client:
        bundle = RecordingRefreshBundle(_auth(), http_client)

        with pytest.raises(ValueError) as exc_info:
            await _invoke(bundle)

    message = str(exc_info.value)
    assert "Failed to extract CSRF token (SNlM0e)." in message
    assert "Preview:" in message
    assert "\n" not in message
    assert bundle.operations == []


@pytest.mark.asyncio
async def test_refresh_auth_session_missing_session_id_wraps_extraction_error() -> None:
    html = '\n    <html>\n      "SNlM0e":"csrf_only"\n    </html>\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    async with _client(httpx.MockTransport(handler)) as http_client:
        bundle = RecordingRefreshBundle(_auth(), http_client)

        with pytest.raises(ValueError) as exc_info:
            await _invoke(bundle)

    message = str(exc_info.value)
    assert "Failed to extract session ID (FdrFJe)." in message
    assert "Preview:" in message
    assert "\n" not in message
    assert bundle.operations == []


@pytest.mark.asyncio
async def test_refresh_auth_session_persists_through_client_core_save_cookies(
    tmp_path: Path,
) -> None:
    storage_path = tmp_path / "storage_state.json"
    auth = _auth(storage_path=storage_path)
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

    # Inject the cookie-saver seam directly (Phase 2 PR 4 — replaces the
    # legacy ``_core.save_cookies_to_storage`` string-target monkeypatch
    # with constructor injection through ``ClientLifecycle._cookie_saver``).
    core = build_client_shell_for_tests(auth, cookie_saver=fake_save_cookies_to_storage)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=auth.cookie_jar,
        follow_redirects=True,
    )
    install_http_client_for_test(core._collaborators.kernel, http_client)
    core._collaborators.cookie_persistence.capture_open_snapshot(http_client.cookies)
    try:
        # Wave 2 of plan ``host-protocol-removal`` made every dependency
        # of :func:`refresh_auth_session` an explicit keyword-only
        # collaborator. Drive the real Session's collaborators through
        # the new entry point so the persistence path goes through the
        # production ``ClientLifecycle.save_cookies`` → ``CookiePersistence``
        # → ``asyncio.to_thread(fake_save_cookies_to_storage)`` plumbing.
        await refresh_auth_session(
            auth=core._auth,
            kernel=core._collaborators.kernel,
            auth_coord=core._collaborators.auth_coord,
            lifecycle=core._collaborators.lifecycle,
            cookie_persistence=core._collaborators.cookie_persistence,
        )
    finally:
        await http_client.aclose()
        install_http_client_for_test(core._collaborators.kernel, None)

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


def test_auth_session_has_no_runtime_class_imports() -> None:
    """``_auth/session.py`` must not import ``NotebookLMClient`` or ``Session``.

    Module-level guards against importing ``notebooklm.client`` /
    ``notebooklm._core`` modules live in
    ``tests/_guardrails/test_no_core_imports.py``; this test covers the
    type-name axis (import the *class* by name from anywhere), which the
    module-level lint can't see.
    """
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

    forbidden_type_names = {"NotebookLMClient", "Session"}
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if inside_type_checking(node):
            continue
        if isinstance(node, ast.ImportFrom):
            imported_names = {alias.name for alias in node.names}
            for name in sorted(imported_names & forbidden_type_names):
                violations.append((node.lineno, name))

    assert violations == []
