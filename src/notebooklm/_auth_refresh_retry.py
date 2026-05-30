"""Shared auth refresh-and-retry core for the two retry layers.

NotebookLM recovers from an auth failure at **two** distinct layers, and
issue #1205 flagged that they were implemented as divergent copies:

* **HTTP-status layer** — :class:`notebooklm._middleware_auth_refresh.AuthRefreshMiddleware`
  catches a raw ``httpx.HTTPStatusError`` 400/401/403 from ``Kernel.post``,
  refreshes, rebuilds the request envelope, and re-invokes the chain leaf
  once.
* **Decoded-RPC layer** — :meth:`notebooklm._rpc_executor.RpcExecutor.try_refresh_and_retry`
  catches an auth-shaped decoded ``RPCError`` (HTTP 200 carrying an auth
  error in the batchexecute payload), refreshes, and re-calls ``rpc_call``
  once.

The *triggers* are genuinely different (raw transport error vs decoded RPC
error) and must stay separate, but the **refresh-then-retry core** —
"log → await refresh → on failure log+raise → optional sleep → log success
→ count the auth-retry metric" — was duplicated, and the copies had drifted:
they raised different exception shapes on refresh failure and only the
HTTP-status copy incremented ``rpc_auth_retries``.

This module owns that common core exactly once:

* :class:`RefreshBudget` — a single-consume token that bounds a *logical*
  RPC call to **one** refresh across BOTH layers. The executor mints one
  budget per logical ``rpc_call`` and threads it through the chain context
  (so :class:`AuthRefreshMiddleware` sees it) AND keeps a reference for the
  decode-time leg. Without the shared budget a ``wire-401 → refresh →
  decoded-auth-error`` sequence would refresh twice — the HTTP-status copy's
  per-chain ``auth_refreshed`` flag and the decode copy's ``_is_retry`` flag
  could not see each other (issue #1205, the audit's named double-refresh
  concern).
* :func:`refresh_and_count` — the shared refresh body. It performs the log
  lines, the coalesced single-flight refresh, the refresh-failure raise
  (delegated to a caller-supplied ``on_refresh_failure`` so each layer keeps
  its own externally-observable exception shape), the optional post-refresh
  sleep, and the ``rpc_auth_retries`` metric increment. The caller performs
  its own layer-specific retry afterward (chain re-invoke vs ``rpc_call``
  recursion).

The two layers keep their distinct retry *mechanics* and their distinct
*failure exception types* (callers and tests depend on
``TransportAuthExpired`` from the chain and the original ``RPCError`` from
the decoder); what they now share is the refresh core and the once-per-call
budget.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._client_metrics import ClientMetrics


class RefreshBudget:
    """Single-consume once-per-logical-call refresh token.

    A logical RPC call may attempt **at most one** auth refresh, even though
    that call can surface an auth failure at two different layers (a raw
    ``httpx.HTTPStatusError`` from the wire, then — if the first refresh's
    retry succeeds at the HTTP layer but the decoded payload still carries an
    auth error — a decoded ``RPCError``). The same budget instance is shared
    across both layers so the second layer observes the first layer's
    consumption.

    :meth:`consume` returns ``True`` exactly once (transferring the budget to
    the caller) and ``False`` on every subsequent call. It is **not**
    coroutine-safe in the sense of guarding against interleaved ``await``
    across tasks — it is single-threaded asyncio state read and written
    synchronously within one logical call's control flow, mirroring the
    pre-consolidation ``_is_retry`` recursion flag and the per-chain
    ``RPC_CONTEXT_AUTH_REFRESHED`` boolean it unifies.
    """

    __slots__ = ("_available",)

    def __init__(self) -> None:
        self._available = True

    @property
    def available(self) -> bool:
        """``True`` while the single refresh has not yet been consumed."""
        return self._available

    def consume(self) -> bool:
        """Claim the refresh budget. Returns ``True`` only on the first call."""
        if not self._available:
            return False
        self._available = False
        return True


async def refresh_and_count(
    *,
    refresh: Callable[[], Awaitable[object]],
    on_refresh_failure: Callable[[Exception], BaseException],
    sleep: Callable[[float], Awaitable[object]],
    refresh_retry_delay: float,
    log_label: str,
    logger: logging.Logger,
    metrics: ClientMetrics | None,
) -> None:
    """Run the shared refresh body common to both auth-retry layers.

    Sequence (identical to the pre-consolidation copies):

    1. Log ``"<label> auth error detected, attempting token refresh"``.
    2. ``await refresh()`` — the coalesced single-flight refresh
       (``AuthRefreshCoordinator.await_refresh`` in production, directly or
       via the chain host's dynamic delegate).
    3. On refresh failure: log ``"Token refresh failed: <error>"`` and hand
       the original error to ``on_refresh_failure``, which *returns* the
       layer-specific exception to raise (``TransportAuthExpired`` for the
       chain, the original ``RPCError`` for the decoder). This function raises
       it ``from refresh_error`` so the refresh error stays chained as
       ``__cause__`` exactly as both copies did historically.
    4. Optional post-refresh sleep when ``refresh_retry_delay > 0`` — preserves
       the historical timing both copies applied between refresh and retry.
    5. Log ``"Token refresh successful, retrying <label>"``.
    6. Increment ``rpc_auth_retries`` once per successful refresh. (Before
       consolidation only the HTTP-status copy did this; the decoded-RPC copy
       silently skipped it — issue #1205's metrics divergence. Counting on
       both layers is the corrected, unified behavior.)

    Returns ``None`` on success; the caller then performs its own
    layer-specific retry (chain re-invoke vs ``rpc_call`` recursion). On
    refresh failure this never returns — ``on_refresh_failure`` raises.

    The ``raise <exc> from refresh_error`` cause-chaining is performed inside
    this function so the refresh error is attached as ``__cause__`` exactly as
    both copies did historically.
    """
    logger.info("%s auth error detected, attempting token refresh", log_label)
    try:
        await refresh()
    except Exception as refresh_error:
        logger.warning("Token refresh failed: %s", refresh_error)
        raise on_refresh_failure(refresh_error) from refresh_error

    if refresh_retry_delay > 0:
        await sleep(refresh_retry_delay)
    logger.info("Token refresh successful, retrying %s", log_label)
    if metrics is not None:
        metrics.increment(rpc_auth_retries=1)


__all__ = ["RefreshBudget", "refresh_and_count"]
