"""Tests for the internal auth session refresh collaborator."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._auth import session as session_module
from notebooklm._auth.cookie_types import CookieJar
from notebooklm._auth.cookies import _load_cookie_pair_pure
from notebooklm._auth.profile_migration import LegacyAccountContext, _load_profile_pair_pure
from notebooklm._auth.recovery import try_storage_cookie_reload
from notebooklm._auth.session import refresh_auth_session
from notebooklm._env import get_base_host
from notebooklm._web.transport.auth import AuthRefreshCoordinator
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from tests._fixtures.kernel_test_helpers import install_http_client_for_test
from tests._helpers.client_factory import build_client_shell_for_tests

REFRESH_HTML = '"SNlM0e":"new_csrf_token_123" "FdrFJe":"new_session_id_456"'
TEST_EPOCH = 1


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

    def assert_epoch(self, expected_epoch: int) -> None:
        assert expected_epoch == TEST_EPOCH

    def get_http_client(self, *, expected_epoch: int | None = None) -> httpx.AsyncClient:
        assert expected_epoch == TEST_EPOCH
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
        jar: httpx.Cookies,
        path: Path | None = None,
        *,
        expected_epoch: int | None = None,
    ) -> None:
        assert path is None
        assert expected_epoch == TEST_EPOCH
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

    def assert_epoch(self, expected_epoch: int) -> None:
        assert expected_epoch == TEST_EPOCH

    async def update_auth_tokens(
        self,
        *,
        auth: AuthTokens,
        csrf: str,
        session_id: str,
        expected_epoch: int | None = None,
    ) -> None:
        assert expected_epoch == TEST_EPOCH
        self._operations.append("update_auth_tokens")
        auth.csrf_token = csrf
        auth.session_id = session_id

    async def install_profile_session(
        self,
        *,
        auth: AuthTokens,
        target_cookie_jar: httpx.Cookies,
        source_cookie_jar: httpx.Cookies,
        expected_cookie_jar: CookieJar,
        expected_authuser: int,
        expected_account_email: str | None,
        expected_generation: int,
        authuser: int,
        account_email: str | None,
        expected_epoch: int | None = None,
    ) -> bool | None:
        assert expected_epoch == TEST_EPOCH
        return auth._replace_profile_session(
            target_cookie_jar=target_cookie_jar,
            source_cookie_jar=source_cookie_jar,
            expected_cookie_jar=expected_cookie_jar,
            expected_authuser=expected_authuser,
            expected_account_email=expected_account_email,
            expected_generation=expected_generation,
            authuser=authuser,
            account_email=account_email,
        )

    def update_auth_headers(
        self,
        *,
        auth: AuthTokens,
        kernel: Any,
        expected_epoch: int | None = None,
    ) -> None:
        assert expected_epoch == TEST_EPOCH
        self._operations.append("update_auth_headers")


class _RecordingCookiePersistence:
    async def _adopt_reloaded_baseline(self, path: Path, expected: Any, *, to_thread: Any) -> None:
        del path, expected, to_thread


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
    web_transport: _RecordingLifecycle = field(default_factory=_RecordingLifecycle)

    def __post_init__(self) -> None:
        self.kernel = _RecordingKernel(self.http_client)
        # Share storage so the legacy ordering
        # (``[..., "save_cookies"]``) still resolves through a single
        # log without re-aggregating two test-side lists.
        self.web_transport.operations = self.operations
        self.auth_coord = _RecordingAuthCoord(self.operations)
        self.cookie_persistence = _RecordingCookiePersistence()

    @property
    def saved_jars(self) -> list[httpx.Cookies]:
        """Back-compat passthrough so existing assertions still read the recorded jars."""
        return self.web_transport.saved_jars


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
        web_transport=bundle.web_transport,  # type: ignore[arg-type]
        cookie_persistence=bundle.cookie_persistence,
        expected_epoch=TEST_EPOCH,
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

    assert requests == [httpx.URL("https://notebook.google.com/")]
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

    assert requests == [httpx.URL("https://notebook.google.com/?authuser=bob%40example.com")]


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

    assert requests == [httpx.URL("https://notebook.google.com/?authuser=2")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_account", "retry_url", "expected_route"),
    [
        (
            {"authuser": 2, "email": "new@example.com"},
            httpx.URL("https://notebook.google.com/?authuser=new%40example.com"),
            (2, "new@example.com"),
        ),
        (None, httpx.URL("https://notebook.google.com/"), (0, None)),
    ],
    ids=["selected-account", "default-account"],
)
async def test_refresh_reloads_account_only_profile_generation_for_retry(
    tmp_path: Path,
    stored_account: dict[str, object] | None,
    retry_url: httpx.URL,
    expected_route: tuple[int, str | None],
) -> None:
    """An account-only profile rewrite reroutes the immediate retry and later RPCs."""
    storage = tmp_path / "storage_state.json"
    stored_state: dict[str, object] = {
        "cookies": [
            {
                "name": "SID",
                "value": "same-sid",
                "domain": ".google.com",
                "path": "/",
                "httpOnly": True,
            },
            {
                "name": "__Secure-1PSIDTS",
                "value": "same-ts",
                "domain": ".google.com",
                "path": "/",
                "httpOnly": True,
            },
        ],
        "origins": [],
    }
    if stored_account is not None:
        stored_state["notebooklm"] = {"account": stored_account}
    storage.write_text(json.dumps(stored_state), encoding="utf-8")
    live = httpx.Cookies()
    live.set("SID", "same-sid", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "same-ts", domain=".google.com", path="/")
    requests: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == get_base_host():
            requests.append(request.url)
            if request.url == retry_url:
                return httpx.Response(200, text=REFRESH_HTML, request=request)
            return httpx.Response(
                302,
                headers={"Location": "https://accounts.google.com/signin/v2/identifier"},
                request=request,
            )
        return httpx.Response(200, text="<html>Please sign in</html>", request=request)

    auth = _auth(
        storage_path=storage,
        cookie_jar=live,
        authuser=1,
        account_email="old@example.com",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=live,
        follow_redirects=True,
    ) as http_client:
        bundle = RecordingRefreshBundle(auth, http_client)
        assert await _invoke(bundle) is auth

    assert requests == [
        httpx.URL("https://notebook.google.com/?authuser=old%40example.com"),
        retry_url,
    ]
    assert (auth.authuser, auth.account_email) == expected_route
    snapshot = await AuthRefreshCoordinator().snapshot(auth=auth)
    assert (snapshot.authuser, snapshot.account_email) == expected_route


@pytest.mark.asyncio
async def test_refresh_auth_session_detects_login_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == get_base_host():
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
async def test_refresh_auth_session_reloads_fresh_profile_after_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale long-lived jar heals from a sibling process's persisted cookies."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "SID",
                        "value": "fresh-sid",
                        "domain": ".google.com",
                        "path": "/",
                        "secure": True,
                    },
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "fresh-ts",
                        "domain": ".google.com",
                        "path": "/",
                        "secure": True,
                    },
                    {
                        "name": "OSID",
                        "value": "fresh-osid",
                        "domain": "notebook.google.com",
                        "path": "/",
                        "secure": True,
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    stale = httpx.Cookies()
    stale.set("SID", "stale-sid", domain=".google.com", path="/")
    stale.set("__Secure-1PSIDTS", "stale-ts", domain=".google.com", path="/")
    stale.set("OSID", "stale-osid", domain="notebook.google.com", path="/")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == get_base_host():
            cookie = request.headers.get("cookie", "")
            requests.append(cookie)
            if "SID=fresh-sid" in cookie and "__Secure-1PSIDTS=fresh-ts" in cookie:
                return httpx.Response(200, text=REFRESH_HTML, request=request)
            return httpx.Response(
                302,
                headers={"Location": "https://accounts.google.com/signin/v2/identifier"},
                request=request,
            )
        return httpx.Response(200, text="<html>Please sign in</html>", request=request)

    monkeypatch.delenv("NOTEBOOKLM_REFRESH_CMD_MIDSESSION", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_HEADLESS_REAUTH", raising=False)

    auth = _auth(storage_path=storage, cookie_jar=stale)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=stale,
        follow_redirects=True,
    ) as http_client:
        bundle = RecordingRefreshBundle(auth, http_client)
        result = await _invoke(bundle)

    assert result is auth
    assert len(requests) == 2
    assert "SID=stale-sid" in requests[0]
    assert "SID=fresh-sid" in requests[1]
    assert auth.cookies[("SID", ".google.com", "/")] == "fresh-sid"
    assert auth.cookie_jar is http_client.cookies
    assert bundle.operations == ["update_auth_tokens", "update_auth_headers", "save_cookies"]


@pytest.mark.asyncio
async def test_refresh_preserves_a_jar_changed_after_the_rejected_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A keepalive-style A→B mutation must not be rolled back to disk A."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "stale-a", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "stale-a-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    live = httpx.Cookies()
    live.set("SID", "stale-a", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "stale-a-ts", domain=".google.com", path="/")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == get_base_host():
            cookie = request.headers.get("cookie", "")
            requests.append(cookie)
            if "SID=rotated-b" in cookie:
                return httpx.Response(200, text=REFRESH_HTML, request=request)
            return httpx.Response(
                302,
                headers={
                    "Location": "https://accounts.google.com/signin/v2/identifier",
                    "Set-Cookie": "SID=rotated-b; Domain=.google.com; Path=/",
                },
                request=request,
            )
        return httpx.Response(200, text="<html>Please sign in</html>", request=request)

    monkeypatch.delenv("NOTEBOOKLM_REFRESH_CMD_MIDSESSION", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_HEADLESS_REAUTH", raising=False)
    auth = _auth(storage_path=storage, cookie_jar=live)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=live,
        follow_redirects=True,
    ) as http_client:
        bundle = RecordingRefreshBundle(auth, http_client)
        assert await _invoke(bundle) is auth

    assert len(requests) == 2
    assert "SID=stale-a" in requests[0]
    assert "SID=rotated-b" in requests[1]
    assert auth.cookies[("SID", ".google.com", "/")] == "rotated-b"


@pytest.mark.asyncio
async def test_refresh_forces_disk_sample_after_repeated_response_cookie_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Repeated rejected-response mutations cannot starve the bounded disk read."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "disk-b", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "disk-b-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    live = httpx.Cookies()
    live.set("SID", "stale-a", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "stale-a-ts", domain=".google.com", path="/")
    requests: list[str] = []
    mutation = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mutation
        if request.url.host == get_base_host():
            cookie = request.headers.get("cookie", "")
            requests.append(cookie)
            if "SID=disk-b" in cookie:
                return httpx.Response(200, text=REFRESH_HTML, request=request)
            mutation += 1
            return httpx.Response(
                302,
                headers={
                    "Location": "https://accounts.google.com/signin/v2/identifier",
                    "Set-Cookie": f"NID=mutation-{mutation}; Domain=.google.com; Path=/",
                },
                request=request,
            )
        return httpx.Response(200, text="<html>Please sign in</html>", request=request)

    monkeypatch.delenv("NOTEBOOKLM_REFRESH_CMD_MIDSESSION", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_HEADLESS_REAUTH", raising=False)
    auth = _auth(storage_path=storage, cookie_jar=live)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=live,
        follow_redirects=True,
    ) as http_client:
        bundle = RecordingRefreshBundle(auth, http_client)
        assert await _invoke(bundle) is auth

    assert len(requests) == 3
    assert "SID=stale-a" in requests[0]
    assert "NID=mutation-1" in requests[1]
    assert "SID=disk-b" in requests[2]


@pytest.mark.asyncio
async def test_refresh_retries_live_rotation_while_disk_lags_after_second_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A forced disk sample cannot clobber an untried external live rotation."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "stale-a", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "stale-a-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
                "notebooklm": {
                    "account": {"authuser": 2, "email": "disk@example.com"},
                },
            }
        ),
        encoding="utf-8",
    )
    live = httpx.Cookies()
    live.set("SID", "stale-a", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "stale-a-ts", domain=".google.com", path="/")
    requests: list[str] = []
    client_holder: list[httpx.AsyncClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == get_base_host():
            cookie = request.headers.get("cookie", "")
            requests.append(cookie)
            if "SID=rotated-b" in cookie:
                return httpx.Response(200, text=REFRESH_HTML, request=request)
            if len(requests) == 1:
                return httpx.Response(
                    302,
                    headers={
                        "Location": "https://accounts.google.com/signin/v2/identifier",
                        "Set-Cookie": "NID=intermediate; Domain=.google.com; Path=/",
                    },
                    request=request,
                )

            authoritative = client_holder[0].cookies
            authoritative.set("SID", "rotated-b", domain=".google.com", path="/")
            authoritative.set("__Secure-1PSIDTS", "rotated-b-ts", domain=".google.com", path="/")
            return httpx.Response(
                302,
                headers={"Location": "https://accounts.google.com/signin/v2/identifier"},
                request=request,
            )
        return httpx.Response(200, text="<html>Please sign in</html>", request=request)

    monkeypatch.delenv("NOTEBOOKLM_REFRESH_CMD_MIDSESSION", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_HEADLESS_REAUTH", raising=False)
    auth = _auth(
        storage_path=storage,
        cookie_jar=live,
        authuser=1,
        account_email="live@example.com",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=live,
        follow_redirects=True,
    ) as http_client:
        client_holder.append(http_client)
        bundle = RecordingRefreshBundle(auth, http_client)
        assert await _invoke(bundle) is auth

    assert len(requests) == 3
    assert "SID=stale-a" in requests[0]
    assert "NID=intermediate" in requests[1]
    assert "SID=rotated-b" in requests[2]
    assert (auth.authuser, auth.account_email) == (1, "live@example.com")


@pytest.mark.parametrize("file_backed", [False, True])
@pytest.mark.asyncio
async def test_refresh_retries_second_auth_response_cookie_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    file_backed: bool,
) -> None:
    """A response-set auth cookie is retryable even when stale disk exists."""
    storage = tmp_path / "storage_state.json"
    if file_backed:
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "SID",
                            "value": "stale-a",
                            "domain": ".google.com",
                            "path": "/",
                        },
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "stale-a-ts",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ],
                    "origins": [],
                }
            ),
            encoding="utf-8",
        )
    live = httpx.Cookies()
    live.set("SID", "stale-a", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "stale-a-ts", domain=".google.com", path="/")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == get_base_host():
            cookie = request.headers.get("cookie", "")
            requests.append(cookie)
            if "SID=valid-c" in cookie:
                return httpx.Response(200, text=REFRESH_HTML, request=request)
            set_cookie = (
                "NID=intermediate-b; Domain=.google.com; Path=/"
                if len(requests) == 1
                else "SID=valid-c; Domain=.google.com; Path=/"
            )
            return httpx.Response(
                302,
                headers={
                    "Location": "https://accounts.google.com/signin/v2/identifier",
                    "Set-Cookie": set_cookie,
                },
                request=request,
            )
        return httpx.Response(200, text="<html>Please sign in</html>", request=request)

    monkeypatch.delenv("NOTEBOOKLM_REFRESH_CMD_MIDSESSION", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_HEADLESS_REAUTH", raising=False)
    auth = _auth(storage_path=storage if file_backed else None, cookie_jar=live)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=live,
        follow_redirects=True,
    ) as http_client:
        bundle = RecordingRefreshBundle(auth, http_client)
        assert await _invoke(bundle) is auth

    assert len(requests) == 3
    assert "SID=stale-a" in requests[0]
    assert "NID=intermediate-b" in requests[1]
    assert "SID=valid-c" in requests[2]


@pytest.mark.asyncio
async def test_refresh_uses_final_disk_sample_after_rejected_auth_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The bounded third attempt tests disk after two live auth candidates fail."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "disk-d", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "disk-d-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    live = httpx.Cookies()
    live.set("SID", "stale-a", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "stale-a-ts", domain=".google.com", path="/")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == get_base_host():
            cookie = request.headers.get("cookie", "")
            requests.append(cookie)
            if "SID=disk-d" in cookie:
                return httpx.Response(200, text=REFRESH_HTML, request=request)
            set_cookie = {
                1: "NID=intermediate; Domain=.google.com; Path=/",
                2: "SID=invalid-b; Domain=.google.com; Path=/",
                3: "SID=invalid-c; Domain=.google.com; Path=/",
            }[len(requests)]
            return httpx.Response(
                302,
                headers={
                    "Location": "https://accounts.google.com/signin/v2/identifier",
                    "Set-Cookie": set_cookie,
                },
                request=request,
            )
        return httpx.Response(200, text="<html>Please sign in</html>", request=request)

    monkeypatch.delenv("NOTEBOOKLM_REFRESH_CMD_MIDSESSION", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_HEADLESS_REAUTH", raising=False)
    auth = _auth(storage_path=storage, cookie_jar=live)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=live,
        follow_redirects=True,
    ) as http_client:
        bundle = RecordingRefreshBundle(auth, http_client)
        assert await _invoke(bundle) is auth

    assert len(requests) == 4
    assert "SID=stale-a" in requests[0]
    assert "NID=intermediate" in requests[1]
    assert "SID=invalid-b" in requests[2]
    assert "SID=disk-d" in requests[3]


@pytest.mark.asyncio
async def test_cancelled_baseline_adoption_still_synchronizes_public_cookie_views(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "disk-b", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "disk-b-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
                "notebooklm": {
                    "account": {"authuser": 2, "email": "new@example.com"},
                },
            }
        ),
        encoding="utf-8",
    )
    live = httpx.Cookies()
    live.set("SID", "stale-a", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "stale-a-ts", domain=".google.com", path="/")
    auth = _auth(
        storage_path=storage,
        cookie_jar=live,
        authuser=1,
        account_email="old@example.com",
    )
    adoption_started = asyncio.Event()

    class BlockingPersistence:
        async def _adopt_reloaded_baseline(
            self,
            path: Path,
            expected: CookieJar,
            *,
            to_thread: Any,
        ) -> None:
            del path, expected, to_thread
            adoption_started.set()
            await asyncio.Future()

    async with httpx.AsyncClient(cookies=live) as http_client:
        rejected = CookieJar.from_httpx(http_client.cookies)
        task = asyncio.create_task(
            session_module._try_storage_cookie_reload(
                auth=auth,
                kernel=_RecordingKernel(http_client),
                auth_coord=_RecordingAuthCoord([]),  # type: ignore[arg-type]
                cookie_persistence=BlockingPersistence(),  # type: ignore[arg-type]
                rejected_cookie_jar=rejected,
                expected_epoch=TEST_EPOCH,
            )
        )
        await adoption_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert auth.cookie_jar is http_client.cookies
        assert auth.cookies[("SID", ".google.com", "/")] == "disk-b"
        assert (auth.authuser, auth.account_email) == (2, "new@example.com")


@pytest.mark.asyncio
async def test_profile_reload_adopts_disk_baseline_before_persisting_response_cookies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A→B disk reload must let the retry's B→C Set-Cookie pass CAS."""
    storage = tmp_path / "storage_state.json"

    def write_profile(sid: str) -> None:
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": sid, "domain": ".google.com", "path": "/"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": f"{sid}-ts",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ],
                    "origins": [],
                }
            ),
            encoding="utf-8",
        )

    write_profile("open-a")
    stale = httpx.Cookies()
    stale.set("SID", "open-a", domain=".google.com", path="/")
    stale.set("__Secure-1PSIDTS", "open-a-ts", domain=".google.com", path="/")
    auth = _auth(storage_path=storage, cookie_jar=stale)
    core = build_client_shell_for_tests(auth)

    async def inline_to_thread(func):  # type: ignore[no-untyped-def]
        return func()

    persistence = core._web_runtime.cookie_persistence
    await persistence._prepare_open_baseline(storage, to_thread=inline_to_thread)
    write_profile("disk-b")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == get_base_host():
            cookie = request.headers.get("cookie", "")
            if "SID=disk-b" in cookie:
                return httpx.Response(
                    200,
                    text=REFRESH_HTML,
                    headers={"Set-Cookie": "SID=response-c; Domain=.google.com; Path=/"},
                    request=request,
                )
            return httpx.Response(
                302,
                headers={"Location": "https://accounts.google.com/signin/v2/identifier"},
                request=request,
            )
        return httpx.Response(200, text="<html>Please sign in</html>", request=request)

    monkeypatch.delenv("NOTEBOOKLM_REFRESH_CMD_MIDSESSION", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_HEADLESS_REAUTH", raising=False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=stale,
        follow_redirects=True,
    ) as http_client:
        core._web_runtime.kernel.activate(TEST_EPOCH)
        core._web_runtime.auth_coord.activate_epoch(TEST_EPOCH)
        install_http_client_for_test(core._web_runtime.kernel, http_client)
        await core._refresh_auth_for_epoch(expected_epoch=TEST_EPOCH)

    persisted = json.loads(storage.read_text(encoding="utf-8"))["cookies"]
    assert next(row["value"] for row in persisted if row["name"] == "SID") == "response-c"


