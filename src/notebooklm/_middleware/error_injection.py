"""ErrorInjectionMiddleware — synthetic-error short-circuit for the chain.

Per ADR-0009 §"Chain ordering", ``ErrorInjectionMiddleware`` sits just *inside*
``RetryMiddleware`` / ``AuthRefreshMiddleware`` and just *outside*
``TracingMiddleware``. The chain is
``[Drain, Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection, Tracing]``.

Test-only path. Production behavior is unchanged when no builder is wired
into the middleware — the constructor's default ``builder=None`` makes
``__call__`` a pass-through even if ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is set.
Production code paths (``MiddlewareChainBuilder`` in ``_middleware/chain.py``)
construct ``ErrorInjectionMiddleware()`` with no builder, so the substitution
path can never fire from a production install. Tests that want the
substitution to fire construct the middleware directly with an explicit
``builder=`` argument (issue #1005 — replaces the previous filesystem-walking
``_load_builder()`` that called ``importlib.util.spec_from_file_location`` on
``tests/cassette_patterns.py`` at runtime, which broke wheel installs and
exposed an arbitrary-code-exec path keyed off the env var).

When a builder IS wired AND the env var resolves to ``"429"`` / ``"5xx"`` /
``"expired_csrf"`` (via :func:`_error_injection._get_error_injection_mode`),
every chain invocation short-circuits with a synthetic :class:`httpx.Response`
built by the injected callable (canonical implementation:
``tests/cassette_patterns.build_synthetic_error_response``) — the chain leaf
(``_perform_authed_post``) is NOT called. The same env-var startup guard
(:func:`_error_injection._refuse_synthetic_error_outside_test_context`)
still fires at client construction (``NotebookLMClient.__init__``) so a leaked
deploy env never reaches production wiring; the builder-not-wired default is the
second line of defense closing the issue-#1005 attack surface.

This middleware is the only production substitution surface for synthetic
errors.

Behavior contract:

- ``builder`` is ``None`` (production default) → ``await next_call(request)``
  unchanged (pass-through), regardless of env var.
- Env var unset → ``await next_call(request)`` unchanged (pass-through),
  regardless of builder.
- Env var set, builder wired, mode ``"429"`` → raise
  :class:`TransportRateLimited` so the OUTER ``RetryMiddleware`` retries
  (restoring ADR-0009 §"ErrorInjection inside Retry — synthetic transient
  failures trigger retry"). The raised
  exception carries the synthetic ``Retry-After`` header so the retry
  honors the rate-limit timing.
- Env var set, builder wired, mode ``"5xx"`` → raise
  :class:`TransportServerError` so ``RetryMiddleware`` retries with
  exponential backoff.
- Env var set, builder wired, mode ``"expired_csrf"`` (HTTP 400) → raise
  the raw :class:`httpx.HTTPStatusError` so ``AuthRefreshMiddleware``
  (outside this middleware in the chain ordering) catches it via
  ``is_auth_error`` and drives the refresh-then-retry flow.

All raised exceptions wrap a synthetic :class:`httpx.Response` anchored to
``request.url`` / ``request.headers`` / ``request.body`` so callers
inspecting ``response.request`` see what the leaf would have sent.

Retry semantics: ``RetryMiddleware`` sits OUTSIDE this middleware, and this
middleware raises the proper transport exceptions for 429/5xx so the outer
retry fires. Each retry re-enters this middleware, which re-raises — so every
retry re-fires the synthetic error.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract and
``src/notebooklm/_error_injection.py`` for the env-var / startup-guard
helpers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx

from .. import _error_injection
from .._error_injection import ERROR_INJECT_ENV_VAR
from .._runtime.config import CORE_LOGGER_NAME
from .._transport_errors import (
    TransportRateLimited,
    TransportServerError,
    parse_retry_after,
)
from .context import RPC_CONTEXT_LOG_LABEL
from .core import NextCall, RpcRequest, RpcResponse

# Logger name pinned via :data:`CORE_LOGGER_NAME` so log filters in
# tests — e.g. ``caplog.at_level(..., logger=CORE_LOGGER_NAME)`` — keep
# matching the synthetic-error log line the lifecycle previously emitted.
logger = logging.getLogger(CORE_LOGGER_NAME)

_SyntheticBuilder = Callable[[str], tuple[int, bytes, dict[str, str]]]


class ErrorInjectionMiddleware:
    """Short-circuit chain middleware that returns synthetic error responses.

    Conforms to :class:`notebooklm._middleware.core.Middleware` — ``__call__``
    matches the Protocol so instances are assignable into a
    ``Sequence[Middleware]``.

    Holds no shared state. The synthetic-response builder is injected by
    the caller (default ``None``); production wiring in
    :class:`notebooklm._middleware.chain.MiddlewareChainBuilder` never
    passes a builder, so the substitution path stays inaccessible from
    installed packages. Tests pass an explicit builder (typically
    ``tests.cassette_patterns.build_synthetic_error_response``) when they
    want the substitution to fire.

    Args:
        builder: Optional callable that maps a mode string (``"429"`` /
            ``"5xx"`` / ``"expired_csrf"``) to a
            ``(status_code, body, headers)`` triple used to build the
            synthetic :class:`httpx.Response`. When ``None`` (production
            default), ``__call__`` is a pass-through even with the env var
            set — this closes issue #1005's attack surface (a leaked env
            var on a user install can no longer trigger any synthetic
            substitution, because the production chain never injects a
            builder).
    """

    def __init__(self, builder: _SyntheticBuilder | None = None) -> None:
        # Production default: no builder wired → middleware is a pass-through
        # regardless of env var state. Tests that want substitution must pass
        # ``builder=tests.cassette_patterns.build_synthetic_error_response``
        # (or any compatible callable) explicitly. See module docstring and
        # issue #1005 for the rationale (the prior implementation walked the
        # filesystem at runtime to ``importlib``-load
        # ``tests/cassette_patterns.py``, which broke wheel installs AND
        # exposed an arbitrary-code-exec path keyed off the env var).
        self._builder: _SyntheticBuilder | None = builder
        # Gates the one-shot "injection enabled" log line — the log signal
        # that operators running cassette-recording flows rely on to confirm
        # their env var was picked up.
        self._logged_activation = False

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Substitute a synthetic error response when env var AND builder are set.

        Reads the env var via
        :func:`_error_injection._get_error_injection_mode` at call
        time (not construction time) so tests that flip the var
        per-test — via :func:`monkeypatch.setenv` or by monkeypatching the
        function itself on :mod:`notebooklm._error_injection` —
        see the change without rebuilding the chain. Resolving through the
        module (rather than a value-imported binding) keeps the
        :func:`monkeypatch.setattr` seam live: a value-import would freeze
        the binding at module-load time and silently dead-letter any
        function swap.

        Pass-through (``await next_call(request)``) happens when EITHER
        gate is open:

        - ``self._builder is None`` (no builder injected — production
          default per issue #1005), OR
        - ``mode is None`` (env var unset / empty / unknown value).

        Builder is checked first to skip the env-var lookup on every RPC
        in production (where ``self._builder`` is always ``None``).
        """
        if self._builder is None:
            return await next_call(request)
        mode = _error_injection._get_error_injection_mode()
        if mode is None:
            return await next_call(request)

        if not self._logged_activation:
            logger.info(
                "synthetic-error injection enabled (mode=%s) — "
                "chain will return substituted responses until %s is unset",
                mode,
                ERROR_INJECT_ENV_VAR,
            )
            self._logged_activation = True

        status_code, body, headers = self._builder(mode)
        # Anchor the synthetic response to the original method/URL/body/headers
        # so callers that inspect ``response.request`` see what the leaf would
        # have sent.
        synthetic_request = httpx.Request(
            method="POST",
            url=request.url,
            headers=dict(request.headers),
            content=request.body,
        )
        response = httpx.Response(
            status_code=status_code,
            headers=headers,
            content=body,
            request=synthetic_request,
        )
        # Raise the proper exception for each mode so the OUTER chain
        # middlewares actually fire — per ADR-0009 §"Chain ordering
        # rationale":
        # - 429 → ``TransportRateLimited`` → ``RetryMiddleware`` retries
        #   with Retry-After or exponential backoff
        # - 5xx → ``TransportServerError`` → ``RetryMiddleware`` retries
        #   with exponential backoff
        # - 400 / expired_csrf → raw ``httpx.HTTPStatusError`` →
        #   ``AuthRefreshMiddleware`` catches via ``is_auth_error``,
        #   refreshes, retries once. Returning a plain ``RpcResponse`` here
        #   would skip ``AuthRefreshMiddleware`` entirely.
        log_label = request.context.get(RPC_CONTEXT_LOG_LABEL, "<unknown-chain-call>")
        original = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=synthetic_request,
            response=response,
        )
        if status_code == 429:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            raise TransportRateLimited(
                f"{log_label} rate-limited (HTTP 429)",
                retry_after=retry_after,
                response=response,
                original=original,
            ) from original
        if 500 <= status_code < 600:
            raise TransportServerError(
                f"{log_label} server error (HTTP {status_code})",
                original=original,
                response=response,
                status_code=status_code,
            ) from original
        # Auth shapes (400 expired_csrf, and any other 4xx the synthetic
        # builder grows in future) propagate as the raw
        # ``HTTPStatusError`` so ``AuthRefreshMiddleware`` can drive the
        # refresh-then-retry flow.
        raise original


__all__ = ["ErrorInjectionMiddleware"]
