"""ErrorInjectionMiddleware — synthetic-error short-circuit for the Tier-12 chain.

Per ADR-009 §"Chain ordering", ``ErrorInjectionMiddleware`` sits just *inside*
``RetryMiddleware`` / ``AuthRefreshMiddleware`` (which extract in PRs 12.7–12.8)
and just *outside* ``TracingMiddleware``. The final Tier-12 chain (post-PR 12.9,
after ``SemaphoreMiddleware`` was inserted between ``Metrics`` and ``Retry``) is
``[Drain, Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection, Tracing]``. PR 12.6 ships
the interim 4-middleware chain ``[Drain, Metrics, ErrorInjection, Tracing]``;
PRs 12.7–12.8 insert ``Retry`` and ``AuthRefresh`` BETWEEN ``Metrics`` and
``ErrorInjection`` so the ordering rationale holds at every step.

Test-only path. Production behavior is unchanged when ``NOTEBOOKLM_VCR_RECORD_ERRORS``
is unset — the middleware delegates straight to ``next_call``. When the env var
resolves to ``"429"`` / ``"5xx"`` / ``"expired_csrf"`` (via
:func:`_error_injection._get_error_injection_mode`), every chain invocation
short-circuits with a synthetic :class:`httpx.Response` built by
``tests/cassette_patterns.build_synthetic_error_response`` — the chain leaf
(``_perform_authed_post``) is NOT called. The same env-var startup guard
(:func:`_error_injection._refuse_synthetic_error_outside_test_context`)
still fires at ``Session`` construction so a leaked deploy env never reaches
this code path in production.

Tier-12 history: PR 12.6 lifted the substitution from the pre-Tier-12
httpx transport (``_error_injection._SyntheticErrorTransport``,
which wrapped ``httpx.AsyncClient`` in ``ClientLifecycle``) into this
middleware. PR 12.9 deleted the legacy transport class outright; this
middleware is now the only production substitution surface.

Behavior contract:

- Env var unset → ``await next_call(request)`` unchanged (pass-through).
- Env var set, mode ``"429"`` → raise :class:`TransportRateLimited` so the
  OUTER ``RetryMiddleware`` retries (restoring ADR-009 §"ErrorInjection
  inside Retry — synthetic transient failures trigger retry"; codex iter-1
  catch on PR 12.7). The raised exception carries the synthetic
  ``Retry-After`` header so the retry honors the rate-limit timing.
- Env var set, mode ``"5xx"`` → raise :class:`TransportServerError` so
  ``RetryMiddleware`` retries with exponential backoff.
- Env var set, mode ``"expired_csrf"`` (HTTP 400) → raise the raw
  :class:`httpx.HTTPStatusError` so ``AuthRefreshMiddleware`` (outside
  this middleware in the final chain ordering) catches it via
  ``is_auth_error`` and drives the refresh-then-retry flow. Pre-PR-12.6
  this happened naturally because the legacy ``_SyntheticErrorTransport``
  returned the synthetic 400 below httpx, the leaf's ``raise_for_status``
  lifted it into an ``HTTPStatusError``, and the leaf's auth-refresh
  branch handled it. PR 12.8 restores that end-to-end (codex iter-1
  catch on PR 12.8).

All raised exceptions wrap a synthetic :class:`httpx.Response` anchored to
``request.url`` / ``request.headers`` / ``request.body`` so callers
inspecting ``response.request`` see what the leaf would have sent.

Restored retry semantics: pre-PR-12.6 the httpx-layer
:class:`_SyntheticErrorTransport` fired on *every* batchexecute POST,
including the ones ``AuthedTransport.perform_authed_post`` re-issued from
its internal retry loop. PR 12.6 lifted the middleware above the leaf,
which broke that. PR 12.7 added ``RetryMiddleware`` OUTSIDE this
middleware AND has this middleware raise the proper transport exceptions
for 429/5xx so the outer retry actually fires. Each retry re-enters this
middleware, which re-raises — matching the pre-PR-12.6 "every retry
re-fires the synthetic error" behavior bit-for-bit.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract,
``src/notebooklm/_error_injection.py`` for the env-var / startup-guard
helpers, and ``.sisyphus/plans/tier-12-13-greenfield-migration.md`` row 12.6
for the PR sequence.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx

from . import _error_injection
from ._authed_transport import (
    TransportRateLimited,
    TransportServerError,
    parse_retry_after,
)
from ._error_injection import ERROR_INJECT_ENV_VAR
from ._middleware import NextCall, RpcRequest, RpcResponse
from ._session_config import CORE_LOGGER_NAME

# Logger name pinned via :data:`CORE_LOGGER_NAME` so log filters in
# tests — e.g. ``caplog.at_level(..., logger=CORE_LOGGER_NAME)`` — keep
# matching the synthetic-error log line the lifecycle previously emitted.
logger = logging.getLogger(CORE_LOGGER_NAME)

_SyntheticBuilder = Callable[[str], tuple[int, bytes, dict[str, str]]]


class ErrorInjectionMiddleware:
    """Short-circuit chain middleware that returns synthetic error responses.

    Conforms to :class:`notebooklm._middleware.Middleware` — ``__call__``
    matches the Protocol so instances are assignable into a
    ``Sequence[Middleware]``.

    Holds no shared state. The lazily-loaded synthetic-response builder is
    cached on the instance after first activation so a long-running test
    suite doesn't pay the ``importlib`` cost per chain call.
    """

    def __init__(self) -> None:
        # Cached after first ``_load_builder`` call; ``None`` means "not yet loaded".
        self._builder: _SyntheticBuilder | None = None
        # Gates the one-shot "injection enabled" log line — preserves the
        # pre-PR-12.6 ``_session_lifecycle`` log signal that operators running
        # cassette-recording flows rely on to confirm their env var was picked up.
        self._logged_activation = False

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Substitute a synthetic error response when the env var is set.

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
        """
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

        status_code, body, headers = self._load_builder()(mode)
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
        # middlewares actually fire — restoring ADR-009 §"Chain ordering
        # rationale" end-to-end:
        # - 429 → ``TransportRateLimited`` → ``RetryMiddleware`` retries
        #   with Retry-After or exponential backoff
        # - 5xx → ``TransportServerError`` → ``RetryMiddleware`` retries
        #   with exponential backoff
        # - 400 / expired_csrf → raw ``httpx.HTTPStatusError`` →
        #   ``AuthRefreshMiddleware`` catches via ``is_auth_error``,
        #   refreshes, retries once (PR 12.8). Pre-PR-12.6 this happened
        #   naturally because the legacy ``_SyntheticErrorTransport``
        #   returned the synthetic response below httpx and
        #   ``AuthedTransport``'s ``raise_for_status()`` lifted it into
        #   an ``HTTPStatusError`` that the leaf's auth-refresh branch
        #   then handled. Returning a plain ``RpcResponse`` here would
        #   skip ``AuthRefreshMiddleware`` entirely (codex iter-1 catch
        #   on PR 12.7 (429/5xx) + PR 12.8 (400)).
        log_label = request.context.get("log_label", "<unknown-chain-call>")
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

    def _load_builder(self) -> _SyntheticBuilder:
        """Lazy importlib-load of ``tests.cassette_patterns.build_synthetic_error_response``.

        Production code must not import from ``tests/`` at module load —
        installed-package layouts don't ship ``tests/``. The env var that
        gates this whole path is test-only, so this import only ever runs
        in recording / unit-test contexts where ``tests/`` is on disk
        relative to ``src/notebooklm/``.

        This was previously duplicated in the (now-deleted)
        ``_error_injection._SyntheticErrorTransport._load_builder``;
        PR 12.9 removed that class so the chain middleware is the single
        source of truth for synthetic-response loading.
        """
        if self._builder is not None:
            return self._builder
        # Walk up from src/notebooklm/_middleware_error_injection.py to the
        # repo root, then dive into tests/cassette_patterns.py.
        repo_root = Path(__file__).resolve().parent.parent.parent
        target = repo_root / "tests" / "cassette_patterns.py"
        if not target.exists():
            raise RuntimeError(
                f"{ERROR_INJECT_ENV_VAR} is set but "
                f"tests/cassette_patterns.py is not available at {target}. "
                f"This plumbing is test-only — unset {ERROR_INJECT_ENV_VAR} "
                f"to restore normal behavior."
            )
        spec = importlib.util.spec_from_file_location("_notebooklm_cassette_patterns", target)
        # NOT ``assert`` — runtime invariant must survive ``python -O``.
        if spec is None or spec.loader is None:
            raise RuntimeError(
                f"Failed to load module spec for {target}. "
                f"Unset {ERROR_INJECT_ENV_VAR} to restore normal behavior."
            )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        builder = getattr(mod, "build_synthetic_error_response", None)
        if builder is None:
            # Explicit guard so a renamed/removed symbol in
            # tests/cassette_patterns.py surfaces with the same actionable
            # remediation as the missing-file path above — without this,
            # ``cast`` is type-only and the failure would be a bare
            # ``AttributeError`` on the next call to ``builder(mode)``.
            raise RuntimeError(
                f"tests/cassette_patterns.py at {target} does not export "
                f"``build_synthetic_error_response`` — the synthetic-error "
                f"plumbing is misaligned. Unset {ERROR_INJECT_ENV_VAR} to "
                f"restore normal behavior."
            )
        self._builder = cast(_SyntheticBuilder, builder)
        return self._builder


__all__ = ["ErrorInjectionMiddleware"]
