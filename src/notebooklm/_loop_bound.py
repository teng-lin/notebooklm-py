"""Template-method base for the event-loop-affinity ``set_bound_loop`` protocol.

Several runtime collaborators each own a lazily-built ``asyncio`` primitive
(``Lock`` / ``Semaphore`` / ``Condition``) plus a ``_bound_loop`` field that
records the loop ``ClientLifecycle.open()`` ran on. They all expose an
identically-named ``set_bound_loop(loop)`` that the lifecycle calls to
propagate the captured loop. The *bodies* historically diverged in exactly one
axis:

* The trivial owners (``ReqidCounter`` / ``AuthRefreshCoordinator``) only
  stored the new binding.
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
base does not guard *use*, only *rebuild*. Each owner also keeps its own
``reset_after_open`` (they reset different owner-specific state and must not be
unified). The ``_bound_loop`` field name is preserved because
``_runtime/lifecycle.py`` and ``_loop_affinity`` read it directly.
"""

from __future__ import annotations

import asyncio


class LoopBoundPrimitive:
    """Base providing the canonical ``set_bound_loop`` template method.

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


class EpochFenced(LoopBoundPrimitive):
    """Own the common active-generation fence used by runtime resources.

    ``activate`` publishes one resource epoch, ``fence`` retires it before
    asynchronous teardown begins, and ``assert_epoch`` rejects work holding a
    stale lease. Owners provide their diagnostic text and may preserve either
    the standard epoch-detail suffix or a fixed historical message. The
    Android upload boundary also supplies its historical ``RuntimeError``
    subclass.
    """

    def __init__(
        self,
        retired_message: str,
        *,
        error_type: type[RuntimeError] = RuntimeError,
        initially_closing: bool = False,
        assert_loop: bool = False,
        include_epoch_details: bool = True,
    ) -> None:
        self._epoch_retired_message = retired_message
        self._epoch_error_type = error_type
        self._epoch_assert_loop = assert_loop
        self._epoch_include_details = include_epoch_details
        self._active_epoch: int | None = None
        self._closing = initially_closing

    def activate(self, epoch: int) -> None:
        """Publish ``epoch`` as the active resource generation."""
        self._active_epoch = epoch
        self._closing = False

    def fence(self) -> None:
        """Synchronously retire the active generation before teardown."""
        self._closing = True
        self._active_epoch = None

    def assert_epoch(self, expected_epoch: int) -> None:
        """Reject work that does not belong to the active generation."""
        if self._epoch_assert_loop:
            from ._loop_affinity import assert_bound_loop

            assert_bound_loop(self._bound_loop)
        if self._closing or self._active_epoch != expected_epoch:
            message = self._epoch_retired_message
            if self._epoch_include_details:
                message = f"{message} (expected={expected_epoch}, active={self._active_epoch!r})."
            raise self._epoch_error_type(message)


__all__ = ["EpochFenced", "LoopBoundPrimitive"]
