"""RPC execution collaborator for NotebookLM core operations."""

from __future__ import annotations

__all__ = ["DecodeResponse", "RpcExecutor"]

import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, NoReturn, Protocol
from urllib.parse import urlencode

import httpx

from ._auth_refresh_retry import RefreshBudget, refresh_and_count
from ._deadline import RuntimeDeadline
from ._env import get_default_language
from ._idempotency import (
    IDEMPOTENCY_REGISTRY,
    resolve_effective_disable_internal_retries,
)
from ._logging import get_request_id, reset_request_id, set_request_id
from ._request_types import AuthSnapshot
from ._transport_errors import (
    TransportAuthExpired,
    TransportRateLimited,
    TransportServerError,
    parse_retry_after,
)
from .auth import format_authuser_value
from .rpc import (
    ClientError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCMethod,
    RPCTimeoutError,
    ServerError,
    build_request_body,
    encode_rpc_request,
    get_batchexecute_url,
    resolve_rpc_id,
)

if TYPE_CHECKING:
    from ._client_metrics import ClientMetrics
    from ._kernel import Kernel
    from ._runtime.auth import AuthRefreshCoordinator
    from ._runtime.contracts import RpcCaller
    from ._runtime.transport import RuntimeTransport

logger = logging.getLogger(__name__)


class DecodeResponse(Protocol):
    def __call__(self, raw: str, rpc_id: str, *, allow_null: bool = False) -> Any: ...


