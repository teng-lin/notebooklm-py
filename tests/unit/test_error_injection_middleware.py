"""Unit tests for :class:`ErrorInjectionMiddleware` (Tier-12 PR 12.6).

Pins the contract documented in ``src/notebooklm/_middleware_error_injection.py``
and ADR-009 §"Chain ordering":

- **Pass-through when env var is unset.** The middleware delegates straight
  to ``next_call``; production behavior is byte-for-byte unchanged.
- **Short-circuit when env var is set.** Every chain invocation returns a
  synthetic :class:`httpx.Response` built by
  ``tests.cassette_patterns.build_synthetic_error_response``; the leaf is
  NOT called.
- **All three synthetic modes** (``"429"`` / ``"5xx"`` / ``"expired_csrf"``)
  produce the documented status code + body + headers shape.
- **Request shape preserved on the synthetic response.** The wrapped
  :class:`httpx.Response` has a ``response.request`` attached whose
  ``method`` / ``url`` / ``body`` mirror the incoming
  :class:`RpcRequest`, so callers that inspect the request after the fact
  still see what they would have sent.
- **Context propagated.** ``RpcResponse.context`` is the same dict object
  as ``RpcRequest.context`` (no copying); middlewares above can see any
  annotations a deeper middleware would have added had the leaf been
  reached.
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
from notebooklm._middleware import NextCall, RpcRequest, RpcResponse, build_chain
from notebooklm._middleware_error_injection import ErrorInjectionMiddleware


def _static_terminal(response: httpx.Response) -> NextCall:
    """Build a chain-terminal coroutine that wraps ``response``.

    The terminal records nothing — tests that want to know whether the
    leaf was reached should use ``_recording_terminal`` instead.
    """

    async def terminal(request: RpcRequest) -> RpcResponse:
        return RpcResponse(response=response, context=request.context)

    return terminal


def _recording_terminal() -> tuple[NextCall, list[RpcRequest]]:
    """Build a terminal that records every request it sees.

    Returns ``(terminal, calls)`` — append the request to ``calls`` before
    returning a default 200 OK. Tests assert on ``len(calls)`` to detect
    whether the leaf was reached.
    """
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
    """An empty-string env var also resolves to ``None`` → pass-through.

    ``_get_error_injection_mode`` strips whitespace; ``""`` and ``"   "``
    both resolve to ``None`` so the middleware must not short-circuit on
    those values. Defends against an operator who set the var via a
    half-baked shell snippet (e.g. ``export NOTEBOOKLM_VCR_RECORD_ERRORS=``).
    """
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
# Short-circuit + synthetic response shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [("429", 429), ("5xx", 500), ("expired_csrf", 400)],
)
@pytest.mark.asyncio
async def test_short_circuits_with_synthetic_response_for_each_mode(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_status: int,
) -> None:
    """Each valid mode → short-circuit + status code from
    ``build_synthetic_error_response``.

    The middleware must NOT call ``next_call`` (the leaf), and the returned
    ``httpx.Response`` must carry the status the builder produces.
    """
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, mode)
    terminal, calls = _recording_terminal()
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    response = await chain(make_request(context={"log_label": "RPC LIST_NOTEBOOKS"}))

    # Leaf NEVER reached.
    assert calls == []
    assert response.response.status_code == expected_status


@pytest.mark.asyncio
async def test_short_circuit_response_carries_retry_after_for_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``429`` mode → synthetic ``Retry-After`` header is forwarded.

    ``build_synthetic_error_response`` sets ``Retry-After: 1`` for the
    rate-limited shape so the eventual ``RetryMiddleware`` (PR 12.7,
    outside this middleware in the final chain) honors it.
    ErrorInjectionMiddleware must forward that header verbatim onto the
    wrapped ``httpx.Response``.
    """
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "429")
    terminal = _static_terminal(httpx.Response(200, content=b"unreached"))
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    response = await chain(make_request())

    assert response.response.status_code == 429
    assert response.response.headers.get("retry-after") == "1"


@pytest.mark.asyncio
async def test_short_circuit_response_carries_json_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic bodies are JSON-shaped → ``Content-Type`` is propagated."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "5xx")
    terminal = _static_terminal(httpx.Response(200, content=b"unreached"))
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    response = await chain(make_request())

    assert "application/json" in response.response.headers.get("content-type", "")
    assert b'"error"' in response.response.content


