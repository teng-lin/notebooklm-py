"""In-process pending-id registry — provenance for poll → 200 vs 404.

``client.sources.get_or_none`` returns ``None`` and ``poll_status`` returns
``GenerationState.NOT_FOUND`` for *both* the benign post-create lag AND a bogus
id — the underlying API cannot tell them apart. So the server cannot honor
"200-pending for the lag, 404 for a never-created id" from the client alone.

This registry remembers, per notebook, the source/artifact ids that *this
server* created. A poll consults it:

* a **registry-known** id whose status is ``None`` / ``NOT_FOUND`` → still
  pending (the caller keeps polling);
* an **unknown** id → ``404`` (the server never created it);
* a known id that reaches a terminal state (``READY`` / ``COMPLETED`` /
  ``REMOVED`` / ``FAILED``) is dropped — the resource is now listable (or gone),
  so subsequent polls resolve from the client directly.

It is **process-lifetime** and **single-tenant**: a restart loses in-flight ids
(a later poll for a still-pending id falls to ``404`` rather than ``200``), which
is acceptable for personal automation (the caller re-lists / re-polls). There is
no ``/jobs`` resource and no persistence.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

import threading

__all__ = ["PendingRegistry"]


class PendingRegistry:
    """Per-notebook sets of created-but-not-yet-terminal source/artifact ids.

    Thread-safe: ``starlette`` runs sync dependencies / handlers in a thread
    pool, so the registry guards its state with a lock.
    """

    def __init__(self) -> None:
        self._ids: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def record(self, notebook_id: str, resource_id: str) -> None:
        """Remember that this server created ``resource_id`` under ``notebook_id``."""
        with self._lock:
            self._ids.setdefault(notebook_id, set()).add(resource_id)

    def knows(self, notebook_id: str, resource_id: str) -> bool:
        """Return whether ``resource_id`` was recorded under ``notebook_id``."""
        with self._lock:
            return resource_id in self._ids.get(notebook_id, ())

    def drop(self, notebook_id: str, resource_id: str) -> None:
        """Forget ``resource_id`` (it reached a terminal state — now listable/gone)."""
        with self._lock:
            ids = self._ids.get(notebook_id)
            if ids is not None:
                ids.discard(resource_id)
                if not ids:
                    del self._ids[notebook_id]