@pytest.mark.asyncio
async def test_refresh_reloads_disk_after_concurrent_live_retry_is_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "disk-b", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "disk-b-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    read_started = threading.Event()
    release_read = threading.Event()
    blocked_once = False

    class BlockingPath(type(storage)):
        def read_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal blocked_once
            if not blocked_once:
                blocked_once = True
                read_started.set()
                if not release_read.wait(timeout=2):
                    raise TimeoutError("test did not release profile read")
            return super().read_text(*args, **kwargs)

    blocking_storage = BlockingPath(storage)
    stale = httpx.Cookies()
    stale.set("SID", "stale-a", domain=".google.com", path="/")
    stale.set("__Secure-1PSIDTS", "stale-a-ts", domain=".google.com", path="/")
    stale.set("HSID", "before", domain=".google.com", path="/")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == get_base_host():
            cookie = request.headers.get("cookie", "")
            requests.append(cookie)
            if "SID=disk-b" in cookie:
                return httpx.Response(200, text=REFRESH_HTML, request=request)
            return httpx.Response(
                302,
                headers={"Location": "https://accounts.google.com/signin/v2/identifier"},
                request=request,
            )
        return httpx.Response(200, text="<html>Please sign in</html>", request=request)

    monkeypatch.delenv("NOTEBOOKLM_REFRESH_CMD_MIDSESSION", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_HEADLESS_REAUTH", raising=False)
    auth = _auth(storage_path=blocking_storage, cookie_jar=stale)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        cookies=stale,
        follow_redirects=True,
    ) as http_client:
        bundle = RecordingRefreshBundle(auth, http_client)
        refresh_task = asyncio.create_task(_invoke(bundle))
        assert await asyncio.to_thread(read_started.wait, 2)
        stale.set("HSID", "keepalive-insufficient", domain=".google.com", path="/")
        http_client.cookies.set(
            "HSID",
            "keepalive-insufficient",
            domain=".google.com",
            path="/",
        )
        release_read.set()
        result = await refresh_task

    assert result is auth
    assert len(requests) == 3
    assert "SID=stale-a" in requests[0]
    assert "HSID=keepalive-insufficient" in requests[1]
    assert "SID=disk-b" in requests[2]


