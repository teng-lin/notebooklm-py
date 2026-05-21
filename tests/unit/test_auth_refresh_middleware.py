"""Unit tests for :class:`AuthRefreshMiddleware` (Tier-12 PR 12.8).

Pins the contract documented in ``src/notebooklm/_middleware_auth_refresh.py``
and ADR-009 §"Chain ordering":

- **Pass-through on success.** Single ``next_call``; result returned.
- **Pass-through on non-auth exception.** ``TransportRateLimited`` /
  ``TransportServerError`` propagate to ``RetryMiddleware`` outside.
- **Pass-through when no refresh callback configured.** The
  ``refresh_callback_enabled`` gate matches the legacy
  ``host._refresh_callback is not None`` check.
- **Refresh-and-retry on auth error.** First ``next_call`` raises
  ``httpx.HTTPStatusError`` recognized by ``is_auth_error`` → refresh
  callable runs (coalesced single-flight) → optional post-refresh sleep
  → metric increment → exactly one retry via ``next_call(request)``.
- **Refresh failure** → wrap original ``HTTPStatusError`` in
  ``TransportAuthExpired`` and propagate.
- **Exactly one retry.** Per ADR-009 §"Retry semantics", a second auth
  error on the retry leg propagates — no second refresh, no recursion.
- **Post-refresh sleep honored** when ``refresh_retry_delay > 0``.
- **Live-bound `refresh_retry_delay`.** Callable getter so test mutation
  on the host still takes effect (matches the RetryMiddleware idiom).
- **Log shape preservation** — "auth error detected, attempting token
  refresh" / "Token refresh failed: X" / "Token refresh successful,
  retrying Y" match the legacy ``AuthedTransport`` messages bit-for-bit
  so log-grep alerts keep matching.
- **Metrics increment** — ``rpc_auth_retries`` incremented exactly once
  per successful refresh.
- **Log-label fallback** — missing ``log_label`` in context surfaces
  ``"<unknown-chain-call>"`` rather than ``KeyError`` (defensive for
  ``__new__``-built fixtures; matches Drain/Retry behavior).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

# pytest puts ``tests/`` on ``sys.path``; ``_fixtures.chain`` is the canonical
# import path documented in ``tests/_fixtures/__init__.py``.
from _fixtures.chain import make_request
from notebooklm._authed_transport import (
    TransportAuthExpired,
    TransportRateLimited,
    TransportServerError,
)
from notebooklm._client_metrics import ClientMetrics
from notebooklm._middleware import NextCall, RpcRequest, RpcResponse, build_chain
from notebooklm._middleware_auth_refresh import AuthRefreshMiddleware
from notebooklm._session_helpers import is_auth_error


def _recording_sleep() -> tuple[Callable[[float], Awaitable[None]], list[float]]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return sleep, slept


def _auth_error(*, status: int = 401, url: str = "https://example.test/x") -> httpx.HTTPStatusError:
    """Build an ``httpx.HTTPStatusError`` that ``is_auth_error`` recognizes."""
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _scripted_terminal(behaviors: list[Any]) -> tuple[NextCall, list[RpcRequest]]:
    """Yield each ``behaviors`` entry per call. Exceptions raise; Responses wrap."""
    calls: list[RpcRequest] = []
    iterator = iter(behaviors)

    async def terminal(request: RpcRequest) -> RpcResponse:
        calls.append(request)
        nxt = next(iterator)
        if isinstance(nxt, BaseException):
            raise nxt
        return RpcResponse(response=nxt, context=request.context)

    return terminal, calls


def _make_middleware(
    *,
    refresh_callable: Callable[[], Awaitable[None]] | None = None,
    refresh_enabled: bool = True,
    refresh_retry_delay: float = 0.0,
    sleep: Callable[[float], Awaitable[object]] | None = None,
    metrics: ClientMetrics | None = None,
    auth_error_predicate: Callable[[Exception], bool] = is_auth_error,
) -> AuthRefreshMiddleware:
    """Build an ``AuthRefreshMiddleware`` with sensible defaults for tests."""

    async def _noop_refresh() -> None:
        return None

    return AuthRefreshMiddleware(
        refresh_callable=refresh_callable or _noop_refresh,
        is_auth_error=auth_error_predicate,
        refresh_callback_enabled=lambda: refresh_enabled,
        refresh_retry_delay=lambda: refresh_retry_delay,
        sleep=sleep,
        metrics=metrics,
    )


# ---------------------------------------------------------------------------
# Pass-through paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passes_through_on_success() -> None:
    """No exception → single ``next_call`` invocation, response returned."""
    terminal, calls = _scripted_terminal([httpx.Response(200, content=b"ok")])
    middleware = _make_middleware()
    chain = build_chain([middleware], terminal)

    response = await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert len(calls) == 1
    assert response.response.status_code == 200


@pytest.mark.asyncio
async def test_passes_through_on_rate_limited() -> None:
    """``TransportRateLimited`` propagates to ``RetryMiddleware`` outside."""
    request = httpx.Request("POST", "https://example.test/x")
    boom = TransportRateLimited(
        "rate limited",
        retry_after=1,
        response=httpx.Response(429, request=request),
        original=httpx.HTTPStatusError("HTTP 429", request=request, response=httpx.Response(429)),
    )
    terminal, calls = _scripted_terminal([boom])
    middleware = _make_middleware()
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportRateLimited) as excinfo:
        await chain(make_request())

    assert excinfo.value is boom
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_passes_through_on_server_error() -> None:
    """``TransportServerError`` propagates."""
    request = httpx.Request("POST", "https://example.test/x")
    boom = TransportServerError(
        "server error",
        original=httpx.HTTPStatusError("HTTP 503", request=request, response=httpx.Response(503)),
    )
    terminal, calls = _scripted_terminal([boom])
    middleware = _make_middleware()
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportServerError) as excinfo:
        await chain(make_request())

    assert excinfo.value is boom
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_passes_through_when_refresh_callback_not_configured() -> None:
    """No refresh callback → propagate auth error without refresh attempt.

    Matches the legacy ``host._refresh_callback is not None`` gate in the
    leaf — a client constructed without ``refresh_callback`` should not
    silently swallow auth errors.
    """
    boom = _auth_error(status=401)
    terminal, calls = _scripted_terminal([boom])
    refresh_calls: list[None] = []

    async def refresh() -> None:
        refresh_calls.append(None)

    middleware = _make_middleware(refresh_callable=refresh, refresh_enabled=False)
    chain = build_chain([middleware], terminal)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await chain(make_request())

    assert excinfo.value is boom
    assert len(calls) == 1
    assert refresh_calls == []  # refresh NOT triggered


@pytest.mark.asyncio
async def test_passes_through_on_non_auth_http_error() -> None:
    """A non-auth HTTPStatusError (e.g. 404) propagates without refresh."""
    boom = _auth_error(status=404)  # 404 is NOT 400/401/403
    terminal, calls = _scripted_terminal([boom])
    refresh_calls: list[None] = []

    async def refresh() -> None:
        refresh_calls.append(None)

    middleware = _make_middleware(refresh_callable=refresh)
    chain = build_chain([middleware], terminal)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await chain(make_request())

    assert excinfo.value is boom
    assert refresh_calls == []


# ---------------------------------------------------------------------------
# Refresh-and-retry path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refreshes_and_retries_on_auth_error() -> None:
    """401 → refresh → retry → success."""
    boom = _auth_error(status=401)
    terminal, calls = _scripted_terminal([boom, httpx.Response(200, content=b"retry-ok")])
    refresh_calls: list[None] = []

    async def refresh() -> None:
        refresh_calls.append(None)

    middleware = _make_middleware(refresh_callable=refresh)
    chain = build_chain([middleware], terminal)

    response = await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert refresh_calls == [None]
    assert len(calls) == 2  # initial + retry
    assert response.response.status_code == 200
    assert response.response.content == b"retry-ok"


@pytest.mark.parametrize("status", [400, 401, 403])
@pytest.mark.asyncio
async def test_refreshes_on_all_auth_status_codes(status: int) -> None:
    """Google returns 400 / 401 / 403 for auth errors — all trigger refresh."""
    boom = _auth_error(status=status)
    terminal, calls = _scripted_terminal([boom, httpx.Response(200, content=b"ok")])
    refresh_calls: list[None] = []

    async def refresh() -> None:
        refresh_calls.append(None)

    middleware = _make_middleware(refresh_callable=refresh)
    chain = build_chain([middleware], terminal)

    response = await chain(make_request())

    assert refresh_calls == [None]
    assert len(calls) == 2
    assert response.response.status_code == 200


@pytest.mark.asyncio
async def test_refresh_retry_delay_honored() -> None:
    """``refresh_retry_delay > 0`` → sleep that duration before retry."""
    boom = _auth_error()
    terminal, _calls = _scripted_terminal([boom, httpx.Response(200, content=b"ok")])
    sleep, slept = _recording_sleep()

    middleware = _make_middleware(refresh_retry_delay=1.5, sleep=sleep)
    chain = build_chain([middleware], terminal)

    await chain(make_request())

    assert slept == [1.5]


@pytest.mark.asyncio
async def test_refresh_retry_delay_zero_skips_sleep() -> None:
    """``refresh_retry_delay == 0`` → no sleep call."""
    boom = _auth_error()
    terminal, _calls = _scripted_terminal([boom, httpx.Response(200, content=b"ok")])
    sleep, slept = _recording_sleep()

    middleware = _make_middleware(refresh_retry_delay=0.0, sleep=sleep)
    chain = build_chain([middleware], terminal)

    await chain(make_request())

    assert slept == []


# ---------------------------------------------------------------------------
# Refresh failure → TransportAuthExpired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_failure_raises_transport_auth_expired() -> None:
    """Refresh callable raises → wrap in ``TransportAuthExpired``."""
    boom = _auth_error(status=401)
    terminal, calls = _scripted_terminal([boom])  # only one attempt — refresh fails before retry
    refresh_error = RuntimeError("refresh blew up")

    async def refresh() -> None:
        raise refresh_error

    middleware = _make_middleware(refresh_callable=refresh)
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportAuthExpired) as excinfo:
        await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert excinfo.value.original is boom
    assert excinfo.value.__cause__ is refresh_error
    assert "RPC LIST_NOTEBOOKS" in str(excinfo.value)
    assert len(calls) == 1  # NO retry attempted


# ---------------------------------------------------------------------------
# Once-per-logical-call contract (codex iter-1 catch on PR 12.8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_refresh_skipped_when_context_marks_already_refreshed() -> None:
    """If ``context["auth_refreshed"]`` is set, a fresh 401 propagates.

    Models the load-bearing scenario where ``RetryMiddleware`` (outside
    this middleware in the final chain) re-invokes the chain after a 429
    that fired post-refresh. The retry hits a 401 again; without the
    per-request flag, AuthRefreshMiddleware would refresh a SECOND time.
    With it, the 401 propagates — restoring the pre-PR-12.7
    "one refresh max per logical call" contract.

    Codex iter-1 PR 12.8 finding.
    """
    boom = _auth_error()
    terminal, _calls = _scripted_terminal([boom])
    refresh_calls: list[None] = []

    async def refresh() -> None:
        refresh_calls.append(None)

    middleware = _make_middleware(refresh_callable=refresh)
    chain = build_chain([middleware], terminal)

    # Simulate a chain re-entry where a prior leg already refreshed.
    request = make_request(context={"auth_refreshed": True})

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await chain(request)

    assert excinfo.value is boom
    assert refresh_calls == []  # NO refresh — prior leg already did it


@pytest.mark.asyncio
async def test_context_auth_refreshed_flag_set_after_first_refresh() -> None:
    """A successful refresh marks ``context["auth_refreshed"]`` so a later
    chain re-entry through ``RetryMiddleware`` won't refresh again.
    """
    boom = _auth_error()
    terminal, _calls = _scripted_terminal([boom, httpx.Response(200, content=b"ok")])

    async def refresh() -> None:
        return None

    middleware = _make_middleware(refresh_callable=refresh)
    chain = build_chain([middleware], terminal)

    request = make_request(context={"log_label": "RPC LIST_NOTEBOOKS"})
    await chain(request)

    assert request.context["auth_refreshed"] is True


# ---------------------------------------------------------------------------
# Exactly-one-retry contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_auth_error_on_retry_propagates_no_recursion() -> None:
    """Per ADR-009: "exactly one retry per next_call invocation".

    First call raises 401 → refresh → retry raises 401 again → propagate.
    The middleware must NOT refresh a second time, NOT recurse.
    """
    first_boom = _auth_error(status=401)
    second_boom = _auth_error(status=401)
    terminal, calls = _scripted_terminal([first_boom, second_boom])
    refresh_calls: list[None] = []

    async def refresh() -> None:
        refresh_calls.append(None)

    middleware = _make_middleware(refresh_callable=refresh)
    chain = build_chain([middleware], terminal)

    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await chain(make_request())

    assert excinfo.value is second_boom
    assert len(refresh_calls) == 1  # exactly one refresh
    assert len(calls) == 2  # initial + one retry


# ---------------------------------------------------------------------------
# Metrics emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_increment_on_successful_refresh() -> None:
    """``rpc_auth_retries`` increments once per successful refresh."""
    boom = _auth_error()
    terminal, _calls = _scripted_terminal([boom, httpx.Response(200, content=b"ok")])
    metrics = ClientMetrics()

    async def refresh() -> None:
        return None

    middleware = _make_middleware(refresh_callable=refresh, metrics=metrics)
    chain = build_chain([middleware], terminal)

    await chain(make_request())

    assert metrics.snapshot().rpc_auth_retries == 1


@pytest.mark.asyncio
async def test_metrics_not_incremented_on_refresh_failure() -> None:
    """Refresh failure → no auth-retry metric (no retry happened)."""
    boom = _auth_error()
    terminal, _calls = _scripted_terminal([boom])
    metrics = ClientMetrics()

    async def refresh() -> None:
        raise RuntimeError("refresh failed")

    middleware = _make_middleware(refresh_callable=refresh, metrics=metrics)
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportAuthExpired):
        await chain(make_request())

    assert metrics.snapshot().rpc_auth_retries == 0


# ---------------------------------------------------------------------------
# Log shape preservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_shape_on_successful_refresh(caplog: pytest.LogCaptureFixture) -> None:
    """Log messages match the legacy ``AuthedTransport`` shape verbatim."""
    boom = _auth_error()
    terminal, _calls = _scripted_terminal([boom, httpx.Response(200, content=b"ok")])

    async def refresh() -> None:
        return None

    middleware = _make_middleware(refresh_callable=refresh)
    chain = build_chain([middleware], terminal)

    with caplog.at_level("INFO", logger="notebooklm._core"):
        await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    info_msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert any(
        "RPC LIST_NOTEBOOKS auth error detected, attempting token refresh" in m for m in info_msgs
    )
    assert any("Token refresh successful, retrying RPC LIST_NOTEBOOKS" in m for m in info_msgs)


@pytest.mark.asyncio
async def test_log_shape_on_refresh_failure(caplog: pytest.LogCaptureFixture) -> None:
    """Refresh failure log matches "Token refresh failed: X"."""
    boom = _auth_error()
    terminal, _calls = _scripted_terminal([boom])

    async def refresh() -> None:
        raise RuntimeError("login expired")

    middleware = _make_middleware(refresh_callable=refresh)
    chain = build_chain([middleware], terminal)

    with (
        caplog.at_level("WARNING", logger="notebooklm._core"),
        pytest.raises(TransportAuthExpired),
    ):
        await chain(make_request())

    warn_msgs = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("Token refresh failed: login expired" in m for m in warn_msgs)


# ---------------------------------------------------------------------------
# Log-label fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_log_label_falls_back_to_sentinel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A request without ``log_label`` does not raise ``KeyError``."""
    boom = _auth_error()
    terminal, _calls = _scripted_terminal([boom, httpx.Response(200, content=b"ok")])

    async def refresh() -> None:
        return None

    middleware = _make_middleware(refresh_callable=refresh)
    chain = build_chain([middleware], terminal)

    with caplog.at_level("INFO", logger="notebooklm._core"):
        await chain(make_request(context={}))  # no log_label

    info_msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
    assert any("<unknown-chain-call>" in m for m in info_msgs)


