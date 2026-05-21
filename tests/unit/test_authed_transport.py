"""Parity tests for the shared transport pipeline.

Pins down the behavior of :meth:`Session._perform_authed_post`
extracted from ``_rpc_call_impl``:

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

The chat-side error mapping that used to live on
``Session.query_post`` moved to
:func:`notebooklm._chat_transport.chat_aware_authed_post` in the D2
cutover; equivalent coverage lives in ``tests/unit/test_chat_transport.py``.
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

from conftest import install_post_as_stream
from notebooklm._authed_transport import (
    AuthedTransport,
    _AuthSnapshot,
    _TransportAuthExpired,
    _TransportRateLimited,
    _TransportServerError,
)
from notebooklm._logging import get_request_id
from notebooklm._middleware import RpcRequest, RpcResponse
from notebooklm._session import Session
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod


@pytest.fixture(autouse=True)
def _no_backoff_jitter(monkeypatch):
    """Pin the 5xx/network backoff jitter to 0 for deterministic sleep assertions.

    Production code adds a small ±20% jitter to the exponential backoff to
    reduce thundering-herd effects across clients. These transport tests
    assert exact sleep schedules (``[1, 2, 4, ...]``), so we patch
    ``random.uniform`` on its canonical importing module
    :mod:`notebooklm._session` (Phase 2 PR 5 migrated this off the
    deprecated ``notebooklm._core.random.uniform`` shim) to return 0. The
    429 path uses ``Retry-After`` instead of jitter, so this fixture has
    no effect on those tests.
    """
    monkeypatch.setattr("notebooklm._session.random.uniform", lambda a, b: 0.0)


def _make_core(
    *,
    refresh_callback: Callable[[], Any] | None = None,
    rate_limit_max_retries: int = 0,
    server_error_max_retries: int = 0,
) -> Session:
    auth = AuthTokens(
        csrf_token="CSRF_OLD",
        session_id="SID_OLD",
        cookies={"SID": "sid_cookie"},
    )
    return Session(
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


def test_authed_transport_has_no_runtime_core_imports():
    """The collaborator must not create a runtime import cycle back to _core."""
    path = Path(__file__).parents[2] / "src/notebooklm/_authed_transport.py"
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
async def test_chain_reads_live_retry_budget(monkeypatch):
    """Tier-12 PR 12.7 lifted the 429 / 5xx retry loop into ``RetryMiddleware``.

    The middleware reads ``self._rate_limit_max_retries`` on the host LIVE
    (via the callable factory the chain seed installs) so a test that
    mutates the budget AFTER ``open()`` still takes effect — preserving
    the pre-PR-12.7 contract where ``AuthedTransport`` read the same
    attr live inside its loop. Drives the chain via
    ``core._perform_authed_post`` so the assertion exercises the
    production seam ``RpcExecutor.execute`` uses.
    """
    core = _make_core(rate_limit_max_retries=0)
    await core.open()
    try:
        # Confirm the leaf is still AuthedTransport (sanity).
        assert isinstance(core._get_authed_transport(), AuthedTransport)
        # Mutate AFTER open() — middleware reads via lambda closure so this
        # bump from 0 → 1 grants a single retry on the next chain call.
        core._rate_limit_max_retries = 1
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        # ``RetryMiddleware`` defaults to ``asyncio.sleep`` resolved at call
        # time, so patching the asyncio module's ``sleep`` reaches it
        # through Python's module identity.
        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(429, retry_after="1")
            return _ok_response()

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

        response = await core._perform_authed_post(build_request=build, log_label="test")

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
async def test_auth_refresh_middleware_honors_injected_predicate() -> None:
    """``AuthRefreshMiddleware`` calls ``refresh_callable`` and retries
    exactly once when the injected ``is_auth_error`` predicate returns
    ``True``, regardless of the actual HTTP status code.

    Phase 2 PR 4 (``.sisyphus/plans/refactor-completion-plan.md``)
    rewrote this test off the legacy ``_core.is_auth_error`` string-target
    monkeypatch and instead constructs the middleware directly with an
    injected predicate. The
    production chain seeds ``AuthRefreshMiddleware`` with
    ``is_auth_error=_live_is_auth_error`` (see
    ``notebooklm._session._get_authed_transport``); that wiring is
    covered separately. Here we pin the middleware-level contract:
    *whatever* predicate is injected drives the refresh-and-retry
    decision.
    """
    from notebooklm._middleware_auth_refresh import AuthRefreshMiddleware

    refresh_calls: list[bool] = []

    async def refresh() -> None:
        refresh_calls.append(True)

    # A 418 (I'm a teapot) — NOT recognised by the production
    # ``is_auth_error`` (which keys off 400/401/403). The injected
    # predicate returns True unconditionally, so the middleware treats it
    # as an auth error and runs the refresh path.
    boom = _status_error(418)
    call_count = {"n": 0}

    async def terminal(request):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise boom
        return RpcResponse(response=_ok_response(), context=request.context)

    middleware = AuthRefreshMiddleware(
        refresh_callable=refresh,
        is_auth_error=lambda exc: True,
        refresh_callback_enabled=lambda: True,
        refresh_retry_delay=lambda: 0.0,
    )

    request = RpcRequest(
        url="https://example.test/x",
        headers={},
        body=b"payload",
        context={"log_label": "test"},
    )
    response = await middleware(request, terminal)

    assert response.response.status_code == 200
    assert refresh_calls == [True]
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_production_chain_drives_refresh_on_real_401(monkeypatch):
    """Production-chain regression: a real ``HTTPStatusError(401)`` raised
    by the transport leaf must drive the refresh-and-retry path through
    ``Session._perform_authed_post``.

    This is the wiring-level counterpart to
    :func:`test_auth_refresh_middleware_honors_injected_predicate` (which
    pins the middleware-level contract in isolation). Together they
    cover both halves of the contract:

    1. ``AuthRefreshMiddleware`` honors its injected predicate.
    2. ``Session.__init__`` actually wires the middleware with a
       predicate that recognises real auth errors — currently
       ``is_auth_error=_live_is_auth_error`` (see
       ``notebooklm._session._get_authed_transport`` /
       ``_get_rpc_executor``), where ``_live_is_auth_error`` resolves
       :func:`notebooklm._session_helpers.is_auth_error` at call time.

    Restored in Phase 2 PR 4 after the migration of
    ``test_chain_uses_late_bound_is_auth_error`` (which used
    ``monkeypatch.setattr("notebooklm._core.is_auth_error", lambda exc:
    True)`` to force ANY exception to be treated as an auth error)
    deleted the only end-to-end check of that wiring. Codex / agy
    review caught the regression; this test re-adds the coverage
    without depending on the soon-to-be-retired ``_core`` indirection
    by using a real 401 that the canonical predicate already
    recognises.
    """
    refresh_calls: list[bool] = []

    async def refresh() -> AuthTokens:
        refresh_calls.append(True)
        return core.auth

    core = _make_core(refresh_callback=refresh)
    await core.open()
    try:

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Real 401 — recognised by ``is_auth_error``, which
                # ``_live_is_auth_error`` resolves at call time. No
                # monkeypatch needed: the predicate's natural behavior
                # drives the refresh path.
                raise _status_error(401)
            return _ok_response()

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

        response = await core._perform_authed_post(build_request=build, log_label="test")

        assert response.status_code == 200
        assert refresh_calls == [True], (
            "Production chain must drive the refresh-and-retry path on a "
            "real 401 — proves Session wires AuthRefreshMiddleware with a "
            "predicate that recognises canonical auth errors."
        )
        assert call_count["n"] == 2
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_chain_uses_late_bound_sleep_and_shared_random_uniform(monkeypatch):
    """``RetryMiddleware`` resolves ``asyncio.sleep`` at call time and uses
    the shared ``random`` module for jitter, so tests can monkey-patch both
    surfaces post-construction. Pre-PR-12.7 this contract sat on
    ``AuthedTransport``; PR 12.7 lifted retry into the chain but the
    same late-bound seam is preserved end-to-end.
    """
    core = _make_core(server_error_max_retries=1)
    await core.open()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)
        monkeypatch.setattr("notebooklm._session.random.uniform", lambda a, b: 0.2)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(503)
            return _ok_response()

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

        response = await core._perform_authed_post(build_request=build, log_label="test")

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

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            raise _status_error(503)

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

        # Use _perform_authed_post directly inside set_request_id to verify
        # the helper itself doesn't reset the id. ``_perform_authed_post``
        # is the transport-level call below ``rpc_call``; it never invokes
        # ``decode_response``, so no decode_response patch is needed here.
        # (Pre-Phase-2-PR-5 this test carried a stale
        # ``monkeypatch.setattr("notebooklm._core.decode_response", …)`` —
        # dead code from when the test was earlier driven through
        # ``rpc_call``. Removed in PR 5 alongside the stdlib seam
        # migration to keep the diff localized to a single review pass.)
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


# NOTE: ``query_post`` (chat-side wrapper) tests were removed in
# ``arch-d2-cutover`` — the chat-flavored error mapping moved to
# :func:`notebooklm._chat_transport.chat_aware_authed_post`. Equivalent
# coverage lives in ``tests/unit/test_chat_transport.py``.


# ---------------------------------------------------------------------------
# rpc_call happy-path parity (URL + body byte-for-byte)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_call_happy_path_url_and_body_unchanged(monkeypatch):
    """After the rpc_call extraction, ``rpc_call`` must produce the same outgoing
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

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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
# server_error_max_retries — 5xx + network with exponential backoff
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

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(503)
            return _ok_response()

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            raise _status_error(502)

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise httpx.ReadTimeout("connection blip")
            return _ok_response()

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            raise _status_error(500)

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(503)

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(429, retry_after="5")

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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
    captured_core: dict[str, Session] = {}

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

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        def build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(503)

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

        with pytest.raises(_TransportServerError):
            await core._perform_authed_post(build_request=build, log_label="test")

        assert refresh_calls == []
    finally:
        await core.close()