@pytest.mark.asyncio
async def test_storage_cookie_reload_declines_without_a_changed_valid_profile(
    tmp_path: Path,
) -> None:
    live = httpx.Cookies()
    live.set("SID", "same", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "same-ts", domain=".google.com", path="/")

    assert await try_storage_cookie_reload(storage_path=None, cookie_jar=live) is False

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{", encoding="utf-8")
    assert await try_storage_cookie_reload(storage_path=invalid, cookie_jar=live) is False

    unchanged = tmp_path / "unchanged.json"
    unchanged.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "same", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "same-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    unchanged_live = httpx.Cookies()
    assert (
        await try_storage_cookie_reload(storage_path=unchanged, cookie_jar=unchanged_live) is True
    )
    assert (
        await try_storage_cookie_reload(storage_path=unchanged, cookie_jar=unchanged_live) is False
    )


@pytest.mark.asyncio
async def test_storage_cookie_reload_preserves_legacy_account_route(tmp_path: Path) -> None:
    """A pending/failed context.json promotion remains authoritative on reload."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "disk-b", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "disk-b-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "context.json").write_text(
        json.dumps({"account": {"authuser": 6, "email": "legacy@example.com"}}),
        encoding="utf-8",
    )
    live = httpx.Cookies()
    live.set("SID", "open-a", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "open-a-ts", domain=".google.com", path="/")
    core = build_client_shell_for_tests(
        _auth(
            storage_path=storage,
            cookie_jar=live,
            authuser=6,
            account_email="legacy@example.com",
        )
    )
    expected = CookieJar.from_httpx(live)

    assert await try_storage_cookie_reload(
        storage_path=storage,
        cookie_jar=live,
        install_profile=lambda target, source, sampled, authuser, account_email: (
            core._web_runtime.auth_coord.install_profile_session(
                auth=core.auth,
                target_cookie_jar=target,
                source_cookie_jar=source,
                expected_cookie_jar=sampled,
                expected_authuser=6,
                expected_account_email="legacy@example.com",
                expected_generation=0,
                authuser=authuser,
                account_email=account_email,
            )
        ),
    )
    assert expected != CookieJar.from_httpx(live)
    assert live.get("SID", domain=".google.com", path="/") == "disk-b"
    assert (core.auth.authuser, core.auth.account_email) == (6, "legacy@example.com")


def test_profile_pair_rechecks_primary_after_legacy_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A login/promotion during the sibling read yields one second-file generation."""
    storage = tmp_path / "storage_state.json"

    def write_profile(sid: str, account: dict[str, object] | None) -> None:
        payload: dict[str, object] = {
            "cookies": [
                {"name": "SID", "value": sid, "domain": ".google.com", "path": "/"},
                {
                    "name": "__Secure-1PSIDTS",
                    "value": f"{sid}-ts",
                    "domain": ".google.com",
                    "path": "/",
                },
            ],
            "origins": [],
        }
        if account is not None:
            payload["notebooklm"] = {"account": account}
        storage.write_text(json.dumps(payload), encoding="utf-8")

    write_profile("first-a", None)

    def advance_primary(self: LegacyAccountContext, path: Path):  # type: ignore[no-untyped-def]
        del self
        assert path == storage
        write_profile("second-b", {"authuser": 2, "email": "second@example.com"})
        return {"authuser": 9, "email": "stale-legacy@example.com"}

    monkeypatch.setattr(LegacyAccountContext, "read", advance_primary)
    pair = _load_profile_pair_pure(storage, require_routable=False)

    assert pair.cookies.live.get("SID", domain=".google.com", path="/") == "second-b"
    assert pair.account is not None
    assert (pair.account.authuser, pair.account.email) == (2, "second@example.com")


def test_profile_pair_does_not_apply_legacy_route_to_changed_default_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent clear/default login wins over stale sibling metadata."""
    storage = tmp_path / "storage_state.json"

    def write_profile(sid: str) -> None:
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": sid, "domain": ".google.com", "path": "/"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": f"{sid}-ts",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ],
                    "origins": [],
                }
            ),
            encoding="utf-8",
        )

    write_profile("first-a")

    def clear_to_default(self: LegacyAccountContext, path: Path):  # type: ignore[no-untyped-def]
        del self
        assert path == storage
        write_profile("second-b")
        return {"authuser": 7, "email": "stale-legacy@example.com"}

    monkeypatch.setattr(LegacyAccountContext, "read", clear_to_default)
    pair = _load_profile_pair_pure(storage, require_routable=False)

    assert pair.cookies.live.get("SID", domain=".google.com", path="/") == "second-b"
    assert pair.account is None


def test_profile_pair_tombstone_beats_stale_legacy_after_clear_commit(tmp_path: Path) -> None:
    """The post-commit/pre-scrub window has an authoritative in-band absence."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "new-b", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "new-b-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "notebooklm": {"version": 1, "account_route_cleared": True},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "context.json").write_text(
        json.dumps({"account": {"authuser": 7, "email": "stale@example.com"}}),
        encoding="utf-8",
    )

    pair = _load_profile_pair_pure(storage, require_routable=False)

    assert pair.cookies.live.get("SID", domain=".google.com", path="/") == "new-b"
    assert pair.account is None