# ---------------------------------------------------------------------------
# Live-bound refresh_retry_delay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_retry_delay_is_live_bound() -> None:
    """Mutating the delay between chain calls takes effect on the next call."""
    delay = [0.0]

    async def refresh() -> None:
        return None

    sleep, slept = _recording_sleep()
    middleware = AuthRefreshMiddleware(
        refresh_callable=refresh,
        is_auth_error=is_auth_error,
        refresh_callback_enabled=lambda: True,
        refresh_retry_delay=lambda: delay[0],
        sleep=sleep,
    )
    boom1 = _auth_error()
    boom2 = _auth_error()
    terminal, _calls = _scripted_terminal(
        [boom1, httpx.Response(200, content=b"ok"), boom2, httpx.Response(200, content=b"ok")]
    )
    chain = build_chain([middleware], terminal)

    # Call 1: delay=0 → no sleep.
    await chain(make_request())
    assert slept == []

    # Mutate the delay.
    delay[0] = 0.5

    # Call 2: delay=0.5 → one sleep of 0.5.
    await chain(make_request())
    assert slept == [0.5]


# ---------------------------------------------------------------------------
# Type hygiene
# ---------------------------------------------------------------------------


def test_middleware_satisfies_protocol() -> None:
    """``AuthRefreshMiddleware`` instance is assignable to ``Middleware``."""
    from notebooklm._middleware import Middleware

    async def _noop() -> None:
        return None

    middleware: Middleware = AuthRefreshMiddleware(
        refresh_callable=_noop,
        is_auth_error=is_auth_error,
        refresh_callback_enabled=lambda: True,
        refresh_retry_delay=lambda: 0.0,
    )
    assert callable(middleware)
