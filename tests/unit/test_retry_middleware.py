"""Unit tests for :class:`RetryMiddleware` (Tier-12 PR 12.7).

Pins the contract documented in ``src/notebooklm/_middleware/retry.py`` and
ADR-0009 §"Chain ordering":

- **Pass-through on success.** Single ``next_call`` invocation; result
  returned unchanged.
- **Retry on ``TransportRateLimited``** up to ``rate_limit_max_retries``;
  honor ``Retry-After`` when present, otherwise exponential backoff.
- **Retry on ``TransportServerError``** up to ``server_error_max_retries``;
  exponential backoff.
- **Disable gate**: when ``context["disable_internal_retries"]`` is truthy,
  the first failure propagates without retry.
- **Exhaustion**: after the budget is spent, the last exception re-raises
  unchanged so callers
  (``_chat.transport.chat_aware_authed_post``) see the same shape they
  always did.
- **Metrics**: ``rpc_rate_limit_retries`` / ``rpc_server_error_retries``
  increment per retry (NOT for the original failed attempt — same
  semantics as the pre-PR-12.7 legacy loop in
  the transport POST path).
- **Log lines** match the legacy "rate-limited (HTTP 429); sleeping (…);
  retrying (n/N)" / "server/network error (…); backing off …; retrying
  (n/N)" shape so log-grep alerts keep matching.

Tests inject a deterministic ``sleep`` stub so backoff is observed by
duration call, not by wall-clock wait. The stub records every sleep
duration so tests can assert on the timing model without flakiness.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from notebooklm._client_metrics import ClientMetrics
from notebooklm._middleware.core import NextCall, RpcRequest, RpcResponse, build_chain
from notebooklm._middleware.retry import RetryMiddleware
from notebooklm._transport_errors import TransportRateLimited, TransportServerError

# The ``tests/`` package chain is complete; ``tests._fixtures.chain`` is the
# fully-qualified import path documented in ``tests/_fixtures/__init__.py``.
from tests._fixtures.chain import make_request


def _recording_sleep() -> tuple[Callable[[float], Awaitable[None]], list[float]]:
    """Build an async sleep stub that records every duration it's asked to wait.

    Returns ``(sleep, slept)``: tests pass ``sleep`` into ``RetryMiddleware``
    via the ``sleep=`` kwarg and assert on ``slept`` to verify the backoff
    timing without spending wall-clock time.
    """
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return sleep, slept


def _rate_limited(
    *,
    log_label: str = "RPC LIST_NOTEBOOKS",
    retry_after: int | None = None,
    status: int = 429,
) -> TransportRateLimited:
    """Build a ``TransportRateLimited`` instance shaped like the leaf would raise."""
    request = httpx.Request("POST", "https://example.test/x")
    headers = {"retry-after": str(retry_after)} if retry_after is not None else {}
    response = httpx.Response(status, request=request, headers=headers)
    original = httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)
    return TransportRateLimited(
        f"{log_label} rate-limited (HTTP {status})",
        retry_after=retry_after,
        response=response,
        original=original,
    )


def _server_error(
    *,
    log_label: str = "RPC LIST_NOTEBOOKS",
    status: int = 503,
) -> TransportServerError:
    """Build a ``TransportServerError`` instance shaped like the leaf would raise."""
    request = httpx.Request("POST", "https://example.test/x")
    response = httpx.Response(status, request=request)
    original = httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)
    return TransportServerError(
        f"{log_label} server error (HTTP {status})",
        original=original,
        response=response,
        status_code=status,
    )


def _network_error(*, log_label: str = "RPC LIST_NOTEBOOKS") -> TransportServerError:
    """Build a ``TransportServerError`` wrapping an ``httpx.RequestError``."""
    request = httpx.Request("POST", "https://example.test/x")
    original = httpx.RequestError("connect failed", request=request)
    return TransportServerError(
        f"{log_label} network error: {original}",
        original=original,
    )


def _read_timeout(*, log_label: str = "chat.ask") -> TransportServerError:
    """Build a ``TransportServerError`` wrapping an HTTPX read timeout."""
    request = httpx.Request("POST", "https://example.test/x")
    original = httpx.ReadTimeout("read timed out", request=request)
    return TransportServerError(
        f"{log_label} network error: {original}",
        original=original,
    )


def _scripted_terminal(behaviors: list[Any]) -> tuple[NextCall, list[RpcRequest]]:
    """Build a terminal that yields each entry from ``behaviors`` per call.

    Each entry is either an exception (raised) or an ``httpx.Response`` (wrapped
    in an ``RpcResponse`` and returned). The list of received ``RpcRequest``
    instances is exposed so tests can assert call count + per-attempt context.
    """
    calls: list[RpcRequest] = []
    iterator = iter(behaviors)

    async def terminal(request: RpcRequest) -> RpcResponse:
        calls.append(request)
        nxt = next(iterator)
        if isinstance(nxt, BaseException):
            raise nxt
        return RpcResponse(response=nxt, context=request.context)

    return terminal, calls


# ---------------------------------------------------------------------------
# Pass-through on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passes_through_when_no_retryable_failure() -> None:
    """No retry on a clean 200 — single ``next_call`` invocation."""
    sleep, slept = _recording_sleep()
    terminal, calls = _scripted_terminal([httpx.Response(200, content=b"ok")])
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    response = await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert len(calls) == 1
    assert slept == []
    assert response.response.status_code == 200


# ---------------------------------------------------------------------------
# 429 retry path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_on_429_until_success() -> None:
    """Two 429s then success → three total terminal calls, two sleeps."""
    sleep, slept = _recording_sleep()
    terminal, calls = _scripted_terminal(
        [
            _rate_limited(retry_after=1),
            _rate_limited(retry_after=2),
            httpx.Response(200, content=b"ok"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    response = await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert len(calls) == 3
    assert slept == [1.0, 2.0]  # Retry-After values honored verbatim
    assert response.response.status_code == 200


@pytest.mark.asyncio
async def test_429_retry_after_larger_than_remaining_timeout_does_not_sleep() -> None:
    """A large ``Retry-After`` fails fast when no retry can fit in the deadline."""
    slept: list[float] = []
    clock = 0.0

    def monotonic() -> float:
        return clock

    async def sleep(seconds: float) -> None:
        nonlocal clock
        slept.append(seconds)
        clock += seconds

    first_429 = _rate_limited(retry_after=300)
    terminal, calls = _scripted_terminal(
        [
            first_429,
            httpx.Response(200, content=b"late-success"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        retry_timeout=1.0,
        sleep=sleep,
        monotonic=monotonic,
    )
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportRateLimited) as excinfo:
        await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert excinfo.value is first_429
    assert len(calls) == 1
    assert slept == []
    assert clock == 0.0


@pytest.mark.asyncio
async def test_429_does_not_sleep_when_attempt_already_exhausted_retry_timeout() -> None:
    """Time spent in the failed attempt counts against the aggregate retry timeout."""
    slept: list[float] = []
    clock = 0.0
    first_429 = _rate_limited(retry_after=1)
    calls: list[RpcRequest] = []

    def monotonic() -> float:
        return clock

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    async def terminal(request: RpcRequest) -> RpcResponse:
        nonlocal clock
        calls.append(request)
        clock = 1.5
        raise first_429

    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        retry_timeout=1.0,
        sleep=sleep,
        monotonic=monotonic,
    )
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportRateLimited) as excinfo:
        await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert excinfo.value is first_429
    assert len(calls) == 1
    assert slept == []


@pytest.mark.asyncio
async def test_429_retry_timeout_none_disables_aggregate_deadline() -> None:
    """``None`` preserves the historical retry-count-only behavior."""
    sleep, slept = _recording_sleep()
    terminal, calls = _scripted_terminal(
        [
            _rate_limited(retry_after=1),
            httpx.Response(200, content=b"ok"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        retry_timeout=lambda: None,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    response = await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert len(calls) == 2
    assert slept == [1.0]
    assert response.response.status_code == 200


@pytest.mark.asyncio
async def test_429_without_retry_after_uses_exponential_backoff() -> None:
    """``Retry-After`` absent → exponential backoff with min-floor."""
    sleep, slept = _recording_sleep()
    terminal, _calls = _scripted_terminal(
        [
            _rate_limited(retry_after=None),
            httpx.Response(200, content=b"ok"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    # ``compute_backoff_delay`` with attempt=0, base=1.0 → roughly ~1s with
    # jitter. The exact value depends on random.uniform; floor is 0.1s.
    assert len(slept) == 1
    assert slept[0] >= 0.1


@pytest.mark.asyncio
async def test_429_budget_exhausted_reraises_last_exception() -> None:
    """After ``rate_limit_max_retries`` exhausted, the last ``TransportRateLimited`` propagates."""
    sleep, slept = _recording_sleep()
    last = _rate_limited(retry_after=1)
    terminal, calls = _scripted_terminal(
        [
            _rate_limited(retry_after=1),
            _rate_limited(retry_after=1),
            last,  # 3rd attempt — exhausts budget of 2
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=2,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportRateLimited) as excinfo:
        await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert excinfo.value is last
    assert len(calls) == 3  # initial + 2 retries
    assert slept == [1.0, 1.0]


# ---------------------------------------------------------------------------
# 5xx / network retry path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retries_on_503_until_success() -> None:
    """5xx server error → retried; second attempt succeeds."""
    sleep, slept = _recording_sleep()
    terminal, calls = _scripted_terminal(
        [
            _server_error(status=503),
            httpx.Response(200, content=b"ok"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    response = await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert len(calls) == 2
    assert len(slept) == 1
    assert slept[0] >= 0.1
    assert response.response.status_code == 200


@pytest.mark.asyncio
async def test_5xx_backoff_larger_than_remaining_timeout_does_not_sleep() -> None:
    """5xx backoff uses the same aggregate deadline guard as the 429 path."""
    slept: list[float] = []
    clock = 0.0

    def monotonic() -> float:
        return clock

    async def sleep(seconds: float) -> None:
        nonlocal clock
        slept.append(seconds)
        clock += seconds

    first_503 = _server_error(status=503)
    terminal, calls = _scripted_terminal(
        [
            first_503,
            httpx.Response(200, content=b"late-success"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        retry_timeout=0.05,
        sleep=sleep,
        monotonic=monotonic,
    )
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportServerError) as excinfo:
        await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert excinfo.value is first_503
    assert len(calls) == 1
    assert slept == []
    assert clock == 0.0


@pytest.mark.asyncio
async def test_retries_on_network_error_until_success() -> None:
    """``httpx.RequestError`` wrapped as ``TransportServerError`` → retried."""
    sleep, _slept = _recording_sleep()
    terminal, calls = _scripted_terminal(
        [
            _network_error(),
            httpx.Response(200, content=b"ok"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    response = await chain(make_request())

    assert len(calls) == 2
    assert response.response.status_code == 200


@pytest.mark.asyncio
async def test_disable_read_timeout_retries_skips_read_timeout_retry() -> None:
    """Chat can opt out of restarting a slow streamed generation on read timeout."""
    sleep, slept = _recording_sleep()
    boom = _read_timeout()
    terminal, calls = _scripted_terminal([boom, httpx.Response(200, content=b"late")])
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportServerError) as excinfo:
        await chain(
            make_request(
                context={
                    "log_label": "chat.ask",
                    "disable_read_timeout_retries": True,
                }
            )
        )

    assert excinfo.value is boom
    assert len(calls) == 1
    assert slept == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "original",
    [
        httpx.ReadError("peer reset", request=httpx.Request("POST", "https://example.test/x")),
        httpx.RemoteProtocolError(
            "server disconnected", request=httpx.Request("POST", "https://example.test/x")
        ),
    ],
)
async def test_disable_read_timeout_retries_skips_post_send_network_errors(
    original: httpx.RequestError,
) -> None:
    """A connection severed after the chat request was sent is not replayed."""
    sleep, slept = _recording_sleep()
    boom = TransportServerError("chat.ask network error", original=original)
    terminal, calls = _scripted_terminal([boom, httpx.Response(200, content=b"late")])
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportServerError) as excinfo:
        await chain(
            make_request(
                context={
                    "log_label": "chat.ask",
                    "disable_read_timeout_retries": True,
                }
            )
        )

    assert excinfo.value is boom
    assert len(calls) == 1
    assert slept == []


@pytest.mark.asyncio
async def test_disable_read_timeout_retries_still_retries_connect_errors() -> None:
    """Connect-time failures (request not sent) stay retryable even with the gate on."""
    sleep, slept = _recording_sleep()
    request = httpx.Request("POST", "https://example.test/x")
    connect = TransportServerError(
        "chat.ask network error",
        original=httpx.ConnectError("connection refused", request=request),
    )
    terminal, calls = _scripted_terminal([connect, httpx.Response(200, content=b"ok")])
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    result = await chain(
        make_request(
            context={
                "log_label": "chat.ask",
                "disable_read_timeout_retries": True,
            }
        )
    )

    assert result.response.status_code == 200
    assert len(calls) == 2
    assert slept != []


@pytest.mark.asyncio
async def test_5xx_budget_exhausted_reraises_last_exception() -> None:
    """After ``server_error_max_retries`` exhausted, last ``TransportServerError`` propagates."""
    sleep, _slept = _recording_sleep()
    last = _server_error(status=502)
    terminal, calls = _scripted_terminal(
        [
            _server_error(status=502),
            last,
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=1,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportServerError) as excinfo:
        await chain(make_request())

    assert excinfo.value is last
    assert len(calls) == 2  # initial + 1 retry


# ---------------------------------------------------------------------------
# disable_internal_retries gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_internal_retries_skips_429_retry() -> None:
    """When ``context["disable_internal_retries"]`` is True, 429 propagates immediately."""
    sleep, slept = _recording_sleep()
    boom = _rate_limited(retry_after=1)
    terminal, calls = _scripted_terminal([boom])
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportRateLimited) as excinfo:
        await chain(make_request(context={"disable_internal_retries": True}))

    assert excinfo.value is boom
    assert len(calls) == 1  # no retry
    assert slept == []  # no sleep


@pytest.mark.asyncio
async def test_disable_internal_retries_skips_5xx_retry() -> None:
    """When ``context["disable_internal_retries"]`` is True, 5xx propagates immediately."""
    sleep, slept = _recording_sleep()
    boom = _server_error(status=503)
    terminal, calls = _scripted_terminal([boom])
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    with pytest.raises(TransportServerError) as excinfo:
        await chain(make_request(context={"disable_internal_retries": True}))

    assert excinfo.value is boom
    assert len(calls) == 1
    assert slept == []


# ---------------------------------------------------------------------------
# Metrics emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_increment_per_429_retry() -> None:
    """Each 429 retry increments ``rpc_rate_limit_retries`` exactly once."""
    sleep, _slept = _recording_sleep()
    metrics = ClientMetrics()
    terminal, _calls = _scripted_terminal(
        [
            _rate_limited(retry_after=1),
            _rate_limited(retry_after=1),
            httpx.Response(200, content=b"ok"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
        metrics=metrics,
    )
    chain = build_chain([middleware], terminal)

    await chain(make_request())

    # 2 retries observed → 2 increments. The original failed attempt is NOT a
    # "retry" — pre-PR-12.7 behavior. The third call (success) is not counted.
    snapshot = metrics.snapshot()
    assert snapshot.rpc_rate_limit_retries == 2
    # 5xx retries untouched.
    assert snapshot.rpc_server_error_retries == 0


@pytest.mark.asyncio
async def test_metrics_increment_per_5xx_retry() -> None:
    """Each 5xx retry increments ``rpc_server_error_retries`` exactly once."""
    sleep, _slept = _recording_sleep()
    metrics = ClientMetrics()
    terminal, _calls = _scripted_terminal(
        [
            _server_error(status=503),
            httpx.Response(200, content=b"ok"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
        metrics=metrics,
    )
    chain = build_chain([middleware], terminal)

    await chain(make_request())

    snapshot = metrics.snapshot()
    assert snapshot.rpc_server_error_retries == 1
    assert snapshot.rpc_rate_limit_retries == 0


# ---------------------------------------------------------------------------
# Log shape preservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_retry_log_message_shape(caplog: pytest.LogCaptureFixture) -> None:
    """The retry-info log line preserves the pre-PR-12.7 message shape.

    Log-grep alerts in operator dashboards match on
    "rate-limited (HTTP 429); sleeping (…); retrying (n/N)" — drifting
    the message format silently breaks those alerts.
    """
    sleep, _slept = _recording_sleep()
    terminal, _calls = _scripted_terminal(
        [
            _rate_limited(retry_after=1),
            httpx.Response(200, content=b"ok"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    with caplog.at_level("WARNING", logger="notebooklm._core"):
        await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    rate_lines = [r.message for r in caplog.records if "rate-limited" in r.message]
    assert len(rate_lines) == 1
    msg = rate_lines[0]
    assert "RPC LIST_NOTEBOOKS rate-limited (HTTP 429)" in msg
    assert "sleeping (Retry-After=1s)" in msg
    assert "retrying (1/3)" in msg


@pytest.mark.asyncio
async def test_5xx_retry_log_message_shape(caplog: pytest.LogCaptureFixture) -> None:
    """The 5xx retry log line preserves the pre-PR-12.7 message shape."""
    sleep, _slept = _recording_sleep()
    terminal, _calls = _scripted_terminal(
        [
            _server_error(status=502),
            httpx.Response(200, content=b"ok"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    with caplog.at_level("WARNING", logger="notebooklm._core"):
        await chain(make_request(context={"log_label": "RPC GET_NOTEBOOK"}))

    server_lines = [r.message for r in caplog.records if "server/network error" in r.message]
    assert len(server_lines) == 1
    msg = server_lines[0]
    assert "RPC GET_NOTEBOOK server/network error (HTTP 502)" in msg
    assert "retrying (1/3)" in msg


# ---------------------------------------------------------------------------
# Log-label fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_log_label_falls_back_to_sentinel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A request without ``log_label`` admits a retry with a sentinel label.

    Defensive against ``__new__``-built fixtures driving the chain raw.
    The middleware must not raise ``KeyError`` on a missing label —
    matches DrainMiddleware's same fallback (pinned in
    ``test_drain_middleware.py::test_missing_log_label_falls_back_to_sentinel``).
    """
    sleep, _slept = _recording_sleep()
    terminal, _calls = _scripted_terminal(
        [
            _rate_limited(retry_after=1),
            httpx.Response(200, content=b"ok"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    with caplog.at_level("WARNING", logger="notebooklm._core"):
        # context={} — no log_label
        await chain(make_request(context={}))

    rate_lines = [r.message for r in caplog.records if "rate-limited" in r.message]
    assert len(rate_lines) == 1
    assert "<unknown-chain-call>" in rate_lines[0]


# ---------------------------------------------------------------------------
# Mixed-failure budgets are independent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_and_5xx_budgets_are_independent() -> None:
    """A 429 then a 5xx then success → both retries observed; both budgets ticked."""
    sleep, _slept = _recording_sleep()
    metrics = ClientMetrics()
    terminal, calls = _scripted_terminal(
        [
            _rate_limited(retry_after=1),
            _server_error(status=503),
            httpx.Response(200, content=b"ok"),
        ]
    )
    middleware = RetryMiddleware(
        rate_limit_max_retries=1,
        server_error_max_retries=1,
        sleep=sleep,
        metrics=metrics,
    )
    chain = build_chain([middleware], terminal)

    response = await chain(make_request())

    assert len(calls) == 3
    assert response.response.status_code == 200
    snapshot = metrics.snapshot()
    assert snapshot.rpc_rate_limit_retries == 1
    assert snapshot.rpc_server_error_retries == 1


# ---------------------------------------------------------------------------
# Type hygiene
# ---------------------------------------------------------------------------


def test_middleware_satisfies_protocol() -> None:
    """``RetryMiddleware`` instance is assignable to ``Middleware``."""
    from notebooklm._middleware.core import Middleware

    middleware: Middleware = RetryMiddleware(rate_limit_max_retries=3, server_error_max_retries=3)
    assert callable(middleware)


# ---------------------------------------------------------------------------
# Non-retryable exceptions pass through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_transport_exception_propagates_without_retry() -> None:
    """``RetryMiddleware`` only catches transport exceptions; everything else flows up.

    A generic ``RuntimeError`` from a deeper middleware (e.g. drain
    rejection) must propagate without consuming the retry budget.
    Pre-PR-12.7 the legacy transport loop only caught
    ``httpx.HTTPStatusError`` / ``httpx.RequestError``; the middleware
    only catches the two named transport-exception types so
    ``DrainMiddleware``'s ``RuntimeError("draining…")`` still propagates.
    """
    sleep, slept = _recording_sleep()
    boom = RuntimeError("not a transport error")
    terminal, calls = _scripted_terminal([boom])
    middleware = RetryMiddleware(
        rate_limit_max_retries=3,
        server_error_max_retries=3,
        sleep=sleep,
    )
    chain = build_chain([middleware], terminal)

    with pytest.raises(RuntimeError) as excinfo:
        await chain(make_request())

    assert excinfo.value is boom
    assert len(calls) == 1
    assert slept == []
