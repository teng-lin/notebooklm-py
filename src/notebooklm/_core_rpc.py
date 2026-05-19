"""RPC execution collaborator for NotebookLM core operations."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn, Protocol
from urllib.parse import urlencode

import httpx

from ._core_transport import (
    _AuthSnapshot,
    _BuildRequest,
    _parse_retry_after,
    _TransportAuthExpired,
    _TransportRateLimited,
    _TransportServerError,
)
from ._env import get_default_language
from ._idempotency import (
    IDEMPOTENCY_REGISTRY,
    maybe_inject_client_token,
    resolve_effective_disable_internal_retries,
)
from ._logging import get_request_id, reset_request_id, set_request_id
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
from .types import RpcTelemetryEvent

logger = logging.getLogger(__name__)


class DecodeResponse(Protocol):
    def __call__(self, raw: str, rpc_id: str, *, allow_null: bool = False) -> Any: ...


class RpcOwner(Protocol):
    _timeout: float
    _refresh_callback: Callable[[], Awaitable[Any]] | None
    _refresh_retry_delay: float
    _http_client: Any

    async def _perform_authed_post(
        self,
        *,
        build_request: _BuildRequest,
        log_label: str,
        disable_internal_retries: bool = False,
        rpc_method: str | None = None,
    ) -> httpx.Response: ...

    async def _await_refresh(self) -> None: ...

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
    ) -> Any: ...

    async def _rpc_call_impl(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str,
        allow_null: bool,
        _is_retry: bool,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any: ...

    def _increment_metrics(self, **increments: int | float) -> None: ...

    async def _emit_rpc_event(self, event: RpcTelemetryEvent) -> None: ...


class RpcExecutor:
    """Owns raw batchexecute RPC encode, transport dispatch, decode, and retry."""

    def __init__(
        self,
        owner: RpcOwner,
        *,
        decode_response_late_bound: DecodeResponse,
        is_auth_error: Callable[[Exception], bool],
        sleep: Callable[[float], Awaitable[Any]],
    ):
        self._owner = owner
        self._decode_response = decode_response_late_bound
        self._is_auth_error = is_auth_error
        self._sleep = sleep

    async def execute_with_telemetry(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str,
        allow_null: bool,
        _is_retry: bool,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any:
        """Run an RPC wrapped with telemetry, reqid, and drain bookkeeping.

        This is the outer entry point that ``Session.rpc_call`` routes
        through. The body owns the operation-token + metrics + request-id
        wiring that surrounds the raw RPC dispatch. Pure relocation from
        ``Session.rpc_call``: no behavior changes.

        Dispatches through ``self._owner._rpc_call_impl(...)`` (rather than
        the executor's own :meth:`execute` directly) so the long-standing
        ``core._rpc_call_impl = fake`` swap point keeps working for tests
        and any private callers that override the impl by attribute.

        The ``_is_retry`` flag suppresses telemetry/reqid wrapping so the
        decode-time refresh-and-retry leg inherits the parent's
        request id and reports under one ``[req=<id>]`` line in logs.

        The ``operation_variant`` kwarg (default ``None``) routes through
        the :class:`IdempotencyRegistry` lookup in :meth:`execute` so the
        executor can pick a method-variant-specific policy. Currently
        every method default-resolves to ``UNCLASSIFIED`` (silent + no
        behavior change); Wave 2 will populate variant entries.
        """
        if not self._owner._http_client:
            raise RuntimeError("Client not initialized. Use 'async with' context.")

        # Only the outer call mints a request id; the decode-time retry path
        # (``_is_retry=True``) inherits the parent's id so a single
        # decode-error → refresh → retry sequence appears under one
        # ``[req=<id>]`` in the logs. HTTP-status retries (auth + 429) happen
        # inside ``_perform_authed_post`` without recursion, so they don't
        # need this guard.
        if _is_retry:
            return await self._owner._rpc_call_impl(
                method,
                params,
                source_path,
                allow_null,
                _is_retry,
                disable_internal_retries=disable_internal_retries,
                operation_variant=operation_variant,
            )

        self._owner._increment_metrics(rpc_calls_started=1)
        # ``rpc_calls_started`` and reqid stay HERE (outside the chain)
        # because they bracket the entire logical RPC including decode —
        # the chain wraps only the transport leg. Per-attempt latency,
        # ``rpc_calls_succeeded`` / ``rpc_calls_failed``, and
        # ``emit_rpc_event`` live in ``MetricsMiddleware``; drain
        # admission lives in ``DrainMiddleware``.
        _reqid_token = None if get_request_id() is not None else set_request_id()
        try:
            return await self._owner._rpc_call_impl(
                method,
                params,
                source_path,
                allow_null,
                _is_retry,
                disable_internal_retries=disable_internal_retries,
                operation_variant=operation_variant,
            )
        finally:
            if _reqid_token is not None:
                reset_request_id(_reqid_token)

    async def execute(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str,
        allow_null: bool,
        _is_retry: bool,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any:
        start = time.perf_counter()
        logger.debug("RPC %s starting", method.name)

        # Consult the idempotency registry. The registry is the single
        # source of truth for "how should this RPC behave under retry?";
        # the caller's explicit ``disable_internal_retries=True`` always
        # wins (caller intent > policy). For UNCLASSIFIED entries (the
        # B1-foundation default for every method), the effective value
        # equals the caller's value and no log is emitted — today's
        # retries fire identically.
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

        # For CLIENT_TOKEN_DEDUPE policies, inject a fresh ``uuid4().hex``
        # into the registry-named param field UNLESS the caller already
        # populated it. No-op for every other policy, so this is a
        # zero-cost call for the UNCLASSIFIED-everywhere B1 default.
        maybe_inject_client_token(
            IDEMPOTENCY_REGISTRY,
            method,
            params,
            operation_variant=operation_variant,
        )

        # Resolve once per logical call so URL, body, and decode use the same
        # override-aware RPC id.
        resolved_id = resolve_rpc_id(method.name, method.value)
        rpc_request = encode_rpc_request(method, params, rpc_id_override=resolved_id)

        def _build(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            url = self.build_url(method, snapshot, source_path, rpc_id_override=resolved_id)
            body = build_request_body(rpc_request, snapshot.csrf_token)
            return url, body, {}

        try:
            response = await self._owner._perform_authed_post(
                build_request=_build,
                log_label=f"RPC {method.name}",
                disable_internal_retries=effective_disable_internal_retries,
                rpc_method=method.name,
            )
        except _TransportAuthExpired as exc:
            # Preserve the historical raw transport exception on refresh failure.
            raise exc.original from exc.__cause__
        except _TransportRateLimited as exc:
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
        except _TransportServerError as exc:
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
                f"Unexpected _TransportServerError.original type: {type(exc.original)}"
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
            if (
                not _is_retry
                and self._owner._refresh_callback is not None
                and self._is_auth_error(exc)
            ):
                refreshed = await self.try_refresh_and_retry(
                    method,
                    params,
                    source_path,
                    allow_null,
                    exc,
                    disable_internal_retries=disable_internal_retries,
                    operation_variant=operation_variant,
                )
                return refreshed

            logger.error("RPC %s failed after %.3fs", method.name, elapsed)
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
        snapshot: _AuthSnapshot,
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
            retry_after = _parse_retry_after(exc.response.headers.get("retry-after"))
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
                timeout_seconds=self._owner._timeout,
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
    ) -> Any | None:
        """Refresh auth after a decode-time auth error and retry once."""
        logger.info("RPC %s auth error detected, attempting token refresh", method.name)

        try:
            await self._owner._await_refresh()
        except Exception as refresh_error:
            logger.warning("Token refresh failed: %s", refresh_error)
            raise original_error from refresh_error

        if self._owner._refresh_retry_delay > 0:
            await self._sleep(self._owner._refresh_retry_delay)

        logger.info("Token refresh successful, retrying RPC %s", method.name)
        return await self._owner.rpc_call(
            method,
            params,
            source_path,
            allow_null,
            _is_retry=True,
            disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
        )
