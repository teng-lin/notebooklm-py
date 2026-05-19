"""DrainMiddleware — in-flight transport-operation tracker for the Tier-12 chain.

Per ADR-009 §"Chain ordering" and master plan §2, ``DrainMiddleware`` sits at
the OUTERMOST position of the final Tier-12 chain ``[Drain, Metrics, Retry,
AuthRefresh, ErrorInjection, Tracing]``. PR 12.5 ships it as the first entry
of the three-middleware chain ``[Drain, Metrics, Tracing]``; PRs 12.6–12.8
insert the remaining middlewares BETWEEN ``Drain`` and ``Metrics`` (Retry,
AuthRefresh, ErrorInjection) so Drain stays outermost.

Pure observer of the transport leg with bookkeeping side-effects: brackets
``next_call`` with calls to :meth:`TransportDrainTracker.begin_transport_post`
and :meth:`TransportDrainTracker.finish_transport_post`, propagating the
``log_label`` from ``request.context`` as the tracker label. The chain
caller (``Session._perform_authed_post``) always populates ``log_label``,
so the middleware reads it via ``context["log_label"]`` and falls back to
a synthetic ``"<unknown-chain-call>"`` only for malformed requests.

This PR lifts the drain bookkeeping from
``RpcExecutor.execute_with_telemetry`` (which previously bracketed
``_rpc_call_impl`` with ``_begin_transport_post`` / ``_finish_transport_post``)
and from ``_chat_transport.send_authed_post`` (the chat-streaming entry).
After PR 12.5, drain admission is owned by the chain — the explicit
bookkeeping calls in those two call sites are gone.

Drain admission semantics preserved:
- ``begin_transport_post`` STILL rejects new top-level work once
  ``TransportDrainTracker._draining`` is set, raising ``RuntimeError``.
  This propagates out of ``next_call`` as it always did — the chain
  doesn't swallow it.
- Nested operations (e.g. an RPC issued from inside a source upload
  whose token was admitted before drain started) STILL pass through
  because ``TransportDrainTracker`` looks at ``asyncio.current_task()``'s
  depth, not the chain seam.
- Source-upload and artifact-polling paths (``_source_upload.py``,
  ``_artifact_polling.py``) keep their explicit ``_begin_transport_post`` /
  ``_finish_transport_post`` calls — they bracket logical operations that
  span multiple chain invocations (the upload spans an authed-POST per
  chunk, the poll spans multiple GET attempts), so the chain seam is the
  wrong scope. Those call sites are unchanged.

Failure mode: if ``next_call`` raises, the ``finally`` clause still calls
``finish_transport_post`` so the in-flight counter never orphans a token —
matching the structure of every previous explicit ``begin/finish`` pair
the codebase had before this PR. Same scope (``Exception``-aware via
``try/finally``, not via narrow ``except``) and same reason.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract,
``src/notebooklm/_core_drain.py`` for the underlying tracker, and
``.sisyphus/plans/tier-12-13-greenfield-migration.md`` row 12.5 for the
PR sequence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._middleware import NextCall, RpcRequest, RpcResponse

if TYPE_CHECKING:
    from ._core_drain import TransportDrainTracker


class DrainMiddleware:
    """Middleware that brackets the chain inner call with drain bookkeeping.

    Conforms to :class:`notebooklm._middleware.Middleware` — the
    ``__call__`` signature matches the Protocol so mypy treats instances
    as assignable into a ``Sequence[Middleware]``.

    Holds a reference to the shared :class:`TransportDrainTracker` owned
    by :class:`Session`. The middleware does not own drain state; it
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
        chain unchanged — that's exactly the pre-PR-12.5 behavior;
        ``RpcExecutor.execute_with_telemetry`` and
        ``_chat_transport.send_authed_post`` both let
        ``_begin_transport_post`` errors propagate without catching.
        """
        log_label = request.context.get("log_label", "<unknown-chain-call>")
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
