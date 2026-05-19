"""Unit tests for :class:`ErrorInjectionMiddleware` (Tier-12 PR 12.6 / PR 12.7).

Pins the contract documented in ``src/notebooklm/_middleware_error_injection.py``
and ADR-009 §"Chain ordering":

- **Pass-through when env var is unset.** The middleware delegates straight
  to ``next_call``; production behavior is byte-for-byte unchanged.
- **Raise transport exceptions for 429 / 5xx (PR 12.7).** When the env var
  resolves to ``"429"`` the middleware raises
  :class:`_TransportRateLimited` (carrying the synthetic ``Retry-After``);
  ``"5xx"`` raises :class:`_TransportServerError`. This is the contract
  that lets the OUTER ``RetryMiddleware`` retry, restoring ADR-009
  §"ErrorInjection inside Retry — synthetic transient failures trigger
  retry" (codex iter-1 catch on PR 12.7).
- **Return synthetic response for expired_csrf (HTTP 400).** That mode
  surfaces as a returned :class:`RpcResponse` until PR 12.8's
  ``AuthRefreshMiddleware`` intercepts and drives refresh-then-retry.
- **Request shape preserved on the wrapped response.** The wrapped
  :class:`httpx.Response` (whether returned or carried in a raised
  exception) has a ``response.request`` attached whose
  ``method`` / ``url`` / ``body`` mirror the incoming
  :class:`RpcRequest`.
- **Builder cached on the instance** so a long-running test suite pays
  the ``importlib`` cost exactly once per middleware.

The tests use a real :class:`ErrorInjectionMiddleware` instance plus the
canonical chain fixtures (``make_request`` and a one-shot terminal stub)
rather than mocking the substitution logic. Activation is flipped via
:func:`monkeypatch.setenv` against ``NOTEBOOKLM_VCR_RECORD_ERRORS`` so the
production env-var resolution code path
(:func:`notebooklm._core_error_injection._get_error_injection_mode`) is
exercised end-to-end.
"""

from __future__ import annotations

import httpx
import pytest

# pytest puts ``tests/`` on ``sys.path``; ``_fixtures.chain`` is the canonical
# import path documented in ``tests/_fixtures/__init__.py``.
from _fixtures.chain import make_request
from notebooklm._core_error_injection import ERROR_INJECT_ENV_VAR
from notebooklm._core_transport import _TransportRateLimited, _TransportServerError
from notebooklm._middleware import NextCall, RpcRequest, RpcResponse, build_chain
from notebooklm._middleware_error_injection import ErrorInjectionMiddleware


def _static_terminal(response: httpx.Response) -> NextCall:
    """Build a chain-terminal coroutine that wraps ``response``."""

    async def terminal(request: RpcRequest) -> RpcResponse:
        return RpcResponse(response=response, context=request.context)

    return terminal


def _recording_terminal() -> tuple[NextCall, list[RpcRequest]]:
    """Build a terminal that records every request it sees."""
    calls: list[RpcRequest] = []

    async def terminal(request: RpcRequest) -> RpcResponse:
        calls.append(request)
        return RpcResponse(
            response=httpx.Response(status_code=200, content=b"leaf-reached"),
            context=request.context,
        )

    return terminal, calls


# ---------------------------------------------------------------------------
# Pass-through when env var is unset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passes_through_when_env_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default path: env var unset → middleware delegates to ``next_call``."""
    monkeypatch.delenv(ERROR_INJECT_ENV_VAR, raising=False)
    terminal, calls = _recording_terminal()
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    response = await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert len(calls) == 1
    assert response.response.status_code == 200
    assert response.response.content == b"leaf-reached"


