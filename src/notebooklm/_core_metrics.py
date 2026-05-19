"""Observability metrics helper for :class:`ClientCore`.

Owns the cumulative ``ClientMetricsSnapshot`` counters, the threading lock that
guards them, and the optional ``on_rpc_event`` telemetry callback. Lifted out of
``_core.py`` so the metrics surface has one home (this file) instead of being
woven into ``ClientCore.__init__`` alongside drain, reqid, and auth state.

Design constraints (load-bearing — see ``tests/unit/test_swallow_observability.py``
and ``tests/unit/test_observability.py``):

* ``__init__`` MUST be event-loop-agnostic — it constructs only a
  ``threading.Lock`` and a plain dataclass. Never call
  ``asyncio.get_running_loop()`` or instantiate any ``asyncio.*`` primitive
  here. ``ClientCore`` is routinely built outside a running loop.

* :meth:`emit_rpc_event` MUST stay ``async def`` and ``await
  maybe_await_callback(...)``. The await is the back-pressure mechanism: a
  slow user-supplied callback intentionally throttles the RPC path. Do NOT
  switch to ``asyncio.create_task`` (fire-and-forget) or a custom
  ``asyncio.iscoroutine`` branch — both would break that contract.

* Exceptions raised by the user callback are swallowed and logged at WARNING.
  The categorized-swallow contract in ``test_swallow_observability.py`` covers
  similar sites; do NOT add new log lines on this path.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import replace

from ._callbacks import maybe_await_callback
from ._core_constants import CORE_LOGGER_NAME
from .types import ClientMetricsSnapshot, RpcTelemetryEvent

# Logger name pinned via :data:`CORE_LOGGER_NAME` so users and tests
# that filter by logger name — e.g. ``caplog.at_level("WARNING",
# logger=CORE_LOGGER_NAME)`` in ``test_client_keepalive.py`` — keep
# matching after the extraction.
logger = logging.getLogger(CORE_LOGGER_NAME)


class ClientMetrics:
    """Cumulative observability counters and the optional RPC telemetry callback.

    Owns three pieces of state:

    * ``_metrics_lock`` — ``threading.Lock`` guarding all reads/writes of the
      snapshot. Threading (not asyncio) because the snapshot is also touched
      by sync helpers like ``metrics_snapshot`` and because ``record_*`` calls
      from RPC paths must work whether or not a loop is running.
    * ``_metrics`` — the cumulative :class:`ClientMetricsSnapshot`. Replaced
      atomically under the lock via ``dataclasses.replace`` so external readers
      always see a consistent snapshot.
    * ``_on_rpc_event`` — the user-supplied telemetry callback, or ``None``.
      Read at emit time so test fixtures can swap it after construction.

    Field names are deliberately the same as the legacy ``ClientCore`` ivars
    (``_metrics_lock``, ``_metrics``, ``_on_rpc_event``) so the compat
    ``@property`` bridges on ``ClientCore`` can delegate with
    ``return self._metrics_obj._<attr>`` and stay readable.
    """

    def __init__(
        self,
        *,
        on_rpc_event: Callable[[RpcTelemetryEvent], object] | None = None,
    ) -> None:
        # Threading lock (NOT asyncio.Lock) so this object can be constructed
        # and used outside of a running event loop — see module docstring.
        self._metrics_lock = threading.Lock()
        self._metrics = ClientMetricsSnapshot()
        self._on_rpc_event = on_rpc_event

    def snapshot(self) -> ClientMetricsSnapshot:
        """Return a fresh copy of the cumulative counters.

        ``replace()`` (rather than handing back ``self._metrics``) isolates the
        returned value from any concurrent in-place rebind that may happen on
        another thread — the dataclass is frozen, but the *attribute* on the
        helper is mutable.
        """
        with self._metrics_lock:
            return replace(self._metrics)

    def increment(self, **increments: int | float) -> None:
        """Atomically add to one or more numeric snapshot fields.

        Held under ``_metrics_lock`` so concurrent RPC producers on the same
        client instance see a consistent snapshot.
        """
        with self._metrics_lock:
            values = {
                field_name: getattr(self._metrics, field_name) + increment
                for field_name, increment in increments.items()
            }
            self._metrics = replace(self._metrics, **values)

    def _record_wait(self, total_field: str, max_field: str, wait_seconds: float) -> None:
        """Bump a ``*_total`` field and update its ``*_max`` companion atomically.

        Shared backend for the three public ``record_*_wait`` methods. Kept
        private because the field-name strings are an implementation detail —
        external callers should use the typed public methods.
        """
        with self._metrics_lock:
            self._metrics = replace(
                self._metrics,
                **{
                    total_field: getattr(self._metrics, total_field) + wait_seconds,
                    max_field: max(getattr(self._metrics, max_field), wait_seconds),
                },
            )

    def record_rpc_queue_wait(self, wait_seconds: float) -> None:
        """Record time a caller spent waiting for the RPC semaphore."""
        self._record_wait(
            "rpc_queue_wait_seconds_total", "rpc_queue_wait_seconds_max", wait_seconds
        )

    def record_upload_queue_wait(self, wait_seconds: float) -> None:
        """Record time spent waiting for the upload semaphore."""
        self._record_wait(
            "upload_queue_wait_seconds_total", "upload_queue_wait_seconds_max", wait_seconds
        )

    def record_lock_wait(self, wait_seconds: float) -> None:
        """Record time spent waiting on the ``_reqid_lock`` (or similar)."""
        self._record_wait("lock_wait_seconds_total", "lock_wait_seconds_max", wait_seconds)

    async def emit_rpc_event(self, event: RpcTelemetryEvent) -> None:
        """Await the optional telemetry callback; swallow + log on failure.

        ``async def`` + ``await`` is load-bearing (back-pressure contract) and
        the swallow keeps a misbehaving callback from surfacing as an RPC
        error. See the module docstring for both constraints.
        """
        callback = self._on_rpc_event
        if callback is None:
            return
        try:
            await maybe_await_callback(callback, event)
        except Exception as exc:  # noqa: BLE001 - observability must not alter behavior
            logger.warning("RPC telemetry callback failed: %s", exc)


__all__ = ["ClientMetrics"]