@pytest.mark.asyncio
async def test_storage_cookie_reload_preserves_live_jar_changed_during_disk_read(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "disk", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "disk-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    live = httpx.Cookies()
    live.set("SID", "rejected", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "rejected-ts", domain=".google.com", path="/")
    read_started = asyncio.Event()
    release_read = asyncio.Event()

    async def blocked_load(path: Path):  # type: ignore[no-untyped-def]
        read_started.set()
        await release_read.wait()
        return _load_profile_pair_pure(path, require_routable=False)

    async def unexpected_adoption(path, baseline):  # type: ignore[no-untyped-def]
        raise AssertionError(f"changed live jar must not adopt {path} {baseline!r}")

    reload_task = asyncio.create_task(
        try_storage_cookie_reload(
            storage_path=storage,
            cookie_jar=live,
            load_profile_pair=blocked_load,
            adopt_baseline=unexpected_adoption,
        )
    )
    await read_started.wait()
    live.set("SID", "keepalive-new", domain=".google.com", path="/")
    release_read.set()

    assert await reload_task is True
    assert live.get("SID", domain=".google.com", path="/") == "keepalive-new"


@pytest.mark.asyncio
async def test_forced_storage_read_retries_matching_untried_live_jar(tmp_path: Path) -> None:
    """A completed live+disk rotation is sampled and still retried."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "rotated-b", "domain": ".google.com", "path": "/"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "rotated-b-ts",
                        "domain": ".google.com",
                        "path": "/",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    live = _load_cookie_pair_pure(storage, require_routable=False).live
    rejected = httpx.Cookies()
    rejected.set("SID", "rejected-a", domain=".google.com", path="/")
    rejected.set("__Secure-1PSIDTS", "rejected-a-ts", domain=".google.com", path="/")
    loads = 0

    async def recording_load(path: Path):  # type: ignore[no-untyped-def]
        nonlocal loads
        loads += 1
        return _load_profile_pair_pure(path, require_routable=False)

    assert await try_storage_cookie_reload(
        storage_path=storage,
        cookie_jar=live,
        rejected_cookie_jar=CookieJar.from_httpx(rejected),
        force_disk_read=True,
        load_profile_pair=recording_load,
    )
    assert loads == 1
    assert live.get("SID", domain=".google.com", path="/") == "rotated-b"

    async def failing_load(path: Path):  # type: ignore[no-untyped-def]
        nonlocal loads
        del path
        loads += 1
        raise OSError("profile temporarily unavailable")

    assert await try_storage_cookie_reload(
        storage_path=storage,
        cookie_jar=live,
        rejected_cookie_jar=CookieJar.from_httpx(rejected),
        force_disk_read=True,
        load_profile_pair=failing_load,
    )
    assert loads == 2


@pytest.mark.asyncio
async def test_storage_cookie_reload_retries_live_jar_changed_during_baseline_adoption(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "SID",
                        "value": "rejected-a",
                        "domain": ".google.com",
                        "path": "/",
                        "httpOnly": True,
                    },
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "rejected-a-ts",
                        "domain": ".google.com",
                        "path": "/",
                        "httpOnly": True,
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    live = httpx.Cookies()
    live.set("SID", "rejected-a", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "rejected-a-ts", domain=".google.com", path="/")
    assert CookieJar.from_httpx(_load_cookie_pair_pure(storage).live) == CookieJar.from_httpx(live)
    adoption_started = asyncio.Event()
    release_adoption = asyncio.Event()

    async def blocked_adoption(path: Path, baseline: CookieJar) -> None:
        del path, baseline
        adoption_started.set()
        await release_adoption.wait()

    reload_task = asyncio.create_task(
        try_storage_cookie_reload(
            storage_path=storage,
            cookie_jar=live,
            rejected_cookie_jar=CookieJar.from_httpx(live),
            adopt_baseline=blocked_adoption,
        )
    )
    await adoption_started.wait()
    live.set("SID", "keepalive-b", domain=".google.com", path="/")
    release_adoption.set()

    assert await reload_task is True
    assert live.get("SID", domain=".google.com", path="/") == "keepalive-b"


@pytest.mark.asyncio
async def test_profile_advance_between_reload_and_adoption_cannot_be_overwritten(
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage_state.json"

    def write_profile(sid: str, authuser: int, email: str) -> None:
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": sid, "domain": ".google.com", "path": "/"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": f"{sid}-ts",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ],
                    "origins": [],
                    "notebooklm": {
                        "account": {"authuser": authuser, "email": email},
                    },
                }
            ),
            encoding="utf-8",
        )

    async def inline_to_thread(func):  # type: ignore[no-untyped-def]
        return func()

    live = httpx.Cookies()
    live.set("SID", "open-a", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "open-a-ts", domain=".google.com", path="/")
    write_profile("open-a", 1, "open@example.com")
    core = build_client_shell_for_tests(
        _auth(
            storage_path=storage,
            cookie_jar=live,
            authuser=1,
            account_email="open@example.com",
        )
    )
    persistence = core._web_runtime.cookie_persistence
    await persistence._prepare_open_baseline(storage, to_thread=inline_to_thread)
    write_profile("sample-b", 2, "sample@example.com")

    async def load_then_advance(path: Path):  # type: ignore[no-untyped-def]
        pair = _load_profile_pair_pure(path, require_routable=False)
        write_profile("sibling-c", 3, "sibling@example.com")
        return pair

    expected_authuser = core.auth.authuser
    expected_account_email = core.auth.account_email
    expected_generation = core.auth._profile_session_generation
    assert await try_storage_cookie_reload(
        storage_path=storage,
        cookie_jar=live,
        load_profile_pair=load_then_advance,
        install_profile=lambda target, source, expected, authuser, account_email: (
            core._web_runtime.auth_coord.install_profile_session(
                auth=core.auth,
                target_cookie_jar=target,
                source_cookie_jar=source,
                expected_cookie_jar=expected,
                expected_authuser=expected_authuser,
                expected_account_email=expected_account_email,
                expected_generation=expected_generation,
                authuser=authuser,
                account_email=account_email,
            )
        ),
        adopt_baseline=lambda path, baseline: persistence._adopt_reloaded_baseline(
            path,
            baseline,
            to_thread=inline_to_thread,
        ),
    )
    assert live.get("SID", domain=".google.com", path="/") == "sample-b"
    assert (core.auth.authuser, core.auth.account_email) == (2, "sample@example.com")

    await persistence._save_canonical(live, storage, to_thread=inline_to_thread)

    persisted = json.loads(storage.read_text(encoding="utf-8"))["cookies"]
    assert next(row["value"] for row in persisted if row["name"] == "SID") == "sibling-c"


@pytest.mark.asyncio
async def test_reload_sequence_supersedes_a_pre_replacement_cookie_save(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"

    def write_profile(sid: str) -> None:
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": sid, "domain": ".google.com", "path": "/"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": f"{sid}-ts",
                            "domain": ".google.com",
                            "path": "/",
                        },
                    ],
                    "origins": [],
                }
            ),
            encoding="utf-8",
        )

    async def inline_to_thread(func):  # type: ignore[no-untyped-def]
        return func()

    live = httpx.Cookies()
    live.set("SID", "open-a", domain=".google.com", path="/")
    live.set("__Secure-1PSIDTS", "open-a-ts", domain=".google.com", path="/")
    write_profile("open-a")
    core = build_client_shell_for_tests(_auth(storage_path=storage, cookie_jar=live))
    persistence = core._web_runtime.cookie_persistence
    await persistence._prepare_open_baseline(storage, to_thread=inline_to_thread)
    write_profile("disk-b")

    read_started = asyncio.Event()
    release_read = asyncio.Event()
    save_queued = asyncio.Event()
    release_save = asyncio.Event()

    async def blocked_load(path: Path):  # type: ignore[no-untyped-def]
        pair = _load_profile_pair_pure(path, require_routable=False)
        read_started.set()
        await release_read.wait()
        return pair

    async def blocked_save_thread(func):  # type: ignore[no-untyped-def]
        save_queued.set()
        await release_save.wait()
        return func()

    reload_task = asyncio.create_task(
        try_storage_cookie_reload(
            storage_path=storage,
            cookie_jar=live,
            load_profile_pair=blocked_load,
            adopt_baseline=lambda path, baseline: persistence._adopt_reloaded_baseline(
                path,
                baseline,
                to_thread=inline_to_thread,
            ),
        )
    )
    await read_started.wait()
    stale_save = asyncio.create_task(
        persistence._save_canonical(live, storage, to_thread=blocked_save_thread)
    )
    await save_queued.wait()
    release_read.set()
    assert await reload_task is True
    release_save.set()
    await stale_save

    assert live.get("SID", domain=".google.com", path="/") == "disk-b"
    persisted = json.loads(storage.read_text(encoding="utf-8"))["cookies"]
    assert next(row["value"] for row in persisted if row["name"] == "SID") == "disk-b"


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
    core._web_runtime.kernel.activate(TEST_EPOCH)
    core._web_runtime.auth_coord.activate_epoch(TEST_EPOCH)
    install_http_client_for_test(core._web_runtime.kernel, http_client)
    core._web_runtime.cookie_persistence.capture_open_snapshot(http_client.cookies)
    try:
        # Wave 2 of plan ``host-protocol-removal`` made every dependency
        # of :func:`refresh_auth_session` an explicit keyword-only
        # collaborator. Drive the real client collaborators through the
        # new entry point so the persistence path goes through the
        # production ``ClientLifecycle.save_cookies`` → ``CookiePersistence``
        # → ``asyncio.to_thread(fake_save_cookies_to_storage)`` plumbing.
        await refresh_auth_session(
            auth=core._auth,
            kernel=core._web_runtime.kernel,
            auth_coord=core._web_runtime.auth_coord,
            web_transport=core._web_runtime.web_transport,
            cookie_persistence=core._web_runtime.cookie_persistence,
            expected_epoch=TEST_EPOCH,
        )
    finally:
        await http_client.aclose()
        install_http_client_for_test(core._web_runtime.kernel, None)

    assert len(calls) == 1
    path, return_result, original_snapshot = calls[0]
    assert path == storage_path
    assert return_result is True
    assert original_snapshot is not None


def test_client_refresh_auth_is_facade_only() -> None:
    public_tree = ast.parse(textwrap.dedent(inspect.getsource(NotebookLMClient.refresh_auth)))
    public_function = next(
        node for node in ast.walk(public_tree) if isinstance(node, ast.AsyncFunctionDef)
    )
    helper_tree = ast.parse(
        textwrap.dedent(inspect.getsource(NotebookLMClient._refresh_web_runtime_auth_for_epoch))
    )
    helper_function = next(
        node for node in ast.walk(helper_tree) if isinstance(node, ast.AsyncFunctionDef)
    )

    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_refresh_auth_for_epoch"
        for node in ast.walk(public_function)
    )

    calls_refresh_session = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "refresh_auth_session"
        for node in ast.walk(helper_function)
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
    for node in ast.walk(helper_function):
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
