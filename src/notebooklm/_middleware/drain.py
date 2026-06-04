"""DrainMiddleware — in-flight transport-operation tracker for the middleware chain.

Per ADR-0009 §"Chain ordering", ``DrainMiddleware`` sits at
the OUTERMOST position of the chain
``[Drain, Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection, Tracing]``.

Pure observer of the transport leg with bookkeeping side-effects: brackets
``next_call`` with calls to :meth:`TransportDrainTracker.begin_transport_post`
and :meth:`TransportDrainTracker.finish_transport_post`, propagating the
``log_label`` from ``request.context`` as the tracker label. The chain
caller (``RuntimeTransport.perform_authed_post``) always populates ``log_label``,
so the middleware reads it via ``RPC_CONTEXT_LOG_LABEL`` and falls back
to a synthetic ``"<unknown-chain-call>"`` only for malformed requests.

Drain admission is owned by the chain rather than by the logical RPC
wrapper or ``_chat.transport.send_authed_post`` (the chat-streaming
entry); those two call sites carry no explicit bookkeeping calls.

Drain admission semantics:
- ``begin_transport_post`` STILL rejects new top-level work once
  ``TransportDrainTracker._draining`` is set, raising ``RuntimeError``.
  This propagates out of ``next_call`` as it always did — the chain
  doesn't swallow it.
- Nested operations (e.g. an RPC issued from inside a source upload
  whose token was admitted before drain started) STILL pass through
  because ``TransportDrainTracker`` looks at ``asyncio.current_task()``'s
  depth, not the chain seam.
- Source-upload and artifact-polling paths (``_source/upload.py``,
  ``_artifact/polling.py``) keep their explicit ``_begin_transport_post`` /
  ``_finish_transport_post`` calls — they bracket logical operations that
  span multiple chain invocations (the upload spans an authed-POST per
  chunk, the poll spans multiple GET attempts), so the chain seam is the
  wrong scope. Those call sites are unchanged.

Failure mode: if ``next_call`` raises, the ``finally`` clause still calls
``finish_transport_post`` so the in-flight counter never orphans a token —
matching the structure of every previous explicit ``begin/finish`` pair
the codebase had before this PR. Same scope (``Exception``-aware via
``try/finally``, not via narrow ``except``) and same reason.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract and
``src/notebooklm/_transport_drain.py`` for the underlying tracker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .context import RPC_CONTEXT_LOG_LABEL
from .core import NextCall, RpcRequest, RpcResponse

if TYPE_CHECKING:
    from .._transport_drain import TransportDrainTracker


class DrainMiddleware:
    """Middleware that brackets the chain inner call with drain bookkeeping.

    Conforms to :class:`notebooklm._middleware.core.Middleware` — the
    ``__call__`` signature matches the Protocol so mypy treats instances
    as assignable into a ``Sequence[Middleware]``.

    Holds a reference to the shared :class:`TransportDrainTracker` owned
    by :class:`NotebookLMClient`. The middleware does not own drain state; it
    is a write-through into the host's counters. This keeps
    ``drain()``'s view of in-flight work authoritative even when tests
    swap a middleware out (the explicit ``_begin/_finish_transport_post``
    calls in the upload + polling paths still feed the same tracker).
    """

    def __init__(self, drain_tracker: TransportDrainTracker) -> None:
        self._drain_tracker = drain_tracker

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse:
        """Admit + finalize one transport operation around ``next_call``.

        Reads ``log_label`` from ``request.context``: the value is the
        same string callers used to pass directly to
        ``_begin_transport_post`` (e.g. ``"RPC LIST_NOTEBOOKS"`` from the
        RPC path, ``"chat.ask"`` from the chat path). A missing key
        surfaces as a defensive sentinel rather than a ``KeyError`` —
        ``__new__``-built fixtures driving the chain raw might omit it,
        and the operation should still admit + count.

        ``await begin_transport_post`` may raise ``RuntimeError`` when
        the tracker is in draining mode and the current task has no
        prior operation depth. The exception propagates out of the
        chain unchanged; the RPC dispatch path and
        ``_chat.transport.send_authed_post`` both let drain admission errors
        propagate without catching.
        """
        log_label = request.context.get(RPC_CONTEXT_LOG_LABEL, "<unknown-chain-call>")
        token = await self._drain_tracker.begin_transport_post(log_label)
        try:
            return await next_call(request)
        finally:
            # ``finish_transport_post`` is the load-bearing notify path for
            # ``drain()``. The ``finally`` ensures the counter is decremented
            # even when ``next_call`` raises — orphaning a token would stall
            # ``drain`` forever. Matches the structure of every previous
            # explicit begin/finish pair in the codebase.
            await self._drain_tracker.finish_transport_post(token)


__all__ = ["DrainMiddleware"]
