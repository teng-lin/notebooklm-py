"""Parity tests for the shared transport pipeline.

Pins down the behavior of :meth:`RuntimeTransport.perform_authed_post`
used by the RPC executor path (the ``NotebookLMClient._perform_authed_post``
compatibility forward was deleted in Wave 11c of session-decoupling;
tests now drive the canonical collaborator method directly):

- ``build_request`` factory is invoked once at chain entry (in
  :meth:`RuntimeTransport.perform_authed_post`) and once again at the
  terminal freshness rebuild (in
  :meth:`RuntimeTransport.refresh_request_for_current_auth`). The
  terminal rebuild runs unconditionally on every attempt so a 429
  retry that re-enters the chain on the original request after an
  auth refresh always sends an envelope built from the current
  :class:`AuthSnapshot`; the happy-path rebuild is byte-identical to
  the chain-entry materialization because the snapshot has not moved.
- On a 401 + successful refresh + 200 retry, ``build_request`` is
  invoked four times — chain-entry + terminal pre-401 (both with the
  stale snapshot), then refresh-rebuild + terminal pre-200 (both with
  the refreshed snapshot). The post-refresh invocations observe a
  fresh ``AuthSnapshot`` capturing whatever the refresh callback
  mutated.
- The request-id correlation tag (``[req=<id>]``) is stable across the retry
  chain.
- ``rate_limit_max_retries`` bounds 429 retries; exhausting the budget
  raises ``TransportRateLimited``.
- A 401 → refresh → 429 → 200 sequence with the retry budget enabled
  must send the post-429 retry with the refreshed auth envelope, not
  the stale pre-refresh one — see
  ``test_stale_envelope_rebuilt_after_refresh_then_retry``.
- The historical ``rpc_call`` happy path is unchanged byte-for-byte
  (URL + body identical to pre-extraction).

The chat-side error mapping that used to live on
``NotebookLMClient.query_post`` moved to
:func:`notebooklm._chat.transport.chat_aware_authed_post` in the D2
cutover; equivalent coverage lives in ``tests/unit/test_chat_transport.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

import notebooklm._backoff as _backoff
import notebooklm._runtime.helpers as _runtime_helpers
from notebooklm._logging import get_request_id
from notebooklm._middleware.core import RpcRequest, RpcResponse
from notebooklm._request_types import AuthSnapshot
from notebooklm._transport_errors import (
    TransportAuthExpired,
    TransportRateLimited,
    TransportServerError,
)
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.rpc import RPCMethod
from tests._helpers.client_factory import build_client_shell_for_tests
from tests.unit.conftest import install_post_as_stream


@pytest.fixture(autouse=True)
def _no_backoff_jitter(monkeypatch):
    """Pin the 5xx/network backoff jitter to 0 for deterministic sleep assertions.

    Production code adds a small ±20% jitter to the exponential backoff to
    reduce thundering-herd effects across clients. These transport tests
    assert exact sleep schedules (``[1, 2, 4, ...]``), so we patch
    ``random.uniform`` on its canonical importing module to return 0. The
    429 path uses ``Retry-After`` instead of jitter, so this fixture has
    no effect on those tests.
    """
    monkeypatch.setattr(_backoff._random, "uniform", lambda a, b: 0.0)


def _make_core(
    *,
    refresh_callback: Callable[[], Any] | None = None,
    rate_limit_max_retries: int = 0,
    server_error_max_retries: int = 0,
) -> NotebookLMClient:
    auth = AuthTokens(
        csrf_token="CSRF_OLD",
        session_id="SID_OLD",
        cookies={"SID": "sid_cookie"},
    )
    return build_client_shell_for_tests(
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


# ---------------------------------------------------------------------------
# RuntimeTransport.perform_authed_post
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_perform_authed_post_populates_request_envelope_for_chain() -> None:
    """Middlewares see the materialized URL, headers, and byte body."""
    core = _make_core()
    captured: list[RpcRequest] = []

    async def fake_chain(request: RpcRequest) -> RpcResponse:
        captured.append(request)
        return RpcResponse(response=_ok_response(), context=request.context)

    core._composed.chain_host._authed_post_chain = fake_chain

    calls: list[AuthSnapshot] = []

    def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
        calls.append(snapshot)
        return (
            f"https://example.test/x?authuser={snapshot.authuser}",
            "payload",
            {"X-Test": "yes"},
        )

    await core.__aenter__()
    try:
        response = await core._composed.transport.perform_authed_post(
            build_request=build,
            log_label="RPC LIST_NOTEBOOKS",
            disable_internal_retries=True,
            rpc_method="LIST_NOTEBOOKS",
        )

        assert response.status_code == 200
        assert len(captured) == 1
        request = captured[0]
        assert request.url == "https://example.test/x?authuser=0"
        assert request.headers == {"X-Test": "yes"}
        assert request.body == b"payload"
        assert request.context["log_label"] == "RPC LIST_NOTEBOOKS"
        assert request.context["disable_internal_retries"] is True
        assert request.context["rpc_method"] == "LIST_NOTEBOOKS"
        assert len(calls) == 1
        assert calls[0].csrf_token == "CSRF_OLD"
        assert request.context["build_request"] is build
        assert request.context["auth_snapshot"] == calls[0]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_chain_reads_live_retry_budget(monkeypatch):
    """Tier-12 PR 12.7 lifted the 429 / 5xx retry loop into ``RetryMiddleware``.

    The middleware reads ``chain_host._rate_limit_max_retries`` LIVE
    (via the callable factory the chain seed installs) so a test that
    mutates the budget AFTER ``open()`` still takes effect — preserving
    the pre-PR-12.7 contract where the retry loop read the same attr live.
    Drives the chain via
    ``core._composed.transport.perform_authed_post`` so the assertion exercises the
    production seam ``RpcExecutor._execute_once`` uses.
    """
    core = _make_core(rate_limit_max_retries=0)
    await core.__aenter__()
    try:
        # Mutate AFTER open() — middleware reads via lambda closure so this
        # bump from 0 → 1 grants a single retry on the next chain call.
        core._composed.chain_host._rate_limit_max_retries = 1
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        # ``RetryMiddleware`` defaults to ``asyncio.sleep`` resolved at call
        # time, so patching the asyncio module's ``sleep`` reaches it
        # through Python's module identity.
        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(429, retry_after="1")
            return _ok_response()

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        response = await core._composed.transport.perform_authed_post(
            build_request=build, log_label="test"
        )

        assert response.status_code == 200
        assert call_count["n"] == 2
        assert sleeps == [1]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_perform_authed_post_requires_open_client():
    core = _make_core()

    def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
        return "https://example.test/x", "payload", {}

    with pytest.raises(RuntimeError, match="Client not initialized"):
        await core._composed.transport.perform_authed_post(build_request=build, log_label="test")


@pytest.mark.asyncio
async def test_auth_refresh_middleware_honors_injected_predicate() -> None:
    """``AuthRefreshMiddleware`` calls ``refresh_callable`` and retries
    exactly once when the injected ``is_auth_error`` predicate returns
    ``True``, regardless of the actual HTTP status code.

    This test avoids the retired ``_core`` auth-predicate string-target
    monkeypatch and instead constructs the middleware directly with an
    injected predicate. The
    production chain seeds ``AuthRefreshMiddleware`` with a live-binding
    ``ClientSeams.is_auth_error`` callable; that wiring is covered
    separately. Here we pin the middleware-level contract: *whatever*
    predicate is injected drives the refresh-and-retry decision.
    """
    from notebooklm._middleware.auth_refresh import AuthRefreshMiddleware

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
    ``RuntimeTransport.perform_authed_post``.

    This is the wiring-level counterpart to
    :func:`test_auth_refresh_middleware_honors_injected_predicate` (which
    pins the middleware-level contract in isolation). Together they
    cover both halves of the contract:

    1. ``AuthRefreshMiddleware`` honors its injected predicate.
    2. The composition root actually wires the middleware with a live
       predicate that recognises real auth errors through
       ``ClientSeams.is_auth_error``.

    Restored in Phase 2 PR 4 after the migration of
    ``test_chain_uses_late_bound_is_auth_error`` deleted the only end-to-end
    check of that wiring. This test avoids the retired ``_core`` indirection
    and uses a real 401 recognized by the canonical predicate.
    """
    refresh_calls: list[bool] = []

    async def refresh() -> AuthTokens:
        refresh_calls.append(True)
        return core.auth

    core = _make_core(refresh_callback=refresh)
    await core.__aenter__()
    try:

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Real 401 — recognised by the default ``is_auth_error``.
                raise _status_error(401)
            return _ok_response()

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        response = await core._composed.transport.perform_authed_post(
            build_request=build, log_label="test"
        )

        assert response.status_code == 200
        assert refresh_calls == [True], (
            "Production chain must drive the refresh-and-retry path on a "
            "real 401 — proves NotebookLMClient wires AuthRefreshMiddleware with a "
            "predicate that recognises canonical auth errors."
        )
        assert call_count["n"] == 2
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_chain_uses_late_bound_sleep_and_shared_random_uniform(monkeypatch):
    """``RetryMiddleware`` resolves ``asyncio.sleep`` at call time and uses
    the shared ``random`` module for jitter, so tests can monkey-patch both
    surfaces post-construction. PR 12.7 lifted retry into the chain but the
    historical late-bound seam is preserved end-to-end.
    """
    core = _make_core(server_error_max_retries=1)
    await core.__aenter__()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(_backoff._random, "uniform", lambda a, b: 0.2)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(503)
            return _ok_response()

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        response = await core._composed.transport.perform_authed_post(
            build_request=build, log_label="test"
        )

        assert response.status_code == 200
        assert call_count["n"] == 2
        assert sleeps == [pytest.approx(1.2)]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_perform_authed_post_disable_internal_retries_short_circuits(monkeypatch):
    core = _make_core(server_error_max_retries=2)
    await core.__aenter__()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            raise _status_error(503)

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(TransportServerError):
            await core._composed.transport.perform_authed_post(
                build_request=build,
                log_label="test",
                disable_internal_retries=True,
            )

        assert call_count["n"] == 1
        assert sleeps == []
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_build_request_rebuilt_at_terminal_on_happy_path(monkeypatch):
    """``build_request`` is invoked at chain entry AND at the terminal
    freshness rebuild — both with the same (unmoved) snapshot on the
    happy path, so the rebuilt envelope is byte-identical to the
    chain-entry materialization."""
    core = _make_core()
    await core.__aenter__()
    try:
        calls: list[AuthSnapshot] = []

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            calls.append(snapshot)
            return "https://example.test/x", "payload", {}

        async def fake_post(url, *, content, **kwargs):
            assert url == "https://example.test/x"
            assert content == b"payload"
            return _ok_response()

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        response = await core._composed.transport.perform_authed_post(
            build_request=build, log_label="test"
        )

        assert response.status_code == 200
        # The terminal freshness rebuild is load-bearing: it runs on every
        # attempt so a 429-retry after auth refresh always sends fresh auth
        # values (see ``test_stale_envelope_rebuilt_after_refresh_then_retry``).
        # On the happy path the snapshot has not moved, so both invocations
        # observe the same ``AuthSnapshot``.
        assert len(calls) == 2
        assert calls[0].csrf_token == "CSRF_OLD"
        assert calls[1].csrf_token == "CSRF_OLD"
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_first_terminal_attempt_rebuilds_when_snapshot_changed(monkeypatch):
    """A changed terminal snapshot rebuilds the envelope before send.

    The pre-chain envelope is observable, but the ``Kernel.post`` terminal must
    not send a stale body if auth changed before its first POST attempt.
    """
    core = _make_core()
    await core.__aenter__()
    try:
        snapshots = iter(
            [
                AuthSnapshot("CSRF_OLD", "SID_OLD", 0, None),
                AuthSnapshot("CSRF_NEW", "SID_NEW", 0, None),
            ]
        )

        async def fake_snapshot(*, auth: AuthTokens) -> AuthSnapshot:
            # Tightened signature pins the explicit-collaborator contract:
            # the production caller MUST pass ``auth=<live AuthTokens>``,
            # not the legacy positional host. ``auth is core.auth`` proves
            # the chain captured the same ``AuthTokens`` instance NotebookLMClient
            # holds (identity-stable per the live-reference contract in
            # ``wire_middleware_chain``).
            assert auth is core.auth
            try:
                return next(snapshots)
            except StopIteration:
                pytest.fail("unexpected extra auth snapshot")

        # PR #4b inlined ``NotebookLMClient._snapshot``; the production call
        # sites now read ``self._auth_coord.snapshot(auth=self.auth)``
        # directly (the NotebookLMClient-shaped ``_AuthRefreshHost`` was deleted
        # in favor of an explicit ``auth: AuthTokens`` kwarg), so this
        # test swaps the canonical coordinator method instead of the
        # (now-deleted) NotebookLMClient delegate.
        core._collaborators.auth_coord.snapshot = fake_snapshot  # type: ignore[method-assign]
        calls: list[AuthSnapshot] = []

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            calls.append(snapshot)
            return "https://example.test/x", f"payload-{snapshot.csrf_token}", {}

        async def fake_post(url, *, content, **kwargs):
            assert content == b"payload-CSRF_NEW"
            return _ok_response()

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        response = await core._composed.transport.perform_authed_post(
            build_request=build, log_label="test"
        )

        assert response.status_code == 200
        assert len(calls) == 2
        assert calls[0].csrf_token == "CSRF_OLD"
        assert calls[1].csrf_token == "CSRF_NEW"
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_build_request_observes_fresh_snapshot_after_401_refresh(monkeypatch):
    """On a 401 + successful refresh, the second HTTP attempt carries the
    refreshed CSRF / session-id, not the stale ones.

    ``build_request`` is invoked four times across the two-attempt flow:
    chain-entry + terminal pre-401 (both stale snapshot), then
    refresh-rebuild + terminal pre-200 (both refreshed snapshot). The
    pre-rebuild invocations carry the stale snapshot and the post-rebuild
    invocations carry the refreshed one.
    """
    refresh_calls = []

    async def refresh() -> AuthTokens:
        refresh_calls.append(True)
        # Mutate auth state so the second snapshot picks up new values.
        core.auth.csrf_token = "CSRF_NEW"
        core.auth.session_id = "SID_NEW"
        return core.auth

    core = _make_core(refresh_callback=refresh)
    await core.__aenter__()
    try:
        snapshots: list[AuthSnapshot] = []

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            snapshots.append(snapshot)
            return "https://example.test/x", f"body-{snapshot.csrf_token}", {}

        call_count = {"n": 0}

        async def fake_post(url, *, content, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(401)
            # Second attempt succeeds — confirm it carries the refreshed body.
            assert content == b"body-CSRF_NEW"
            return _ok_response()

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        response = await core._composed.transport.perform_authed_post(
            build_request=build, log_label="test"
        )

        assert response.status_code == 200
        assert len(refresh_calls) == 1
        assert call_count["n"] == 2
        # Four ``build_request`` invocations: chain-entry + terminal pre-401
        # (stale snapshot) and refresh-rebuild + terminal pre-200 (refreshed
        # snapshot).
        assert len(snapshots) == 4
        # First two invocations observe the pre-refresh snapshot.
        assert snapshots[0].csrf_token == "CSRF_OLD"
        assert snapshots[0].session_id == "SID_OLD"
        assert snapshots[1].csrf_token == "CSRF_OLD"
        assert snapshots[1].session_id == "SID_OLD"
        # Refresh runs after the 401; the last two invocations observe the
        # refreshed snapshot, and the actual HTTP body carries CSRF_NEW.
        assert snapshots[-1].csrf_token == "CSRF_NEW"
        assert snapshots[-1].session_id == "SID_NEW"
        assert snapshots[-2].csrf_token == "CSRF_NEW"
        assert snapshots[-2].session_id == "SID_NEW"
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_stale_envelope_rebuilt_after_refresh_then_retry(monkeypatch):
    """Regression: 401 → refresh → 429 → 200 sends post-429 retry with fresh auth.

    The bug being guarded:

    - ``AuthRefreshMiddleware`` lives just inside ``RetryMiddleware``. After a
      401, it refreshes auth, updates ``request.context[RPC_CONTEXT_AUTH_SNAPSHOT]``
      in-place on the shared context dict, and re-invokes the chain with a
      freshly built ``retry_request`` carrying refreshed URL / body / headers.
    - When that retry attempt receives a 429, the ``TransportRateLimited``
      bubbles back up through ``AuthRefreshMiddleware`` (which does not catch
      transport errors) and is caught by ``RetryMiddleware``, which then
      retries the chain with the **original** ``RpcRequest`` — whose
      ``url`` / ``body`` were built from the pre-refresh snapshot.
    - Without the load-bearing terminal rebuild
      (:meth:`RuntimeTransport.refresh_request_for_current_auth` running on
      every attempt), the original request would be sent verbatim and the
      backend would see stale CSRF / session-id / authuser values even though
      the cookie jar carries the refreshed cookies.

    Assertions:

    - The third terminal-side request (the post-429 retry) carries the
      refreshed CSRF / session-id / authuser values, NOT the pre-refresh
      snapshot.
    - The refresh callback is invoked exactly once across the whole flow —
      the once-per-call marker on ``request.context`` prevents
      ``AuthRefreshMiddleware`` from running a second refresh when the
      original request re-enters the chain on the 429-retry path.
    """
    refresh_calls: list[bool] = []

    async def refresh() -> AuthTokens:
        refresh_calls.append(True)
        # Mutate auth state so a subsequent snapshot captures the new values.
        core.auth.csrf_token = "CSRF_NEW"
        core.auth.session_id = "SID_NEW"
        return core.auth

    # ``rate_limit_max_retries=1`` lets the 429 burn one retry slot so the
    # post-refresh-then-429 retry path actually fires; the regression depends
    # on ``RetryMiddleware`` re-invoking the chain with the original request.
    core = _make_core(refresh_callback=refresh, rate_limit_max_retries=1)
    await core.__aenter__()
    try:
        # Avoid actually sleeping during the 429 backoff.
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            # Encode CSRF in the body and session-id + authuser in the URL,
            # mirroring how ``RpcExecutor._build`` composes the real envelope
            # (see ``RpcExecutor.build_url`` and ``build_request_body``).
            url = f"https://example.test/x?f.sid={snapshot.session_id}&authuser={snapshot.authuser}"
            body = f"at={snapshot.csrf_token}&payload=1"
            return url, body, {"X-Goog-AuthUser": str(snapshot.authuser)}

        terminal_urls: list[str] = []
        terminal_bodies: list[bytes] = []
        terminal_headers: list[dict[str, str]] = []
        call_count = {"n": 0}

        async def fake_post(url, *, content, headers=None, **kwargs):
            call_count["n"] += 1
            terminal_urls.append(url)
            terminal_bodies.append(content)
            terminal_headers.append(dict(headers or {}))
            if call_count["n"] == 1:
                # First attempt: 401 → triggers AuthRefreshMiddleware refresh.
                raise _status_error(401)
            if call_count["n"] == 2:
                # Second attempt (the refreshed retry inside AuthRefresh):
                # 429 → bubbles up through ``RetryMiddleware`` which then
                # retries the chain with the ORIGINAL request.
                raise _status_error(429, retry_after="1")
            # Third attempt (RetryMiddleware retry of the original request):
            # must carry the refreshed auth envelope, not the stale one.
            return _ok_response()

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        response = await core._composed.transport.perform_authed_post(
            build_request=build, log_label="test"
        )

        assert response.status_code == 200
        assert call_count["n"] == 3, "expected three terminal attempts (401, 429, 200)"
        # The 429 retry-after backoff fires exactly once between attempts 2 and 3.
        assert sleeps == [1]
        # The refresh callback runs exactly ONCE; the once-per-call marker on
        # the shared ``request.context`` prevents a second refresh on the
        # RetryMiddleware-driven re-entry.
        assert refresh_calls == [True]

        # First attempt: pre-refresh snapshot on the wire.
        assert terminal_urls[0] == "https://example.test/x?f.sid=SID_OLD&authuser=0"
        assert terminal_bodies[0] == b"at=CSRF_OLD&payload=1"
        assert terminal_headers[0]["X-Goog-AuthUser"] == "0"
        # Second attempt (refreshed retry inside AuthRefreshMiddleware):
        # refreshed snapshot on the wire.
        assert terminal_urls[1] == "https://example.test/x?f.sid=SID_NEW&authuser=0"
        assert terminal_bodies[1] == b"at=CSRF_NEW&payload=1"
        assert terminal_headers[1]["X-Goog-AuthUser"] == "0"
        # Third attempt (RetryMiddleware re-invoking the chain with the
        # ORIGINAL request after the 429): MUST carry the refreshed envelope.
        # This is the stale-envelope regression assertion — without the
        # unconditional terminal rebuild, this attempt would carry
        # ``f.sid=SID_OLD`` / ``at=CSRF_OLD``.
        assert terminal_urls[2] == "https://example.test/x?f.sid=SID_NEW&authuser=0"
        assert terminal_bodies[2] == b"at=CSRF_NEW&payload=1"
        assert terminal_headers[2]["X-Goog-AuthUser"] == "0"
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_transport_auth_expired_when_refresh_fails(monkeypatch):
    refresh_error = RuntimeError("re-authenticate")

    async def refresh() -> AuthTokens:
        raise refresh_error

    core = _make_core(refresh_callback=refresh)
    await core.__aenter__()
    try:

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        original = _status_error(401)

        async def fake_post(*args, **kwargs):
            raise original

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(TransportAuthExpired) as exc_info:
            await core._composed.transport.perform_authed_post(
                build_request=build, log_label="test"
            )

        assert exc_info.value.original is original
        assert exc_info.value.__cause__ is refresh_error
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_429_retries_exhaust_to_transport_rate_limited(monkeypatch):
    core = _make_core(rate_limit_max_retries=2)
    await core.__aenter__()
    try:
        # Avoid actually sleeping during the retry budget.
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            raise _status_error(429, retry_after="1")

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(TransportRateLimited) as exc_info:
            await core._composed.transport.perform_authed_post(
                build_request=build, log_label="test"
            )

        # Initial attempt + 2 retries = 3 total POSTs.
        assert call_count["n"] == 3
        assert sleeps == [1, 1]
        assert exc_info.value.retry_after == 1
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_429_without_retry_budget_raises_immediately(monkeypatch):
    core = _make_core(rate_limit_max_retries=0)
    await core.__aenter__()
    try:

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(429, retry_after="60")

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(TransportRateLimited) as exc_info:
            await core._composed.transport.perform_authed_post(
                build_request=build, log_label="test"
            )

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
    await core.__aenter__()
    try:
        observed_request_ids: list[str | None] = []

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            observed_request_ids.append(get_request_id())
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(401)
            return _ok_response()

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        # Use perform_authed_post directly inside set_request_id to verify
        # the helper itself doesn't reset the id. ``perform_authed_post``
        # is the transport-level call below ``rpc_call``; it never invokes
        # ``decode_response``, so no decode_response patch is needed here.
        # (Pre-Phase-2-PR-5 this test carried a stale string-target
        # monkeypatch of ``_core.decode_response`` —
        # dead code from when the test was earlier driven through
        # ``rpc_call``. Removed in PR 5 alongside the stdlib seam
        # migration to keep the diff localized to a single review pass.)
        from notebooklm._logging import reset_request_id, set_request_id

        token = set_request_id("REQ-stable-1234")
        try:
            await core._composed.transport.perform_authed_post(
                build_request=build, log_label="test"
            )
        finally:
            reset_request_id(token)

        assert call_count["n"] == 2
        # ``build_request`` is invoked four times across two terminal
        # attempts (chain-entry + terminal-rebuild per attempt; see module
        # docstring); the correlation id must be identical for every
        # invocation regardless of attempt index.
        assert len(observed_request_ids) == 4
        assert all(rid == "REQ-stable-1234" for rid in observed_request_ids)
    finally:
        await core.close()


# NOTE: ``query_post`` (chat-side wrapper) tests were removed in
# ``arch-d2-cutover`` — the chat-flavored error mapping moved to
# :func:`notebooklm._chat.transport.chat_aware_authed_post`. Equivalent
# coverage lives in ``tests/unit/test_chat_transport.py``.


# ---------------------------------------------------------------------------
# rpc_call happy-path parity (URL + body byte-for-byte)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_call_happy_path_url_and_body_unchanged(monkeypatch):
    """After the rpc_call extraction, ``rpc_call`` must produce the same outgoing
    ``(url, body)`` as pre-extraction for the happy path."""
    core = _make_core()
    await core.__aenter__()
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

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        # The URL must carry the standard batchexecute query string.
        assert "rpcids=" + RPCMethod.LIST_NOTEBOOKS.value in captured["url"]
        assert "f.sid=SID_OLD" in captured["url"]
        # The body must include the CSRF token under the historical ``at=`` param.
        assert b"at=CSRF_OLD" in captured["content"]
        assert b"f.req=" in captured["content"]
    finally:
        await core.close()


# ---------------------------------------------------------------------------
# server_error_max_retries — 5xx + network with exponential backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds(monkeypatch):
    """503 followed by 200: server_error_max_retries=3 lets us recover."""
    core = _make_core(server_error_max_retries=3)
    await core.__aenter__()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _status_error(503)
            return _ok_response()

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        response = await core._composed.transport.perform_authed_post(
            build_request=build, log_label="test"
        )

        assert response.status_code == 200
        assert call_count["n"] == 2
        # First retry sleeps 2 ** 0 = 1 second.
        assert sleeps == [1]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_5xx_exhausts_budget_raises_transport_server_error(monkeypatch):
    """Persistent 502 with budget=3 → 4 total attempts, then TransportServerError."""
    core = _make_core(server_error_max_retries=3)
    await core.__aenter__()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            raise _status_error(502)

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(TransportServerError) as exc_info:
            await core._composed.transport.perform_authed_post(
                build_request=build, log_label="test"
            )

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
    await core.__aenter__()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise httpx.ReadTimeout("connection blip")
            return _ok_response()

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        response = await core._composed.transport.perform_authed_post(
            build_request=build, log_label="test"
        )

        assert response.status_code == 200
        assert call_count["n"] == 2
        assert sleeps == [1]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_network_error_exhausts_budget_raises_transport_server_error(monkeypatch):
    """Repeated httpx.ConnectError → exhausts budget → TransportServerError
    wrapping the underlying RequestError (status_code/response are None)."""
    core = _make_core(server_error_max_retries=2)
    await core.__aenter__()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise httpx.ConnectError("connection refused")

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(TransportServerError) as exc_info:
            await core._composed.transport.perform_authed_post(
                build_request=build, log_label="test"
            )

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
    await core.__aenter__()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            raise _status_error(500)

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(TransportServerError) as exc_info:
            await core._composed.transport.perform_authed_post(
                build_request=build, log_label="test"
            )

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
    await core.__aenter__()
    try:
        # This test isolates the exponential schedule itself. Keep the aggregate
        # retry deadline high enough that it does not stop before the cap repeats.
        core._collaborators.lifecycle._timeout = 200.0
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(503)

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(TransportServerError):
            await core._composed.transport.perform_authed_post(
                build_request=build, log_label="test"
            )

        # min(2 ** attempt, 30) for attempt in 0..7 → 1, 2, 4, 8, 16, 30, 30, 30.
        assert sleeps == [1, 2, 4, 8, 16, 30, 30, 30]
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_5xx_path_does_not_touch_429_path(monkeypatch):
    """Sanity: a 429 should still hit the rate-limit path, not the 5xx path,
    even when server_error_max_retries is configured."""
    core = _make_core(rate_limit_max_retries=1, server_error_max_retries=3)
    await core.__aenter__()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(429, retry_after="5")

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(TransportRateLimited) as exc_info:
            await core._composed.transport.perform_authed_post(
                build_request=build, log_label="test"
            )

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
    captured_core: dict[str, NotebookLMClient] = {}

    async def refresh() -> AuthTokens:
        refresh_calls.append(True)
        return captured_core["c"].auth

    core = _make_core(refresh_callback=refresh, server_error_max_retries=1)
    captured_core["c"] = core
    await core.__aenter__()
    try:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        def build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return "https://example.test/x", "payload", {}

        async def fake_post(*args, **kwargs):
            raise _status_error(503)

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(TransportServerError):
            await core._composed.transport.perform_authed_post(
                build_request=build, log_label="test"
            )

        assert refresh_calls == []
    finally:
        await core.close()


# ---------------------------------------------------------------------------
# rpc_call wrapper for TransportServerError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rpc_call_maps_transport_server_error_to_server_error(monkeypatch):
    """``RPCError`` family: 5xx after retries → :class:`ServerError`."""
    from notebooklm.rpc import ServerError

    core = _make_core(server_error_max_retries=1)
    await core.__aenter__()
    try:

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        async def fake_post(*args, **kwargs):
            raise _status_error(503)

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(ServerError) as exc_info:
            await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        assert exc_info.value.status_code == 503
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_rpc_call_maps_transport_server_error_network_to_network_error(monkeypatch):
    """Network failure exhausting budget on rpc_call → NetworkError (not RPCError)."""
    from notebooklm.rpc import NetworkError

    core = _make_core(server_error_max_retries=1)
    await core.__aenter__()
    try:

        async def fake_sleep(seconds: float) -> None:
            pass

        monkeypatch.setattr(_runtime_helpers.asyncio, "sleep", fake_sleep)

        async def fake_post(*args, **kwargs):
            raise httpx.ConnectError("nope")

        install_post_as_stream(monkeypatch, core._collaborators.kernel.get_http_client(), fake_post)

        with pytest.raises(NetworkError):
            await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
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
        build_client_shell_for_tests(auth=auth, server_error_max_retries=-1)


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

    from notebooklm._streaming_post import stream_post_with_size_cap
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
            await stream_post_with_size_cap(
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

    from notebooklm._streaming_post import stream_post_with_size_cap

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

        response = await stream_post_with_size_cap(
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

    from notebooklm._streaming_post import stream_post_with_size_cap

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
            await stream_post_with_size_cap(
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

    from notebooklm._streaming_post import stream_post_with_size_cap

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
        response = await stream_post_with_size_cap(
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


def test_transport_constants_live_in_owning_modules():
    """Transport constants live with the modules that enforce them."""
    from notebooklm import _streaming_post, _transport_errors

    assert _streaming_post.MAX_RPC_RESPONSE_BYTES == 50 * 1024 * 1024
    assert _transport_errors.MAX_RETRY_AFTER_SECONDS == 300
