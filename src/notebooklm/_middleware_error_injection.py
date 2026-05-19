"""ErrorInjectionMiddleware — synthetic-error short-circuit for the Tier-12 chain.

Per ADR-009 §"Chain ordering", ``ErrorInjectionMiddleware`` sits just *inside*
``RetryMiddleware`` / ``AuthRefreshMiddleware`` (which extract in PRs 12.7–12.8)
and just *outside* ``TracingMiddleware``. The final Tier-12 chain is
``[Drain, Metrics, Retry, AuthRefresh, ErrorInjection, Tracing]``. PR 12.6 ships
the interim 4-middleware chain ``[Drain, Metrics, ErrorInjection, Tracing]``;
PRs 12.7–12.8 insert ``Retry`` and ``AuthRefresh`` BETWEEN ``Metrics`` and
``ErrorInjection`` so the ordering rationale holds at every step.

Test-only path. Production behavior is unchanged when ``NOTEBOOKLM_VCR_RECORD_ERRORS``
is unset — the middleware delegates straight to ``next_call``. When the env var
resolves to ``"429"`` / ``"5xx"`` / ``"expired_csrf"`` (via
:func:`_core_error_injection._get_error_injection_mode`), every chain invocation
short-circuits with a synthetic :class:`httpx.Response` built by
``tests/cassette_patterns.build_synthetic_error_response`` — the chain leaf
(``_perform_authed_post``) is NOT called. The same env-var startup guard
(:func:`_core_error_injection._refuse_synthetic_error_outside_test_context`)
still fires at ``ClientCore`` construction so a leaked deploy env never reaches
this code path in production.

This PR lifts the substitution from the httpx transport
(:class:`_core_error_injection._SyntheticErrorTransport`, which previously
wrapped ``httpx.AsyncClient`` in ``ClientLifecycle``) into the middleware
chain. After this PR the lifecycle no longer wraps the transport, so the
class is unused production code; PR 12.9 cleanup deletes it. Direct
instantiation tests in ``tests/unit/test_vcr_config.py`` still pass because
the class itself is untouched.

Behavior contract:

- Env var unset → ``await next_call(request)`` unchanged (pass-through).
- Env var set → build the synthetic ``(status_code, body, headers)`` triple,
  wrap as :class:`httpx.Response` anchored to ``request.url`` / ``request.body``
  so callers that inspect ``response.request`` still see a sane request, and
  return :class:`RpcResponse` carrying the synthetic response with
  ``request.context`` propagated unchanged.

Note on retry semantics: the pre-PR-12.6 httpx-layer
:class:`_SyntheticErrorTransport` fired on *every* batchexecute POST, including
the ones that ``AuthedTransport.perform_authed_post`` re-issued from its
internal retry-on-5xx/429 loop. After this PR the middleware short-circuits
*before* the leaf, so that internal retry loop never sees synthetic errors.
PR 12.7 (``RetryMiddleware``) inserts a retry loop OUTSIDE this middleware
and restores the "every retry re-fires the synthetic error" behavior.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract,
``src/notebooklm/_core_error_injection.py`` for the env-var / startup-guard
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

from . import _core_error_injection
from ._core_error_injection import ERROR_INJECT_ENV_VAR
from ._middleware import NextCall, RpcRequest, RpcResponse

# Logger name pinned to ``notebooklm._core`` (not the literal module name) so
# log filters in tests — e.g. ``caplog.at_level(..., logger="notebooklm._core")``
# — keep matching the synthetic-error log line the lifecycle previously emitted.
logger = logging.getLogger("notebooklm._core")

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
        # pre-PR-12.6 ``_core_lifecycle`` log signal that operators running
        # cassette-recording flows rely on to confirm their env var was picked up.
        self._logged_activation = False

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Substitute a synthetic error response when the env var is set.

        Reads the env var via
        :func:`_core_error_injection._get_error_injection_mode` at call
        time (not construction time) so tests that flip the var
        per-test — via :func:`monkeypatch.setenv` or by monkeypatching the
        function itself on :mod:`notebooklm._core_error_injection` —
        see the change without rebuilding the chain. Resolving through the
        module (rather than a value-imported binding) keeps the
        :func:`monkeypatch.setattr` seam live: a value-import would freeze
        the binding at module-load time and silently dead-letter any
        function swap.
        """
        mode = _core_error_injection._get_error_injection_mode()
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
        return RpcResponse(response=response, context=request.context)

    def _load_builder(self) -> _SyntheticBuilder:
        """Lazy importlib-load of ``tests.cassette_patterns.build_synthetic_error_response``.

        Production code must not import from ``tests/`` at module load —
        installed-package layouts don't ship ``tests/``. The env var that
        gates this whole path is test-only, so this import only ever runs
        in recording / unit-test contexts where ``tests/`` is on disk
        relative to ``src/notebooklm/``.

        Mirrors the same lazy-load logic in
        :class:`_core_error_injection._SyntheticErrorTransport._load_builder`
        so the synthetic-response shape stays identical between the
        legacy transport path and the chain path. PR 12.9 removes the
        legacy path and this duplication along with it.
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