# ---------------------------------------------------------------------------
# Request-on-synthetic-response shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthetic_response_carries_request_with_original_url_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synthetic ``httpx.Response.request`` mirrors the chain request.

    Anchors the synthetic response to the would-have-been-sent request so
    callers that read ``response.request.url`` / ``response.request.method``
    (e.g. when constructing an ``httpx.HTTPStatusError`` for diagnostic
    messages) still see the URL and body the leaf would have used.
    """
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "429")
    terminal = _static_terminal(httpx.Response(200, content=b"unreached"))
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    custom_url = "https://example.test/_/LabsTailwindUi/data/batchexecute?authuser=0"
    request = make_request(url=custom_url, body=b"chain-body")
    response = await chain(request)

    assert response.response.request is not None
    assert response.response.request.method == "POST"
    assert str(response.response.request.url) == custom_url
    # ``httpx.Request.content`` is bytes; we passed ``body=b"chain-body"`` so the
    # synthetic request mirrors that exact payload.
    assert response.response.request.content == b"chain-body"


# ---------------------------------------------------------------------------
# Context propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthetic_response_propagates_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``RpcResponse.context`` is the same dict as ``RpcRequest.context``.

    The middleware must NOT copy the context — a deeper middleware (had the
    leaf been reached) might have annotated it; an outer middleware reads
    those annotations. The synthetic path can't add annotations the deeper
    middlewares would have added, but it must still propagate the dict
    instance so outer middlewares see a consistent shape.
    """
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "5xx")
    terminal = _static_terminal(httpx.Response(200, content=b"unreached"))
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    request_context = {"log_label": "RPC LIST_NOTEBOOKS", "rpc_method": "LIST_NOTEBOOKS"}
    request = make_request(context=request_context)
    response = await chain(request)

    assert response.context is request.context
    assert response.context["log_label"] == "RPC LIST_NOTEBOOKS"


# ---------------------------------------------------------------------------
# Builder caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_builder_loaded_once_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_load_builder`` caches its result on the instance.

    Importing ``tests.cassette_patterns`` via importlib is non-trivial; pin
    that the second call reuses the cached builder rather than re-importing.
    Asserted via direct attribute inspection (``middleware._builder``) so
    we don't have to stub importlib itself.
    """
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "429")
    terminal = _static_terminal(httpx.Response(200, content=b"unreached"))
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    assert middleware._builder is None
    await chain(make_request())
    first = middleware._builder
    assert first is not None

    await chain(make_request())
    assert middleware._builder is first  # same object — no re-load


# ---------------------------------------------------------------------------
# Activation flip mid-chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activation_flip_between_calls_is_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test that flips the env var between two chain calls observes both modes.

    Pins that the middleware reads the env var at ``__call__`` time, not
    at construction time. The fixture flow that supports this is:

    1. Construct the middleware + chain with env var unset.
    2. First chain call → passes through, leaf reached.
    3. Set the env var.
    4. Second chain call → short-circuits, leaf NOT reached again.

    Catches a regression where someone "optimizes" the env-var read into
    the ``__init__`` (forfeiting per-call activation flips that tests rely on).
    """
    monkeypatch.delenv(ERROR_INJECT_ENV_VAR, raising=False)
    terminal, calls = _recording_terminal()
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    # Call 1: pass-through, leaf reached.
    await chain(make_request())
    assert len(calls) == 1

    # Flip on.
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "5xx")

    # Call 2: short-circuit, leaf NOT reached again.
    response = await chain(make_request())
    assert len(calls) == 1  # still 1 — leaf was bypassed on second call
    assert response.response.status_code == 500


# ---------------------------------------------------------------------------
# Type hygiene
# ---------------------------------------------------------------------------


def test_middleware_satisfies_protocol() -> None:
    """``ErrorInjectionMiddleware`` instance is assignable to ``Middleware``.

    The :class:`notebooklm._middleware.Middleware` Protocol is the only
    thing :func:`build_chain` accepts. mypy would catch a Protocol drift
    at static-analysis time; this runtime check is the regression guard
    that survives a "mypy not run in CI" misconfiguration.
    """
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

    Regression guard: a value-import (``from ._core_error_injection import
    _get_error_injection_mode``) would freeze the binding at module-load
    time, silently dead-lettering this monkeypatch surface.
    ``tests/unit/test_core_lifecycle.py`` relies on the same seam (via the
    ``_core`` re-export) — keeping it live in the middleware too keeps the
    project's test idiom consistent.
    """
    from notebooklm import _core_error_injection as _eim_module

    monkeypatch.delenv(ERROR_INJECT_ENV_VAR, raising=False)
    # Replace the function itself — env var stays unset, so any reader that
    # cached the function by value would return ``None`` and pass through.
    monkeypatch.setattr(_eim_module, "_get_error_injection_mode", lambda: "5xx")

    terminal, calls = _recording_terminal()
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], terminal)

    response = await chain(make_request())

    assert calls == []
    assert response.response.status_code == 500


@pytest.mark.asyncio
async def test_activation_log_fires_once_per_instance(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The "synthetic-error injection enabled" log line fires exactly once.

    Pre-PR-12.6 the lifecycle emitted this at ``open()``; the middleware
    now emits it at first activation. Pinning "once per instance" guards
    against a refactor that drops the ``_logged_activation`` flag and
    spams the log every chain call.
    """
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "5xx")
    middleware = ErrorInjectionMiddleware()
    chain = build_chain([middleware], _static_terminal(httpx.Response(200, content=b"x")))

    with caplog.at_level("INFO", logger="notebooklm._core"):
        await chain(make_request())
        await chain(make_request())
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
    """When the env var is unset, ``__call__`` invokes ``next_call(request)``.

    Direct ``__call__`` test (not via ``build_chain``) so the contract is
    exercised at the Protocol seam — what every other middleware PR
    relies on.
    """
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