# ---------------------------------------------------------------------------
# rpc_call wrapper for _TransportServerError
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

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        async def fake_post(*args, **kwargs):
            raise _status_error(503)

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

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

        monkeypatch.setattr("notebooklm._session.asyncio.sleep", fake_sleep)

        async def fake_post(*args, **kwargs):
            raise httpx.ConnectError("nope")

        install_post_as_stream(monkeypatch, core._http_client, fake_post)

        with pytest.raises(NetworkError):
            await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
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
        Session(auth=auth, server_error_max_retries=-1)


# ---------------------------------------------------------------------------
# Streamed RPC response size cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streamed_response_size_cap(monkeypatch):
    """A response that exceeds ``max_bytes`` raises before the buffer is full.

    Stubs ``client.stream`` to yield chunks that sum to more than the cap.
    The guard must abort the read loop and surface
    :class:`RPCResponseTooLargeError` instead of buffering an unbounded body.
    """
    from contextlib import asynccontextmanager

    from notebooklm._authed_transport import _stream_post_with_size_cap
    from notebooklm.exceptions import RPCResponseTooLargeError

    cap = 1024  # 1 KiB cap so the test stays fast and small.
    chunks_yielded = 0

    class _FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}
        request = httpx.Request("POST", "https://example.test/x")

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            nonlocal chunks_yielded
            # Each chunk is half the cap; the third one trips the guard. We
            # deliberately yield well past the limit so a buggy implementation
            # that buffers everything is caught (it would OOM in production).
            payload = b"x" * (cap // 2)
            for _ in range(8):
                chunks_yielded += 1
                yield payload

    @asynccontextmanager
    async def fake_stream(method, url, **kwargs):
        yield _FakeResponse()

    client = httpx.AsyncClient()
    try:
        monkeypatch.setattr(client, "stream", fake_stream)

        with pytest.raises(RPCResponseTooLargeError) as exc_info:
            await _stream_post_with_size_cap(
                client,
                "https://example.test/x",
                body=b"",
                headers=None,
                max_bytes=cap,
            )

        # Aborts as soon as the running total crosses the cap — does NOT
        # keep iterating to the end of the upstream stream.
        assert chunks_yielded < 8
        assert exc_info.value.limit_bytes == cap
        assert exc_info.value.bytes_read is not None
        assert exc_info.value.bytes_read > cap
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_normal_response_below_cap_works(monkeypatch):
    """A normal-sized response decodes through the streaming wrapper unchanged."""
    from contextlib import asynccontextmanager

    from notebooklm._authed_transport import _stream_post_with_size_cap

    payload = b"hello world" * 1000  # ~11 KB, well under the 50 MiB default

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "text/plain"}
        request = httpx.Request("POST", "https://example.test/x")

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            # Yield in two chunks to exercise the loop, not a single shot.
            yield payload[: len(payload) // 2]
            yield payload[len(payload) // 2 :]

    @asynccontextmanager
    async def fake_stream(method, url, **kwargs):
        yield _FakeResponse()

    client = httpx.AsyncClient()
    try:
        monkeypatch.setattr(client, "stream", fake_stream)

        response = await _stream_post_with_size_cap(
            client,
            "https://example.test/x",
            body=b"",
            headers=None,
        )

        assert response.status_code == 200
        assert response.content == payload
        # Buffered into a real httpx.Response so downstream callers can keep
        # using ``.text`` without dealing with stream state.
        assert response.text == payload.decode("utf-8")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_streaming_raise_for_status_propagates_before_size_check(monkeypatch):
    """``raise_for_status`` runs before the read loop so the existing
    auth-refresh / 429 / 5xx branches see the same error they always did."""
    from contextlib import asynccontextmanager

    from notebooklm._authed_transport import _stream_post_with_size_cap

    chunk_reads = 0

    class _FakeResponse:
        status_code = 429
        headers = {"retry-after": "1"}
        request = httpx.Request("POST", "https://example.test/x")

        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError(
                "rate limited",
                request=self.request,
                response=httpx.Response(
                    429,
                    headers=self.headers,
                    request=self.request,
                ),
            )

        async def aiter_bytes(self):
            nonlocal chunk_reads
            chunk_reads += 1
            yield b"never read"

    @asynccontextmanager
    async def fake_stream(method, url, **kwargs):
        yield _FakeResponse()

    client = httpx.AsyncClient()
    try:
        monkeypatch.setattr(client, "stream", fake_stream)

        with pytest.raises(httpx.HTTPStatusError):
            await _stream_post_with_size_cap(
                client,
                "https://example.test/x",
                body=b"",
                headers=None,
            )

        assert chunk_reads == 0, "body must not be read when raise_for_status fires"
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "encoding",
    # Every codec httpx wires into its content-decoder chain. ``gzip`` is
    # the one #769 hit in production; ``br`` and ``zstd`` ship with httpx
    # whenever the optional ``brotli`` / ``zstandard`` packages are
    # installed, and ``deflate`` is always available. Parametrizing all
    # four guards against a future codec going through the same rebuild
    # path with an unstripped Content-Encoding header.
    ["gzip", "br", "zstd", "deflate"],
)
@pytest.mark.asyncio
async def test_streaming_strips_content_encoding_to_prevent_double_decode(monkeypatch, encoding):
    """Regression for #769.

    ``response.aiter_bytes()`` yields already-decoded chunks, so the buffered
    payload is plain bytes. If the upstream ``Content-Encoding`` header (e.g.
    ``gzip``) is carried over verbatim onto the rebuilt :class:`httpx.Response`,
    its ``__init__`` re-runs the decoder on already-decoded bytes and raises
    ``DecodingError: Error -3 ... incorrect header check``.

    The wrapper must strip ``content-encoding`` (and ``content-length``) before
    handing headers back so downstream ``.text`` access stays a plain charset
    decode — no double decompression. Parametrized across every codec httpx
    knows about so adding a new ``Content-Encoding`` value in the future
    cannot silently regress this branch.
    """
    # ``br`` / ``zstd`` only re-trigger ``Response.__init__``'s decoder when
    # the optional ``brotli`` / ``zstandard`` packages are present. Without
    # them httpx no-ops the encoding and the test would pass even WITHOUT
    # the strip — defeating the regression. Skip the variant rather than
    # silently lie about coverage.
    if encoding == "br":
        pytest.importorskip("brotli")
    elif encoding == "zstd":
        pytest.importorskip("zstandard")
    from contextlib import asynccontextmanager

    from notebooklm._authed_transport import _stream_post_with_size_cap

    # Realistic batchexecute prefix; only the bytes matter, not the framing.
    decoded_payload = b')]}\'\n\n[["wrb.fr",null,"[1]",null,null,null,"generic"]]'

    class _FakeResponse:
        status_code = 200
        # Upstream advertises the parametrized encoding — the kind of header
        # that flowed through the transport at production time and bit #769.
        headers = {
            "content-type": "application/json; charset=UTF-8",
            "content-encoding": encoding,
            # Length of the compressed body upstream — also a lie for the
            # rebuilt response, since we hold the decoded bytes now.
            "content-length": "9999",
        }
        request = httpx.Request("POST", "https://example.test/x")

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            yield decoded_payload

    @asynccontextmanager
    async def fake_stream(method, url, **kwargs):
        yield _FakeResponse()

    client = httpx.AsyncClient()
    try:
        monkeypatch.setattr(client, "stream", fake_stream)

        # Pre-fix this call raised httpx.DecodingError during Response.__init__.
        response = await _stream_post_with_size_cap(
            client,
            "https://example.test/x",
            body=b"",
            headers=None,
        )

        # Body round-trips as the decoded payload, both as bytes and text.
        assert response.content == decoded_payload
        assert response.text == decoded_payload.decode("utf-8")
        # The misleading content-encoding header must NOT survive — otherwise
        # any downstream consumer that re-streams or re-reads the response
        # would hit the same double-decode trap.
        assert "content-encoding" not in response.headers
        # httpx may auto-repopulate content-length to match the buffered body,
        # which is fine — what matters is that it doesn't carry the stale
        # upstream value (9999) that misrepresented the decoded payload.
        if "content-length" in response.headers:
            assert response.headers["content-length"] == str(len(decoded_payload))
    finally:
        await client.aclose()


def test_max_rpc_response_bytes_constant_lives_in_transport_module():
    """Constant is owned by ``_authed_transport`` (not ``_core``) to avoid an
    import cycle — ``_core`` already imports from ``_authed_transport``."""
    from notebooklm import _authed_transport

    assert _authed_transport.MAX_RPC_RESPONSE_BYTES == 50 * 1024 * 1024
    # Sanity: it sits next to the other transport-layer constant.
    assert _authed_transport.MAX_RETRY_AFTER_SECONDS == 300
