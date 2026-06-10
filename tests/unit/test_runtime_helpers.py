"""Unit tests for :mod:`notebooklm._runtime.helpers`.

Currently focused on :func:`resolve_sleep` — the shared helper that
replaces the duplicated ``_resolve_sleep`` methods previously defined on
both :class:`RetryMiddleware` and :class:`AuthRefreshMiddleware` (audit
finding CC1).

The contract pinned here:

- **Injected callable wins** — ``resolve_sleep(injected)`` returns the
  injected callable verbatim when it is not ``None``.
- **None resolves to ``asyncio.sleep`` at call time** — capturing the
  module-level attribute on each invocation rather than at import time.
- **Late binding through middlewares** — both ``RetryMiddleware`` and
  ``AuthRefreshMiddleware`` observe a ``monkeypatch.setattr`` of
  ``asyncio.sleep`` because they call ``resolve_sleep(self._sleep)``
  per backoff/retry, and ``resolve_sleep`` re-reads the ``asyncio``
  module attribute every time.

This is the regression test for the audit-CC1 extraction: the duplicated
helper is gone, but the late-binding semantics tests previously relied
on (``monkeypatch.setattr("asyncio.sleep", ...)``) still work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import httpx
import pytest

from notebooklm._middleware.auth_refresh import AuthRefreshMiddleware
from notebooklm._middleware.core import NextCall, RpcRequest, RpcResponse, build_chain
from notebooklm._middleware.retry import RetryMiddleware
from notebooklm._runtime.helpers import is_auth_error, resolve_sleep
from notebooklm._transport_errors import TransportServerError
from notebooklm.rpc import AuthError, RPCError, ServerError
from tests._fixtures.chain import make_request

# ---------------------------------------------------------------------------
# Direct unit tests for resolve_sleep
# ---------------------------------------------------------------------------


def test_resolve_sleep_returns_injected_when_provided() -> None:
    """A non-``None`` injection is returned verbatim — no proxying."""

    async def fake(_seconds: float) -> None:
        return None

    assert resolve_sleep(fake) is fake


def test_resolve_sleep_returns_asyncio_sleep_when_none() -> None:
    """``None`` resolves to the canonical ``asyncio.sleep``."""

    assert resolve_sleep(None) is asyncio.sleep


def test_resolve_sleep_late_binds_asyncio_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """``monkeypatch.setattr('asyncio.sleep', ...)`` is observed at call time.

    The helper re-reads ``asyncio.sleep`` from the ``asyncio`` module global
    on every invocation, so patches that mutate the singleton ``asyncio``
    module's ``sleep`` attribute reach every caller. Capturing
    ``asyncio.sleep`` at import or construction time would freeze the binding
    and silently bypass the patch — that's the bug this helper exists to
    prevent.
    """

    async def fake(_seconds: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", fake)
    assert resolve_sleep(None) is fake


# ---------------------------------------------------------------------------
# Direct unit tests for is_auth_error
# ---------------------------------------------------------------------------


def test_is_auth_error_accepts_typed_auth_error() -> None:
    assert is_auth_error(AuthError("Authentication expired")) is True


@pytest.mark.parametrize("status", [400, 401, 403])
def test_is_auth_error_accepts_auth_http_statuses(status: int) -> None:
    assert is_auth_error(_auth_error_http(status)) is True


@pytest.mark.parametrize("rpc_code", [401, 403, 16, "UNAUTHENTICATED"])
def test_is_auth_error_accepts_explicit_rpc_auth_codes(rpc_code: int | str) -> None:
    assert is_auth_error(RPCError("auth service failure", rpc_code=rpc_code)) is True


def test_is_auth_error_accepts_explicit_rpc_status_code() -> None:
    error = RPCError("wrapped auth status")
    error.status_code = 401
    assert is_auth_error(error) is True


def test_is_auth_error_accepts_explicit_rpc_status_label() -> None:
    error = RPCError("wrapped auth status")
    error.status = "UNAUTHENTICATED"
    assert is_auth_error(error) is True


@pytest.mark.parametrize(
    "message",
    [
        "Authentication summary failed for a malformed response",
        "Session expired while processing a non-auth artifact status",
        "Unauthorized access to this notebook",
        "Please login before retrying a quota-limited action",
    ],
)
def test_is_auth_error_ignores_auth_words_without_status_or_code(message: str) -> None:
    assert is_auth_error(RPCError(message)) is False


def test_is_auth_error_does_not_promote_server_error_with_auth_code() -> None:
    error = ServerError("server auth subsystem failed", status_code=500, rpc_code=401)
    assert is_auth_error(error) is False


def test_is_auth_error_prefers_non_auth_rpc_code_over_legacy_message() -> None:
    error = RPCError(
        "Authentication required. Run 'notebooklm login' to re-authenticate.",
        rpc_code=500,
    )
    assert is_auth_error(error) is False


@pytest.mark.parametrize("rpc_code", ["", "   ", "9" * 257])
def test_is_auth_error_ignores_empty_or_large_rpc_code_before_legacy_message(
    rpc_code: str,
) -> None:
    error = RPCError("Authentication expired", rpc_code=rpc_code)
    assert is_auth_error(error) is True


# ---------------------------------------------------------------------------
# End-to-end late-binding through both middlewares
# ---------------------------------------------------------------------------


def _server_error(status: int = 503) -> TransportServerError:
    request = httpx.Request("POST", "https://example.test/x")
    response = httpx.Response(status, request=request)
    original = httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)
    return TransportServerError(
        f"server error (HTTP {status})",
        original=original,
        response=response,
        status_code=status,
    )


def _auth_error_http(status: int = 401) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _ok() -> httpx.Response:
    request = httpx.Request("POST", "https://example.test/x")
    return httpx.Response(200, request=request, content=b"ok")


def _scripted_terminal(behaviors: list[object]) -> NextCall:
    iterator = iter(behaviors)

    async def terminal(request: RpcRequest) -> RpcResponse:
        nxt = next(iterator)
        if isinstance(nxt, BaseException):
            raise nxt
        assert isinstance(nxt, httpx.Response)
        return RpcResponse(response=nxt, context=request.context)

    return terminal


@pytest.mark.asyncio
async def test_retry_middleware_observes_monkeypatched_asyncio_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RetryMiddleware honors a ``monkeypatch.setattr('asyncio.sleep', ...)``.

    The middleware is constructed without a ``sleep=`` injection, so the
    no-jitter / no-real-time backoff comes from late-binding to the
    monkeypatched ``asyncio.sleep`` instead of the real coroutine.
    """
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    # No ``sleep=`` injection — the middleware must resolve ``asyncio.sleep``
    # at call time via the shared helper.
    middleware = RetryMiddleware(rate_limit_max_retries=0, server_error_max_retries=1)
    chain = build_chain([middleware], _scripted_terminal([_server_error(), _ok()]))

    response = await chain(make_request())

    assert response.response.status_code == 200
    assert len(sleeps) == 1, "expected exactly one backoff sleep before retry"