@pytest.mark.asyncio
async def test_passes_through_when_env_var_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-string env var also resolves to ``None`` → pass-through."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "   ")
    terminal, calls = _recording_terminal()
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    await chain(make_request())

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_passes_through_when_env_var_unknown_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unrecognized mode → ``_get_error_injection_mode`` returns ``None``."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "418")  # not in VALID_ERROR_MODES
    terminal, calls = _recording_terminal()
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    await chain(make_request())

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 429 → raise _TransportRateLimited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_429_mode_raises_transport_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``429`` mode raises :class:`_TransportRateLimited` for ``RetryMiddleware``.

    Restores ADR-009 §"ErrorInjection inside Retry — synthetic transient
    failures trigger retry" (codex iter-1 catch on PR 12.7). The raised
    exception carries the synthetic ``Retry-After`` so the outer retry
    honors rate-limit timing.
    """
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "429")
    terminal, calls = _recording_terminal()
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    with pytest.raises(_TransportRateLimited) as excinfo:
        await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert calls == []
    exc = excinfo.value
    assert exc.retry_after == 1
    assert exc.response is not None
    assert exc.response.status_code == 429
    assert "RPC LIST_NOTEBOOKS" in str(exc)


@pytest.mark.asyncio
async def test_429_response_carries_synthetic_request_url_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synthetic ``httpx.Response.request`` mirrors the chain request."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "429")
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], _static_terminal(httpx.Response(200, content=b"unreached")))

    custom_url = "https://example.test/_/LabsTailwindUi/data/batchexecute?authuser=0"
    with pytest.raises(_TransportRateLimited) as excinfo:
        await chain(make_request(url=custom_url, body=b"chain-body"))

    response = excinfo.value.response
    assert response is not None
    assert response.request is not None
    assert response.request.method == "POST"
    assert str(response.request.url) == custom_url
    assert response.request.content == b"chain-body"


# ---------------------------------------------------------------------------
# 5xx → raise _TransportServerError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_mode_raises_transport_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``5xx`` mode raises :class:`_TransportServerError` for ``RetryMiddleware``."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "5xx")
    terminal, calls = _recording_terminal()
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    with pytest.raises(_TransportServerError) as excinfo:
        await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    assert calls == []
    exc = excinfo.value
    assert exc.status_code == 500
    assert exc.response is not None
    assert "application/json" in exc.response.headers.get("content-type", "")
    assert b'"error"' in exc.response.content


# ---------------------------------------------------------------------------
# expired_csrf → return RpcResponse (PR 12.8 will intercept in AuthRefresh)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_csrf_mode_returns_400_response_for_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``expired_csrf`` mode returns the 400 response as-is — pending PR 12.8.

    PR 12.8's AuthRefreshMiddleware will intercept 400 responses and drive
    the refresh-then-retry flow. Until then, the response propagates.
    """
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "expired_csrf")
    terminal, calls = _recording_terminal()
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    response = await chain(make_request())

    assert calls == []  # leaf NOT reached
    assert response.response.status_code == 400


@pytest.mark.asyncio
async def test_expired_csrf_response_propagates_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """For the returned-response path (400), context is propagated."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "expired_csrf")
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], _static_terminal(httpx.Response(200, content=b"unreached")))

    request_context = {"log_label": "RPC LIST_NOTEBOOKS", "rpc_method": "LIST_NOTEBOOKS"}
    request = make_request(context=request_context)
    response = await chain(request)

    assert response.context is request.context


# ---------------------------------------------------------------------------
# End-to-end: Retry + ErrorInjection actually retries (codex iter-1 finding)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_outside_error_injection_retries_synthetic_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: chain ``[Retry, ErrorInjection]`` retries synthetic 429s.

    This is the codex iter-1 catch: without ErrorInjection raising
    :class:`_TransportRateLimited`, the synthetic 429 flowed back as a
    returned response and Retry never saw it. With the exception-raising
    fix, Retry catches and retries N times before re-raising.
    """
    from notebooklm._middleware_retry import RetryMiddleware

    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "429")
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    error_injection = ErrorInjectionMiddleware()
    retry = RetryMiddleware(
        rate_limit_max_retries=2,
        server_error_max_retries=2,
        sleep=fake_sleep,
    )
    chain = build_chain(
        [retry, error_injection], _static_terminal(httpx.Response(200, content=b"x"))
    )

    with pytest.raises(_TransportRateLimited):
        await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    # 1 initial + 2 retries → 2 sleeps observed.
    assert len(slept) == 2
    # Each sleep honors the synthetic Retry-After=1.
    assert slept == [1.0, 1.0]


