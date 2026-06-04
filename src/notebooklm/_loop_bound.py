"""Template-method base for the event-loop-affinity ``set_bound_loop`` protocol.

Several runtime collaborators each own a lazily-built ``asyncio`` primitive
(``Lock`` / ``Semaphore`` / ``Condition``) plus a ``_bound_loop`` field that
records the loop ``ClientLifecycle.open()`` ran on. They all expose an
identically-named ``set_bound_loop(loop)`` that the lifecycle calls to
propagate the captured loop. The *bodies* historically diverged in exactly one
axis:

* The trivial owners (``TransportDrainTracker`` / ``ReqidCounter`` /
  ``AuthRefreshCoordinator``) only stored the new binding.
* The clear-on-rebind owners (``ClientComposed`` / ``SourceUploadPipeline`` /
  ``ChatAPI``) additionally discarded their cached loop-bound state *when the
  loop actually changed* so a stale primitive bound to a now-dead loop is never
  reused after a reopen on a different loop.

:class:`LoopBoundPrimitive` factors that single axis into a template method:
``set_bound_loop`` always stores the binding (matching the trivial owners) and
fires :meth:`_on_loop_rebind` only on a *real* change (matching the
clear-on-rebind owners). The hook runs **before** the store so an override sees
both the old and new loop and can clear state captured under the old one.

Scope is deliberately narrow: this base owns the *binding* and the *rebind
hook* only. The cross-loop **assert** (``assert_bound_loop``) stays in
``_loop_affinity`` and is still called at each owner's async entry point — the
mixin does not guard *use*, only *rebuild*. Each owner also keeps its own
``reset_after_open`` (they reset different owner-specific state and must not be
unified). The ``_bound_loop`` field name is preserved because
``_runtime/lifecycle.py`` and ``_loop_affinity`` read it directly.
"""

from __future__ import annotations

import asyncio


class LoopBoundPrimitive:
    """Mixin providing the canonical ``set_bound_loop`` template method.

    Owners inherit this to drop their duplicated ``_bound_loop`` init and
    ``set_bound_loop`` body. Clear-on-rebind owners override
    :meth:`_on_loop_rebind` to discard owner-specific loop-bound state.
    """

    _bound_loop: asyncio.AbstractEventLoop | None = None

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Capture or clear the event-loop binding for the affinity guard.

        Called by ``ClientLifecycle.open`` after it captures the running loop;
        passing ``None`` clears the binding for the next ``open()`` (which
        rebinds to a fresh loop). The :meth:`_on_loop_rebind` hook fires only
        when the loop actually changes, and *before* the new loop is stored, so
        an override can discard state captured under the old loop.
        """
        if loop is not self._bound_loop:
            self._on_loop_rebind(self._bound_loop, loop)
        self._bound_loop = loop

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        """Hook: discard owner-specific loop-bound state on a rebind.

        Invoked by :meth:`set_bound_loop` only when ``new is not old`` and
        before ``_bound_loop`` is updated to ``new``. The default is a no-op
        (the trivial owners only store the binding).
        """


__all__ = ["LoopBoundPrimitive"]
