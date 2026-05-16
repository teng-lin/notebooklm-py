"""Parity tests for the shared transport pipeline (T2.C).

Pins down the behavior of :meth:`ClientCore._perform_authed_post` (and the
chat-side :meth:`ClientCore.query_post`) extracted from ``_rpc_call_impl``:

- ``build_request`` factory is called once per HTTP attempt.
- On a single auth-error retry, the factory is called TWICE, and the second
  invocation observes a fresh ``_AuthSnapshot`` capturing whatever the
  refresh callback mutated.
- The request-id correlation tag (``[req=<id>]``) is stable across the retry
  chain.
- ``rate_limit_max_retries`` bounds 429 retries; exhausting the budget
  raises ``_TransportRateLimited``.
- The historical ``rpc_call`` happy path is unchanged byte-for-byte
  (URL + body identical to pre-extraction).
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._core import (
    ClientCore,
    _AuthSnapshot,
    _TransportAuthExpired,
    _TransportRateLimited,
    _TransportServerError,
)
from notebooklm._core_transport import AuthedTransport
from notebooklm._logging import get_request_id
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod


@pytest.fixture(autouse=True)
def _no_backoff_jitter(monkeypatch):
    """Pin the 5xx/network backoff jitter to 0 for deterministic sleep assertions.

    Production code adds a small ±20% jitter to the exponential backoff to
    reduce thundering-herd effects across clients. These transport tests
    assert exact sleep schedules (``[1, 2, 4, ...]``), so we patch
    ``random.uniform`` inside ``notebooklm._core`` to return 0. The 429 path
    uses ``Retry-After`` instead of jitter, so this fixture has no effect on
    those tests.
    """
    monkeypatch.setattr("notebooklm._core.random.uniform", lambda a, b: 0.0)


def _make_core(
    *,
    refresh_callback: Callable[[], Any] | None = None,
    rate_limit_max_retries: int = 0,
    server_error_max_retries: int = 0,
) -> ClientCore:
    auth = AuthTokens(
        csrf_token="CSRF_OLD",
        session_id="SID_OLD",
        cookies={"SID": "sid_cookie"},
    )
    return ClientCore(
        auth=auth,
        refresh_callback=refresh_callback,
        refresh_retry_delay=0.0,
        rate_limit_max_retries=rate_limit_max_retries,
        server_error_max_retries=server_error_max_retries,
    )


def _ok_response(text: str = "OK") -> httpx.Response:
    return httpx.Response(
        200,
        text=text,
        request=httpx.Request("POST", "https://example.test/x"),
    )


def _status_error(code: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"retry-after": retry_after} if retry_after else {}
    request = httpx.Request("POST", "https://example.test/x")
    response = httpx.Response(code, request=request, headers=headers)
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


def test_core_reexports_transport_private_names():
    """Private imports from ``notebooklm._core`` remain source compatible."""
    from notebooklm import _core, _core_transport

    moved_names = [
        "_AuthSnapshot",
        "_BuildRequest",
        "_TransportAuthExpired",
        "_TransportRateLimited",
        "_TransportServerError",
        "_parse_retry_after",
    ]
    for name in moved_names:
        assert getattr(_core, name) is getattr(_core_transport, name)
    assert _core.MAX_RETRY_AFTER_SECONDS == _core_transport.MAX_RETRY_AFTER_SECONDS


def test_core_transport_has_no_runtime_core_imports():
    """The collaborator must not create a runtime import cycle back to _core."""
    path = Path(__file__).parents[2] / "src/notebooklm/_core_transport.py"
    tree = ast.parse(path.read_text())
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def inside_type_checking(node: ast.AST) -> bool:
        while node in parents:
            node = parents[node]
            if isinstance(node, ast.If):
                test = node.test
                if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
                    return True
        return False

    forbidden: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if inside_type_checking(node):
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "notebooklm._core" or alias.name.endswith("._core"):
                    forbidden.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = {alias.name for alias in node.names}
            if (
                module == "notebooklm._core"
                or (module == "notebooklm" and "_core" in names)
                or (node.level > 0 and module == "_core")
                or (node.level > 0 and not module and "_core" in names)
            ):
                imported = ", ".join(sorted(names))
                forbidden.append(
                    (node.lineno, f"from {'.' * node.level}{module} import {imported}")
                )

    assert forbidden == []


# ---------------------------------------------------------------------------
# _perform_authed_post
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authed_transport_reads_live_retry_budget(monkeypatch):
    core = _make_core(rate_limit_max_retries=0)
    await core.open()
    try:
        transport = core._get_authed_transport()
        assert isinstance(transport, AuthedTransport)
        core._rate_limit_max_retries = 1
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(429, retry_after="1")
            return _ok_response()

        monkeypatch.setattr(core._http_client, "post", fake_post)

        response = await transport.perform_authed_post(build_request=build, log_label="test")

        assert response.status_code == 200
        assert call_count["n"] == 2
        assert sleeps == [1]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_authed_transport_requires_open_client():
    core = _make_core()
    transport = core._get_authed_transport()

    def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
        return "https://example.test/x", "payload", {}

    with pytest.raises(RuntimeError, match="Client not initialized"):
        await transport.perform_authed_post(build_request=build, log_label="test")


@pytest.mark.asyncio
async def test_authed_transport_uses_late_bound_is_auth_error(monkeypatch):
    refresh_calls: list[bool] = []

    async def refresh() -> AuthTokens:
        refresh_calls.append(True)
        return core.auth

    core = _make_core(refresh_callback=refresh)
    await core.open()
    try:
        transport = core._get_authed_transport()
        monkeypatch.setattr("notebooklm._core.is_auth_error", lambda exc: True)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(418)
            return _ok_response()

        monkeypatch.setattr(core._http_client, "post", fake_post)

        response = await transport.perform_authed_post(build_request=build, log_label="test")

        assert response.status_code == 200
        assert refresh_calls == [True]
        assert call_count["n"] == 2
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_authed_transport_uses_late_bound_sleep_and_uniform(monkeypatch):
    core = _make_core(server_error_max_retries=1)
    await core.open()
    try:
        transport = core._get_authed_transport()
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)
        monkeypatch.setattr("notebooklm._core.random.uniform", lambda a, b: 0.2)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(503)
            return _ok_response()

        monkeypatch.setattr(core._http_client, "post", fake_post)

        response = await transport.perform_authed_post(build_request=build, log_label="test")

        assert response.status_code == 200
        assert call_count["n"] == 2
        assert sleeps == [pytest.approx(1.2)]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_authed_transport_disable_internal_retries_short_circuits(monkeypatch):
    core = _make_core(server_error_max_retries=2)
    await core.open()
    try:
        transport = core._get_authed_transport()
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            raise _status_error(503)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(_TransportServerError):
            await transport.perform_authed_post(
                build_request=build,
                log_label="test",
                disable_internal_retries=True,
            )

        assert call_count["n"] == 1
        assert sleeps == []
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_build_request_called_once_on_happy_path(monkeypatch):
    core = _make_core()
    await core.open()
    try:
        calls: list[_AuthSnapshot] = []

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            calls.append(snapshot)
            return "https://example.test/x", "payload", {}

        async def fake_post(url, *, content, **kwargs):
            assert url == "https://example.test/x"
            assert content == "payload"
            return _ok_response()

        monkeypatch.setattr(core._http_client, "post", fake_post)

        response = await core._perform_authed_post(build_request=build, log_label="test")

        assert response.status_code == 200
        assert len(calls) == 1
        assert calls[0].csrf_token == "CSRF_OLD"
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_build_request_called_twice_with_fresh_snapshot_on_401(monkeypatch):
    """On a 401 + successful refresh, the factory is invoked twice — and the
    second call sees the refreshed CSRF / session-id, not the stale ones."""
    refresh_calls = []

    async def refresh() -> AuthTokens:
        refresh_calls.append(True)
        # Mutate auth state so the second snapshot picks up new values.
        core.auth.csrf_token = "CSRF_NEW"
        core.auth.session_id = "SID_NEW"
        return core.auth

    core = _make_core(refresh_callback=refresh)
    await core.open()
    try:
        snapshots: list[_AuthSnapshot] = []

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            snapshots.append(snapshot)
            return "https://example.test/x", f"body-{snapshot.csrf_token}", {}

        call_count = {"n": 0}

        async def fake_post(url, *, content, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(401)
            # Second attempt succeeds — confirm it carries the refreshed body.
            assert content == "body-CSRF_NEW"
            return _ok_response()

        monkeypatch.setattr(core._http_client, "post", fake_post)

        response = await core._perform_authed_post(build_request=build, log_label="test")

        assert response.status_code == 200
        assert len(refresh_calls) == 1
        assert call_count["n"] == 2
        assert len(snapshots) == 2
        # First snapshot pre-refresh; second snapshot post-refresh.
        assert snapshots[0].csrf_token == "CSRF_OLD"
        assert snapshots[0].session_id == "SID_OLD"
        assert snapshots[1].csrf_token == "CSRF_NEW"
        assert snapshots[1].session_id == "SID_NEW"
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_transport_auth_expired_when_refresh_fails(monkeypatch):
    refresh_error = RuntimeError("re-authenticate")

    async def refresh() -> AuthTokens:
        raise refresh_error

    core = _make_core(refresh_callback=refresh)
    await core.open()
    try:

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        original = _status_error(401)

        async def fake_post(*args, **kwargs):
            raise original

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(_TransportAuthExpired) as exc_info:
            await core._perform_authed_post(build_request=build, log_label="test")

        assert exc_info.value.original is original
        assert exc_info.value.__cause__ is refresh_error
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_429_retries_exhaust_to_transport_rate_limited(monkeypatch):
    core = _make_core(rate_limit_max_retries=2)
    await core.open()
    try:
        # Avoid actually sleeping during the retry budget.
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            raise _status_error(429, retry_after="1")

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(_TransportRateLimited) as exc_info:
            await core._perform_authed_post(build_request=build, log_label="test")

        # Initial attempt + 2 retries = 3 total POSTs.
        assert call_count["n"] == 3
        assert sleeps == [1, 1]
        assert exc_info.value.retry_after == 1
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_429_without_retry_budget_raises_immediately(monkeypatch):
    core = _make_core(rate_limit_max_retries=0)
    await core.open()
    try:

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(429, retry_after="60")

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(_TransportRateLimited) as exc_info:
            await core._perform_authed_post(build_request=build, log_label="test")

        assert exc_info.value.retry_after == 60
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_request_id_constant_across_retry_chain(monkeypatch):
    """The correlation id set by ``rpc_call`` must be visible inside every
    retry attempt — both pre- and post-refresh.
    """

    async def refresh() -> AuthTokens:
        core.auth.csrf_token = "CSRF_NEW"
        return core.auth

    core = _make_core(refresh_callback=refresh)
    await core.open()
    try:
        observed_request_ids: list[str | None] = []

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            observed_request_ids.append(get_request_id())
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(401)
            return _ok_response()

        monkeypatch.setattr(core._http_client, "post", fake_post)

        # Drive through rpc_call so set_request_id is in scope (rpc_call is
        # the caller boundary that owns the request-id context).
        async def fake_decode(*args, **kwargs):
            return []

        monkeypatch.setattr(
            "notebooklm._core.decode_response",
            lambda *args, **kwargs: [],
        )

        # Use _perform_authed_post directly inside set_request_id to verify
        # the helper itself doesn't reset the id.
        from notebooklm._logging import reset_request_id, set_request_id

        token = set_request_id("REQ-stable-1234")
        try:
            await core._perform_authed_post(build_request=build, log_label="test")
        finally:
            reset_request_id(token)

        assert call_count["n"] == 2
        assert observed_request_ids == ["REQ-stable-1234", "REQ-stable-1234"]
    finally:
        await core.close()


# ---------------------------------------------------------------------------
# query_post (chat-side wrapper)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_post_wraps_rate_limit_as_chat_error(monkeypatch):
    from notebooklm.exceptions import ChatError

    core = _make_core(rate_limit_max_retries=0)
    await core.open()
    try:

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(429, retry_after="42")

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(ChatError) as exc_info:
            await core.query_post(build_request=build, parse_label="chat.ask")

        assert "Retry after 42 seconds" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, _TransportRateLimited)
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_query_post_wraps_auth_expired_as_chat_error(monkeypatch):
    from notebooklm.exceptions import ChatError

    async def refresh() -> AuthTokens:
        raise RuntimeError("login needed")

    core = _make_core(refresh_callback=refresh)
    await core.open()
    try:

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(401)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(ChatError) as exc_info:
            await core.query_post(build_request=build, parse_label="chat.ask")

        assert "authentication expired" in str(exc_info.value).lower()
        assert isinstance(exc_info.value.__cause__, _TransportAuthExpired)
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_query_post_wraps_timeout_as_network_error(monkeypatch):
    """Timeout with budget=0: ``_TransportServerError`` path must still produce a
    ``NetworkError`` whose message preserves the ``timed out`` signal (regression
    against PR #464 follow-up — without the timeout-specific branch, the message
    collapses to a generic ``network error after retries``)."""
    from notebooklm.exceptions import NetworkError

    core = _make_core()
    await core.open()
    try:

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise httpx.ReadTimeout("read timeout")

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(NetworkError) as exc_info:
            await core.query_post(build_request=build, parse_label="chat.ask")

        assert isinstance(exc_info.value.original_error, httpx.ReadTimeout)
        msg = str(exc_info.value)
        assert "timed out" in msg
        assert "network error after retries" not in msg
        # Chain: NetworkError -> _TransportServerError -> ReadTimeout
        assert isinstance(exc_info.value.__cause__, _TransportServerError)
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_query_post_timeout_after_budget_keeps_timeout_message(monkeypatch):
    """Timeout that exhausts the retry budget still surfaces ``timed out`` —
    explicit regression for the dead-handler bug: prior to the fix, the only
    code path that could produce a ``timed out`` message was the bare
    ``except httpx.TimeoutException`` handler, which became unreachable once
    ``_perform_authed_post`` started wrapping every ``httpx.RequestError``
    into ``_TransportServerError`` (PR #464)."""
    from notebooklm.exceptions import NetworkError

    core = _make_core(server_error_max_retries=2)
    await core.open()
    try:

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise httpx.ReadTimeout("read timeout")

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(NetworkError) as exc_info:
            await core.query_post(build_request=build, parse_label="chat.ask")

        msg = str(exc_info.value)
        assert "timed out" in msg
        assert "network error after retries" not in msg
        assert isinstance(exc_info.value.original_error, httpx.ReadTimeout)
        # Chain: NetworkError -> _TransportServerError -> ReadTimeout (symmetry
        # with the budget=0 test above).
        assert isinstance(exc_info.value.__cause__, _TransportServerError)
    finally:
        await core.close()


# ---------------------------------------------------------------------------
# rpc_call happy-path parity (URL + body byte-for-byte)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_call_happy_path_url_and_body_unchanged(monkeypatch):
    """After the T2.C extraction, ``rpc_call`` must produce the same outgoing
    ``(url, body)`` as pre-extraction for the happy path."""
    core = _make_core()
    await core.open()
    try:
        captured: dict[str, Any] = {}

        async def fake_post(url, *, content, **kwargs):
            captured["url"] = url
            captured["content"] = content
            # Minimal valid batchexecute response.
            rpc_id = RPCMethod.LIST_NOTEBOOKS.value
            inner = json.dumps([])
            chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]])
            text = f")]}}'\n{len(chunk)}\n{chunk}\n"
            return _ok_response(text)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        # The URL must carry the standard batchexecute query string.
        assert "rpcids=" + RPCMethod.LIST_NOTEBOOKS.value in captured["url"]
        assert "f.sid=SID_OLD" in captured["url"]
        # The body must include the CSRF token under the historical ``at=`` param.
        assert "at=CSRF_OLD" in captured["content"]
        assert "f.req=" in captured["content"]
    finally:
        await core.close()