class RpcExecutor:
    """Owns raw batchexecute RPC encode, transport dispatch, decode, and retry.

    Per ADR-0014 Rule 5, the constructor takes its four runtime collaborators
    (Kernel, RuntimeTransport, AuthRefreshCoordinator, ClientMetrics) directly
    via keyword-only arguments rather than reaching them through an owner facade.
    """

    def __init__(
        self,
        *,
        kernel: Kernel,
        transport: RuntimeTransport,
        auth_refresh: AuthRefreshCoordinator,
        metrics: ClientMetrics,
        decode_response: DecodeResponse,
        is_auth_error: Callable[[Exception], bool],
        sleep: Callable[[float], Awaitable[Any]],
        timeout_provider: Callable[[], float],
        refresh_callback_enabled_provider: Callable[[], bool],
        refresh_retry_delay_provider: Callable[[], float],
    ):
        self._kernel = kernel
        self._transport = transport
        self._auth_refresh = auth_refresh
        self._metrics = metrics
        self._decode_response = decode_response
        self._is_auth_error = is_auth_error
        self._sleep = sleep
        self._timeout_provider = timeout_provider
        self._refresh_callback_enabled_provider = refresh_callback_enabled_provider
        self._refresh_retry_delay_provider = refresh_retry_delay_provider

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
        _refresh_budget: RefreshBudget | None = None,
        _retry_deadline: RuntimeDeadline | None = None,
    ) -> Any:
        """Run an RPC wrapped with telemetry and request-id bookkeeping.

        This is the logical-RPC entry point that ``NotebookLMClient.rpc_call``
        and every feature API route through. The body owns the metrics +
        request-id wiring that surrounds the raw RPC dispatch.

        The ``_is_retry`` flag suppresses telemetry/reqid wrapping so the
        decode-time refresh-and-retry leg inherits the parent's
        request id and reports under one ``[req=<id>]`` line in logs.

        The ``operation_variant`` kwarg (default ``None``) routes through
        the :class:`IdempotencyRegistry` lookup in :meth:`_execute_once` so the
        executor can pick a method-variant-specific policy for wire shapes
        such as ``ADD_SOURCE`` and ``CREATE_NOTE``.

        ``_refresh_budget`` carries the shared once-per-logical-call
        :class:`notebooklm._auth_refresh_retry.RefreshBudget` across the
        decode-time retry recursion so the HTTP-status refresh layer (in the
        chain) and the decoded-RPC refresh layer (here) cannot both refresh on
        the same logical call (issue #1205). Like ``_is_retry`` it is an
        internal-only parameter (leading underscore): external callers leave it
        ``None``; :meth:`_execute_once` mints one and threads it through the
        chain and the retry recursion.

        ``_retry_deadline`` carries the logical call's aggregate
        :class:`notebooklm._deadline.RuntimeDeadline` (started from
        ``timeout_provider`` on the first ``_execute_once``) across the
        decode-time retry recursion so the post-refresh sleep is clamped to the
        remaining budget instead of overshooting it (issue #1271) — symmetric
        with ``RetryMiddleware`` on the HTTP-status layer. Like
        ``_refresh_budget`` it is internal-only and minted once per logical
        call; threading it through the recursion keeps the budget anchored to
        the original start time rather than resetting it on the retry leg.
        """
        # Pre-open guard — preserves the historical ``RuntimeError`` surface by
        # routing through ``Kernel.get_http_client()`` (which raises the same
        # message when the client hasn't been opened). Going through the
        # kernel accessor instead of the now-narrowed :class:`RpcOwner`
        # Protocol attribute keeps the early-fail behavior intact while
        # removing ``_http_client`` from the Protocol surface.
        self._kernel.get_http_client()

        # Only the outer call mints a request id; the decode-time retry path
        # (``_is_retry=True``) inherits the parent's id so a single
        # decode-error → refresh → retry sequence appears under one
        # ``[req=<id>]`` in the logs. HTTP-status retries (auth + 429) happen
        # inside ``_perform_authed_post`` without recursion, so they don't
        # need this guard.
        if _is_retry:
            return await self._execute_once(
                method,
                params,
                source_path,
                allow_null,
                _is_retry,
                disable_internal_retries=disable_internal_retries,
                operation_variant=operation_variant,
                _refresh_budget=_refresh_budget,
                _retry_deadline=_retry_deadline,
            )

        self._metrics.increment(rpc_calls_started=1)
        # ``rpc_calls_started`` and reqid stay HERE (outside the chain)
        # because they bracket the entire logical RPC including decode —
        # the chain wraps only the transport leg. Per-attempt latency,
        # ``rpc_calls_succeeded`` / ``rpc_calls_failed``, and
        # ``emit_rpc_event`` live in ``MetricsMiddleware``; drain
        # admission lives in ``DrainMiddleware``.
        _reqid_token = None if get_request_id() is not None else set_request_id()
        try:
            return await self._execute_once(
                method,
                params,
                source_path,
                allow_null,
                _is_retry,
                disable_internal_retries=disable_internal_retries,
                operation_variant=operation_variant,
                _refresh_budget=_refresh_budget,
                _retry_deadline=_retry_deadline,
            )
        finally:
            if _reqid_token is not None:
                reset_request_id(_reqid_token)

    async def _execute_once(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str,
        allow_null: bool,
        _is_retry: bool,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
        _refresh_budget: RefreshBudget | None = None,
        _retry_deadline: RuntimeDeadline | None = None,
    ) -> Any:
        start = time.perf_counter()
        logger.debug("RPC %s starting", method.name)

        # Mint the shared once-per-logical-call refresh budget on the FIRST
        # ``_execute_once`` of a logical call (``_refresh_budget is None``). The
        # same instance is threaded into the chain (so the HTTP-status refresh
        # layer in ``AuthRefreshMiddleware`` consumes it) AND into the
        # decode-time retry recursion below, so a ``wire-401 → refresh →
        # decoded-auth-error`` sequence drives ONE refresh (issue #1205).
        # Standalone ``_execute_once`` test calls pass ``None`` and get a fresh
        # budget, preserving the single-refresh-per-call contract in isolation.
        #
        # The aggregate ``RuntimeDeadline`` is minted on the SAME first
        # ``_execute_once`` and threaded through the retry recursion alongside
        # the budget (issue #1271), so the decode-time post-refresh sleep is
        # clamped to the time remaining since the logical call began rather than
        # restarting the clock on the retry leg.
        if _refresh_budget is None:
            _refresh_budget = RefreshBudget()
        if _retry_deadline is None:
            _retry_deadline = self._start_retry_deadline()

        # Consult the idempotency registry. The registry is the single
        # source of truth for "how should this RPC behave under retry?";
        # the caller's explicit ``disable_internal_retries=True`` always
        # wins (caller intent > policy). Read-only and idempotent set-state
        # entries keep the caller's value unchanged, so existing retry
        # defaults remain intact for retry-safe RPCs.
        #
        # The registry call also raises ``IdempotencyVariantError`` if
        # the caller passed an unknown ``operation_variant`` to a method
        # with an explicit variant table.
        effective_disable_internal_retries = resolve_effective_disable_internal_retries(
            IDEMPOTENCY_REGISTRY,
            method,
            caller_disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
        )

        # Resolve once per logical call so URL, body, and decode use the same
        # override-aware RPC id.
        resolved_id = resolve_rpc_id(method.name, method.value)
        rpc_request = encode_rpc_request(method, params, rpc_id_override=resolved_id)

        def _build(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            url = self.build_url(method, snapshot, source_path, rpc_id_override=resolved_id)
            body = build_request_body(rpc_request, snapshot.csrf_token)
            return url, body, {}

        try:
            response = await self._transport.perform_authed_post(
                build_request=_build,
                log_label=f"RPC {method.name}",
                disable_internal_retries=effective_disable_internal_retries,
                rpc_method=method.name,
                refresh_budget=_refresh_budget,
            )
        except TransportAuthExpired as exc:
            # Preserve the historical raw transport exception on refresh failure.
            raise exc.original from exc.__cause__
        except TransportRateLimited as exc:
            elapsed = time.perf_counter() - start
            logger.error("RPC %s failed after %.3fs: HTTP 429", method.name, elapsed)
            msg = f"API rate limit exceeded calling {method.name}"
            if exc.retry_after:
                msg += f". Retry after {exc.retry_after} seconds"
            raise RateLimitError(
                msg,
                method_id=method.value,
                retry_after=exc.retry_after,
            ) from exc.original
        except TransportServerError as exc:
            elapsed = time.perf_counter() - start
            if isinstance(exc.original, httpx.HTTPStatusError):
                logger.error(
                    "RPC %s failed after %.3fs: HTTP %s (server-error retries exhausted)",
                    method.name,
                    elapsed,
                    exc.original.response.status_code,
                )
                self.raise_rpc_error_from_http_status(exc.original, method)

            if isinstance(exc.original, httpx.RequestError):
                logger.error(
                    "RPC %s failed after %.3fs: %s (server-error retries exhausted)",
                    method.name,
                    elapsed,
                    exc.original,
                )
                self.raise_rpc_error_from_request_error(exc.original, method)

            raise TypeError(
                f"Unexpected TransportServerError.original type: {type(exc.original)}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            elapsed = time.perf_counter() - start
            logger.error(
                "RPC %s failed after %.3fs: HTTP %s",
                method.name,
                elapsed,
                exc.response.status_code,
            )
            self.raise_rpc_error_from_http_status(exc, method)

        try:
            result = self._decode_response(response.text, resolved_id, allow_null=allow_null)
            elapsed = time.perf_counter() - start
            logger.debug("RPC %s completed in %.3fs", method.name, elapsed)
            return result
        except RPCError as exc:
            elapsed = time.perf_counter() - start
            # A decoded auth-shaped ``RPCError`` triggers a refresh-and-retry
            # ONLY when the effective idempotency classification permits a
            # replay. ``effective_disable_internal_retries`` folds the
            # registry policy with the caller's intent: for non-idempotent /
            # probe-then-create methods it is forced True, in which case the
            # server may have already committed the write before the
            # auth-shaped error surfaced. Re-POSTing would duplicate the side
            # effect (issue #1157), so we surface the original error and let
            # the caller's probe-then-create wrapper disambiguate instead.
            #
            # ``_refresh_budget.consume()`` is the LAST guard and MUST remain
            # last: it is side-effecting (claims the single refresh allowance),
            # so it relies on ``and`` short-circuit to only consume when every
            # other condition already holds. Reordering it earlier would burn
            # the budget on calls that then fall through to a plain raise. It is
            # the shared once-per-logical-call allowance: it returns ``False``
            # once the HTTP-status layer (``AuthRefreshMiddleware``) has already
            # refreshed on this call, suppressing a redundant decode-time
            # refresh (issue #1205), and it returns ``False`` on the
            # ``_is_retry`` recursion leg — replacing the old ``not _is_retry``
            # gate — so the decode-time retry stays bounded to one.
            if (
                not effective_disable_internal_retries
                and self._refresh_callback_enabled_provider()
                and self._is_auth_error(exc)
                and _refresh_budget.consume()
            ):
                refreshed = await self.try_refresh_and_retry(
                    method,
                    params,
                    source_path,
                    allow_null,
                    exc,
                    disable_internal_retries=disable_internal_retries,
                    operation_variant=operation_variant,
                    _refresh_budget=_refresh_budget,
                    _retry_deadline=_retry_deadline,
                )
                return refreshed

            error_details = [type(exc).__name__]
            if exc.rpc_code is not None:
                error_details.append(f"rpc_code={exc.rpc_code}")
            retry_after = getattr(exc, "retry_after", None)
            if retry_after is not None:
                error_details.append(f"retry_after={retry_after}")
            logger.error(
                "RPC %s failed after %.3fs: %s",
                method.name,
                elapsed,
                " ".join(error_details),
            )
            raise
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            # Narrow on purpose: only genuine shape-drift exceptions (bad
            # JSON, missing keys/indices, type-mismatched access) get wrapped
            # as ``RPCError``. ``AttributeError`` / ``NameError`` / other
            # ``RuntimeError`` subclasses indicate code bugs (typos, broken
            # invariants) and MUST propagate as their native type so they
            # surface unmasked in stack traces and tests. Adding any of those
            # back to this tuple re-introduces the shape-vs-bug conflation
            # this guard exists to remove.
            elapsed = time.perf_counter() - start
            logger.error("RPC %s failed after %.3fs: %s", method.name, elapsed, exc)
            raise RPCError(
                f"Failed to decode response for {method.name}: {exc}",
                method_id=method.value,
            ) from exc

    def build_url(
        self,
        rpc_method: RPCMethod,
        snapshot: AuthSnapshot,
        source_path: str = "/",
        rpc_id_override: str | None = None,
    ) -> str:
        """Build the batchexecute URL from a frozen auth snapshot."""
        rpc_id = rpc_id_override if rpc_id_override is not None else rpc_method.value
        params: dict[str, str] = {
            "rpcids": rpc_id,
            "source-path": source_path,
            "f.sid": snapshot.session_id,
            "hl": get_default_language(),
            "rt": "c",
        }
        if snapshot.account_email or snapshot.authuser:
            params["authuser"] = format_authuser_value(
                snapshot.authuser,
                snapshot.account_email,
            )
        return f"{get_batchexecute_url()}?{urlencode(params)}"

    def raise_rpc_error_from_http_status(
        self,
        exc: httpx.HTTPStatusError,
        method: RPCMethod,
    ) -> NoReturn:
        """Map an HTTP-status failure onto the RPC error hierarchy."""
        status = exc.response.status_code

        if status == 429:
            retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
            msg = f"API rate limit exceeded calling {method.name}"
            if retry_after:
                msg += f". Retry after {retry_after} seconds"
            raise RateLimitError(msg, method_id=method.value, retry_after=retry_after) from exc

        if 500 <= status < 600:
            raise ServerError(
                f"Server error {status} calling {method.name}: {exc.response.reason_phrase}",
                method_id=method.value,
                status_code=status,
            ) from exc

        if 400 <= status < 500 and status not in (401, 403):
            raise ClientError(
                f"Client error {status} calling {method.name}: {exc.response.reason_phrase}",
                method_id=method.value,
                status_code=status,
            ) from exc

        raise RPCError(
            f"HTTP {status} calling {method.name}: {exc.response.reason_phrase}",
            method_id=method.value,
        ) from exc

    def raise_rpc_error_from_request_error(
        self,
        exc: httpx.RequestError,
        method: RPCMethod,
    ) -> NoReturn:
        """Map a non-status transport failure onto NetworkError/RPCTimeoutError."""
        if isinstance(exc, httpx.ConnectTimeout):
            raise NetworkError(
                f"Connection timed out calling {method.name}: {exc}",
                method_id=method.value,
                original_error=exc,
            ) from exc

        if isinstance(exc, httpx.TimeoutException):
            raise RPCTimeoutError(
                f"Request timed out calling {method.name}",
                method_id=method.value,
                timeout_seconds=self._timeout_provider(),
                original_error=exc,
            ) from exc

        if isinstance(exc, httpx.ConnectError):
            raise NetworkError(
                f"Connection failed calling {method.name}: {exc}",
                method_id=method.value,
                original_error=exc,
            ) from exc

        raise NetworkError(
            f"Request failed calling {method.name}: {exc}",
            method_id=method.value,
            original_error=exc,
        ) from exc

    async def try_refresh_and_retry(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str,
        allow_null: bool,
        original_error: Exception,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
        _refresh_budget: RefreshBudget,
        _retry_deadline: RuntimeDeadline | None = None,
    ) -> Any | None:
        """Refresh auth after a decode-time auth error and retry once.

        Shares the refresh body with the HTTP-status layer via
        :func:`notebooklm._auth_refresh_retry.refresh_and_count`. The
        decoded-RPC layer's refresh-failure shape is the ORIGINAL ``RPCError``
        (``original_error``) re-raised ``from refresh_error`` — callers and
        tests pin that exact identity, distinct from the chain layer's
        ``TransportAuthExpired``.

        Unlike the HTTP-status layer, this method increments
        ``rpc_auth_retries`` too (via the shared helper); before the #1205
        consolidation only the chain layer counted the auth retry, which was
        an accidental divergence.

        ``_refresh_budget`` is REQUIRED (no default): this method is only
        reached from :meth:`_execute_once` after its
        ``_refresh_budget.consume()`` gate returned ``True``, so the caller
        always holds the already-consumed shared budget. Forcing it to be
        passed forecloses a contrived direct call from minting a fresh budget
        on the retry leg and allowing a second refresh.

        ``_retry_deadline`` carries the logical call's aggregate
        :class:`notebooklm._deadline.RuntimeDeadline` so the decode-time retry
        honors the timeout the same way the HTTP-status layer does (issue
        #1271): :func:`refresh_and_count` clamps the post-refresh sleep to the
        remaining budget, and once that sleep returns an exhausted deadline
        makes this method *give up* — re-raising ``original_error`` instead of
        issuing a retry POST that would run past the aggregate timeout, exactly
        as ``RetryMiddleware`` re-raises rather than re-invoking the chain. The
        deadline is also threaded into the retry :meth:`rpc_call` so the
        recursion keeps the same anchored deadline. ``None`` reproduces the
        historical unclamped sleep and unconditional retry (e.g. when
        ``timeout_provider`` yields a ``None`` / non-finite timeout).
        """
        await refresh_and_count(
            refresh=self._auth_refresh.await_refresh,
            on_refresh_failure=lambda _refresh_error: original_error,
            sleep=self._sleep,
            refresh_retry_delay=self._refresh_retry_delay_provider(),
            log_label=f"RPC {method.name}",
            logger=logger,
            metrics=self._metrics,
            retry_deadline=_retry_deadline,
        )

        # Give up symmetrically with ``RetryMiddleware`` (issue #1271): if the
        # aggregate budget is already spent after the (clamped) post-refresh
        # sleep, re-raise the original decoded auth error instead of issuing a
        # retry POST that would overshoot the logical call's timeout. The
        # refresh still happened (a productive side effect — the next call on
        # this client benefits from the fresh token), mirroring the HTTP layer
        # where the refresh middleware runs independently of the retry budget.
        if _retry_deadline is not None and _retry_deadline.expired():
            logger.warning("%s", _retry_deadline.timeout_message(f"RPC {method.name} auth retry"))
            raise original_error

        return await self.rpc_call(
            method,
            params,
            source_path,
            allow_null,
            _is_retry=True,
            disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
            _refresh_budget=_refresh_budget,
            _retry_deadline=_retry_deadline,
        )

    def _start_retry_deadline(self) -> RuntimeDeadline | None:
        """Start the logical call's aggregate deadline from ``timeout_provider``.

        Mirrors ``RetryMiddleware._start_retry_deadline`` so both auth-retry
        layers derive their deadline from the same client timeout source
        (``lifecycle._timeout``) and treat a ``None`` or non-finite timeout the
        same way: ``None`` (no clamp), preserving the historical unclamped
        post-refresh sleep when the operator disabled the aggregate timeout. The
        ``None`` guard precedes ``float()`` so a disabled timeout returns no
        deadline instead of raising ``TypeError`` mid-call.
        """
        timeout = self._timeout_provider()
        if timeout is None or not math.isfinite(float(timeout)):
            return None
        return RuntimeDeadline.start(float(timeout))


if TYPE_CHECKING:

    def _assert_rpc_executor_satisfies_rpc_caller(executor: RpcExecutor) -> None:
        _: RpcCaller = executor