@pytest.mark.asyncio
async def test_retry_outside_error_injection_retries_synthetic_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: chain ``[Retry, ErrorInjection]`` retries synthetic 5xx too."""
    from notebooklm._middleware_retry import RetryMiddleware

    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "5xx")
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    error_injection = ErrorInjectionMiddleware()
    retry = RetryMiddleware(
        rate_limit_max_retries=2,
        server_error_max_retries=1,
        sleep=fake_sleep,
    )
    chain = build_chain(
        [retry, error_injection], _static_terminal(httpx.Response(200, content=b"x"))
    )

    with pytest.raises(_TransportServerError):
        await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    # 1 initial + 1 retry → 1 sleep observed.
    assert len(slept) == 1


# ---------------------------------------------------------------------------
# Builder caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builder_loaded_once_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_load_builder`` caches its result on the instance."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "429")
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], _static_terminal(httpx.Response(200, content=b"unreached")))

    assert middleware._builder is None
    with pytest.raises(_TransportRateLimited):
        await chain(make_request())
    first = middleware._builder
    assert first is not None

    with pytest.raises(_TransportRateLimited):
        await chain(make_request())
    assert middleware._builder is first  # same object — no re-load


# ---------------------------------------------------------------------------
# Activation flip mid-chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activation_flip_between_calls_is_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipping the env var between two chain calls observes both modes."""
    monkeypatch.delenv(ERROR_INJECT_ENV_VAR, raising=False)
    terminal, calls = _recording_terminal()
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    # Call 1: pass-through, leaf reached.
    await chain(make_request())
    assert len(calls) == 1

    # Flip on.
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "5xx")

    # Call 2: short-circuit (now raises), leaf NOT reached again.
    with pytest.raises(_TransportServerError):
        await chain(make_request())
    assert len(calls) == 1  # still 1 — leaf was bypassed on second call


# ---------------------------------------------------------------------------
# Type hygiene
# ---------------------------------------------------------------------------


def test_middleware_satisfies_protocol() -> None:
    """``ErrorInjectionMiddleware`` instance is assignable to ``Middleware``."""
    from notebooklm._middleware import Middleware

    middleware: Middleware = ErrorInjectionMiddleware()
    assert callable(middleware)


# ---------------------------------------------------------------------------
# Monkeypatch seam + activation log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monkeypatch_setattr_on_get_error_injection_mode_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The middleware resolves ``_get_error_injection_mode`` through the
    module at call time, so ``monkeypatch.setattr(_core_error_injection,
    "_get_error_injection_mode", …)`` reaches the chain.
    """
    from notebooklm import _core_error_injection as _eim_module

    monkeypatch.delenv(ERROR_INJECT_ENV_VAR, raising=False)
    monkeypatch.setattr(_eim_module, "_get_error_injection_mode", lambda: "5xx")

    terminal, calls = _recording_terminal()
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    with pytest.raises(_TransportServerError):
        await chain(make_request())

    assert calls == []


@pytest.mark.asyncio
async def test_activation_log_fires_once_per_instance(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The "synthetic-error injection enabled" log line fires exactly once."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "5xx")
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], _static_terminal(httpx.Response(200, content=b"x")))

    with caplog.at_level("INFO", logger="notebooklm._core"):
        # Each call raises; this verifies the log is emitted on the FIRST
        # call's path and suppressed on subsequent calls.
        with pytest.raises(_TransportServerError):
            await chain(make_request())
        with pytest.raises(_TransportServerError):
            await chain(make_request())
        with pytest.raises(_TransportServerError):
            await chain(make_request())

    activations = [r for r in caplog.records if "synthetic-error injection enabled" in r.message]
    assert len(activations) == 1


# ---------------------------------------------------------------------------
# Function-call signature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_receives_next_call_and_invokes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the env var is unset, ``__call__`` invokes ``next_call(request)``."""
    monkeypatch.delenv(ERROR_INJECT_ENV_VAR, raising=False)
    seen: list[RpcRequest] = []

    async def next_call(request: RpcRequest) -> RpcResponse:
        seen.append(request)
        return RpcResponse(
            response=httpx.Response(status_code=200, content=b"next-called"),
            context=request.context,
        )

    middleware = ErrorInjectionMiddleware()
    request = make_request()

    response = await middleware(request, next_call)

    assert seen == [request]
    assert response.response.content == b"next-called"