# ---------------------------------------------------------------------------
# server_error_max_retries — 5xx + network with exponential backoff (T3.A)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds(monkeypatch):
    """503 followed by 200: server_error_max_retries=3 lets us recover."""
    core = _make_core(server_error_max_retries=3)
    await core.open()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(503)
            return _ok_response()

        monkeypatch.setattr(core._http_client, "post", fake_post)

        response = await core._perform_authed_post(build_request=build, log_label="test")

        assert response.status_code == 200
        assert call_count["n"] == 2
        # First retry sleeps 2 ** 0 = 1 second.
        assert sleeps == [1]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_5xx_exhausts_budget_raises_transport_server_error(monkeypatch):
    """Persistent 502 with budget=3 → 4 total attempts, then _TransportServerError."""
    core = _make_core(server_error_max_retries=3)
    await core.open()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            raise _status_error(502)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(_TransportServerError) as exc_info:
            await core._perform_authed_post(build_request=build, log_label="test")

        # Initial + 3 retries = 4 total attempts.
        assert call_count["n"] == 4
        # Exponential backoff: 1, 2, 4 seconds (capped at 30).
        assert sleeps == [1, 2, 4]
        assert exc_info.value.status_code == 502
        assert isinstance(exc_info.value.original, httpx.HTTPStatusError)
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_network_error_retries_then_succeeds(monkeypatch):
    """httpx.RequestError (network blip) follows the server-error retry path."""
    core = _make_core(server_error_max_retries=3)
    await core.open()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise httpx.ReadTimeout("connection blip")
            return _ok_response()

        monkeypatch.setattr(core._http_client, "post", fake_post)

        response = await core._perform_authed_post(build_request=build, log_label="test")

        assert response.status_code == 200
        assert call_count["n"] == 2
        assert sleeps == [1]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_network_error_exhausts_budget_raises_transport_server_error(monkeypatch):
    """Repeated httpx.ConnectError → exhausts budget → _TransportServerError
    wrapping the underlying RequestError (status_code/response are None)."""
    core = _make_core(server_error_max_retries=2)
    await core.open()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(_TransportServerError) as exc_info:
            await core._perform_authed_post(build_request=build, log_label="test")

        # Initial + 2 retries = 3 attempts; 2 sleeps (1, 2).
        assert sleeps == [1, 2]
        assert exc_info.value.status_code is None
        assert exc_info.value.response is None
        assert isinstance(exc_info.value.original, httpx.ConnectError)
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_server_error_budget_zero_raises_immediately(monkeypatch):
    """server_error_max_retries=0 short-circuits to immediate raise (no sleep)."""
    core = _make_core(server_error_max_retries=0)
    await core.open()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            raise _status_error(500)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(_TransportServerError) as exc_info:
            await core._perform_authed_post(build_request=build, log_label="test")

        # Exactly one attempt, no sleep.
        assert call_count["n"] == 1
        assert sleeps == []
        assert exc_info.value.status_code == 500
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_exponential_backoff_caps_at_30_seconds(monkeypatch):
    """Backoff schedule: 1, 2, 4, 8, 16, 30 — caps at 30 for high attempt counts."""
    core = _make_core(server_error_max_retries=8)
    await core.open()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(503)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(_TransportServerError):
            await core._perform_authed_post(build_request=build, log_label="test")

        # min(2 ** attempt, 30) for attempt in 0..7 → 1, 2, 4, 8, 16, 30, 30, 30.
        assert sleeps == [1, 2, 4, 8, 16, 30, 30, 30]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_5xx_path_does_not_touch_429_path(monkeypatch):
    """Sanity: a 429 should still hit the rate-limit path, not the 5xx path,
    even when server_error_max_retries is configured."""
    core = _make_core(rate_limit_max_retries=1, server_error_max_retries=3)
    await core.open()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(429, retry_after="5")

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(_TransportRateLimited) as exc_info:
            await core._perform_authed_post(build_request=build, log_label="test")

        # 429-path sleep uses Retry-After (5), NOT exponential backoff.
        assert sleeps == [5]
        assert exc_info.value.retry_after == 5
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_5xx_path_does_not_trigger_auth_refresh(monkeypatch):
    """A 503 must not be misclassified as auth error → refresh path. Refresh
    callback must never be called even when configured."""
    refresh_calls: list[bool] = []
    captured_core: dict[str, ClientCore] = {}

    async def refresh() -> AuthTokens:
        refresh_calls.append(True)
        return captured_core["c"].auth

    core = _make_core(refresh_callback=refresh, server_error_max_retries=1)
    captured_core["c"] = core
    await core.open()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(503)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(_TransportServerError):
            await core._perform_authed_post(build_request=build, log_label="test")

        assert refresh_calls == []
    finally:
        await core.close()