@pytest.mark.asyncio
async def test_auth_refresh_middleware_observes_monkeypatched_asyncio_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AuthRefreshMiddleware honors a ``monkeypatch.setattr('asyncio.sleep', ...)``.

    Forces a post-refresh sleep by setting ``refresh_retry_delay`` > 0 and
    asserts the patched ``asyncio.sleep`` (not the real one) is invoked.
    """
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    refresh_calls: list[int] = []

    async def refresh() -> None:
        refresh_calls.append(1)

    def _live_delay() -> float:
        return 0.25

    middleware = AuthRefreshMiddleware(
        refresh_callable=refresh,
        is_auth_error=is_auth_error,
        refresh_callback_enabled=lambda: True,
        refresh_retry_delay=_live_delay,
        # No ``sleep=`` injection — must late-bind to monkeypatched
        # ``asyncio.sleep`` via the shared helper.
    )
    chain = build_chain([middleware], _scripted_terminal([_auth_error_http(401), _ok()]))

    response = await chain(make_request())

    assert response.response.status_code == 200
    assert refresh_calls == [1]
    assert sleeps == [pytest.approx(0.25)], (
        "expected exactly one post-refresh sleep at the configured delay, "
        "routed through the monkeypatched asyncio.sleep"
    )


@pytest.mark.asyncio
async def test_resolve_sleep_directly_invokable_is_awaitable() -> None:
    """Sanity-check that ``resolve_sleep(None)(0)`` is awaitable.

    Guards against accidentally returning a non-coroutine callable from the
    helper. We pass ``0`` so the test runs instantly with no real sleep.
    """
    result = resolve_sleep(None)(0)
    assert isinstance(result, Awaitable)
    await result
