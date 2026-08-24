"""TracingBehavior — innermost middleware in the chain.

Per ADR-0009 §"Chain ordering", ``TracingBehavior`` is
the **innermost** wrapper around the ``Kernel.post`` transport leaf. It logs one
"starting" record before invoking ``next_call`` and one "completed"
(success) or "failed" (exception) record after, capturing per-attempt
HTTP-level visibility — including retry attempts, which is why it sits
inside ``RetryBehavior``.

Pure observer: the middleware never mutates ``request`` or transforms
``response``. The frozen ``RpcRequest`` dataclass enforces request
immutability at the type level; the response is forwarded unchanged
(same :class:`RpcResponse` instance) so any middleware above the leaf
sees exactly what the leaf returned.

Trace record fields (emitted via ``logger.<level>(..., extra={...})`` so
structured-logging consumers see them as ``LogRecord`` attributes):

- ``rpc_method`` — value of ``RpcCallState.rpc_method``, supplied by
  ``WebExecutionRuntime._execute_once`` through the transport entry point.
  ``None`` only for the chat streaming path
  (``_web.chat_transport.chat_aware_authed_post`` — chat-side requests are
  not classified RPCs) and for fixtures driving the chain directly.
- ``log_label`` — value of ``RpcCallState.log_label``. The production
  transport entry point always populates this field. It may be ``None`` only
  for a fixture exercising a malformed request.
- ``status_code`` — ``response.response.status_code`` on the success
  record (omitted from "starting" and "failed" records).
- ``duration_ms`` — wall-clock duration of the ``next_call`` invocation
  in milliseconds (via :func:`time.perf_counter`). Omitted from the
  "starting" record; included on success and failure.
- ``exception_type`` — only on the "failed" record; the qualified name of
  the exception class that propagated out of ``next_call``.

Failure mode: if ``next_call`` raises, emit a "failed" trace record at
``logging.WARNING`` and re-raise the original exception unchanged. The
middleware never swallows.

This is a small class on purpose — the contract is "log before, log
after, never touch the payload." More elaborate tracing (span IDs,
``contextvars`` propagation, OpenTelemetry export) is a future concern;
the structured ``extra=`` fields here give downstream observers enough
hooks to attach.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract.
"""

from __future__ import annotations

import logging
import time

from .rpc_call import NextCall, RpcRequest, RpcResponse

logger = logging.getLogger("notebooklm.middleware.tracing")


class TracingBehavior:
    """Innermost behavior — emits a per-attempt trace record around ``next_call``.

    Stateless and constructor-arg-free: the only collaborator is the
    module-level :data:`logger`. Tests that need to capture emitted
    records use stdlib :mod:`logging` machinery (``caplog`` fixture in
    pytest, or :class:`logging.handlers.MemoryHandler` directly).

    Its ``__call__`` signature matches the fixed behavior-call shape composed
    by :class:`RuntimePipeline`.
    """

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Log one "starting" record, invoke ``next_call``, log one terminal record.

        The ``extra=`` mapping turns each key into a ``LogRecord``
        attribute (e.g. ``record.rpc_method``) for structured-logging
        consumers. The message strings themselves stay short and stable
        so a string-matching grep is also possible. Optional typed-state
        fields surface as ``None`` — the production chain always carries
        ``log_label``; ``rpc_method`` is populated by
        ``WebExecutionRuntime._execute_once`` for the RPC path and left
        ``None`` for the chat streaming path.
        """
        rpc_method = request.state.rpc_method
        log_label = request.state.log_label
        # ``base_extra`` is the per-attempt structured-logging keyset shared
        # across the three records this middleware emits. Each terminal
        # record (completed / failed) augments it with record-specific
        # fields (status_code or exception_type, plus duration_ms).
        base_extra: dict[str, object] = {
            "rpc_method": rpc_method,
            "log_label": log_label,
        }

        logger.debug("rpc starting: %s", log_label, extra=base_extra)

        # ``perf_counter`` is monotonic and not affected by system-clock
        # adjustments, so the resulting ``duration_ms`` is safe to use
        # for both per-attempt latency stats and ordering invariants in
        # tests. ``Exception`` (not ``BaseException``) — we want to skip
        # tracing for cooperative-cancellation signals (``KeyboardInterrupt``,
        # ``SystemExit``, ``asyncio.CancelledError``); those are not "RPC
        # failed" events, they are caller-initiated unwinds.
        start = time.perf_counter()
        try:
            response = await next_call(request)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000.0
            exc_type = type(exc).__qualname__
            logger.warning(
                "rpc failed: %s (%s)",
                log_label,
                exc_type,
                extra={
                    **base_extra,
                    "duration_ms": duration_ms,
                    "exception_type": exc_type,
                },
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000.0
        status_code = response.response.status_code
        logger.debug(
            "rpc completed: %s -> %d",
            log_label,
            status_code,
            extra={
                **base_extra,
                "status_code": status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


__all__ = ["TracingBehavior"]