# ---------------------------------------------------------------------------
# rpc_call + query_post wrappers for _TransportServerError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_call_maps_transport_server_error_to_server_error(monkeypatch):
    """``RPCError`` family: 5xx after retries → :class:`ServerError`."""
    from notebooklm.rpc import ServerError

    core = _make_core(server_error_max_retries=1)
    await core.open()
    try:

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        async def fake_post(*args, **kwargs):
            raise _status_error(503)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(ServerError) as exc_info:
            await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert exc_info.value.status_code == 503
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_rpc_call_maps_transport_server_error_network_to_network_error(monkeypatch):
    """Network failure exhausting budget on rpc_call → NetworkError (not RPCError)."""
    from notebooklm.rpc import NetworkError

    core = _make_core(server_error_max_retries=1)
    await core.open()
    try:

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        async def fake_post(*args, **kwargs):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(NetworkError):
            await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_query_post_maps_transport_server_error_to_chat_error(monkeypatch):
    """``query_post`` (chat surface): 5xx after retries → ChatError."""
    from notebooklm.exceptions import ChatError

    core = _make_core(server_error_max_retries=1)
    await core.open()
    try:

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(500)

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(ChatError) as exc_info:
            await core.query_post(build_request=build, parse_label="chat.ask")

        assert "HTTP 500" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, _TransportServerError)
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_query_post_maps_transport_server_error_network_to_network_error(monkeypatch):
    """Network failure exhausting budget on query_post → NetworkError."""
    from notebooklm.exceptions import NetworkError

    core = _make_core(server_error_max_retries=1)
    await core.open()
    try:

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr("notebooklm._core.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(core._http_client, "post", fake_post)

        with pytest.raises(NetworkError) as exc_info:
            await core.query_post(build_request=build, parse_label="chat.ask")

        assert isinstance(exc_info.value.original_error, httpx.ConnectError)
    finally:
        await core.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_server_error_max_retries_negative_raises():
    """Symmetric with rate_limit_max_retries: negative values are rejected."""
    auth = AuthTokens(
        csrf_token="CSRF",
        session_id="SID",
        cookies={"SID": "x"},
    )
    with pytest.raises(ValueError, match="server_error_max_retries must be >= 0"):
        ClientCore(auth=auth, server_error_max_retries=-1)
