"""Unit tests for the shared loop-binding and epoch-fencing bases.

The template-method base factors the one axis on which the loop-bound collaborators'
``set_bound_loop`` bodies historically diverged: the trivial owners only stored
the binding, while the clear-on-rebind owners additionally discarded cached
loop-bound state *when the loop actually changed*. The template method always
stores and fires :meth:`_on_loop_rebind` only on a real change, before the
store. These tests pin that contract directly so a future edit to the base can't
silently break either family.
"""

from __future__ import annotations

import asyncio

import pytest

from notebooklm._loop_bound import EpochFenced, LoopBoundPrimitive


class _Recorder(LoopBoundPrimitive):
    """Owner that records every ``_on_loop_rebind`` call and the loop seen."""

    def __init__(self) -> None:
        self.rebinds: list[tuple[object, object]] = []
        # Captures ``self._bound_loop`` *as observed inside the hook* so the
        # tests can assert the hook runs BEFORE the store.
        self.bound_loop_during_hook: list[object] = []

    def _on_loop_rebind(self, old: object, new: object) -> None:
        self.rebinds.append((old, new))
        self.bound_loop_during_hook.append(self._bound_loop)


def test_default_hook_is_a_noop() -> None:
    """The base ``_on_loop_rebind`` does nothing (the trivial-owner default)."""
    base = LoopBoundPrimitive()
    # No exception, no state — the default body is a pass.
    base._on_loop_rebind(None, object())


def test_default_bound_loop_is_none() -> None:
    """A fresh instance starts unbound so the affinity guard is a no-op."""
    assert LoopBoundPrimitive()._bound_loop is None


def test_set_bound_loop_stores_the_binding() -> None:
    """``set_bound_loop`` always records the new loop (matches trivial owners)."""
    rec = _Recorder()
    loop = asyncio.new_event_loop()
    try:
        rec.set_bound_loop(loop)
        assert rec._bound_loop is loop
    finally:
        loop.close()


def test_hook_fires_only_on_a_real_change() -> None:
    """Re-binding the *same* loop must not fire the hook."""
    rec = _Recorder()
    loop = asyncio.new_event_loop()
    try:
        rec.set_bound_loop(loop)
        assert rec.rebinds == [(None, loop)]  # None -> loop is a change
        rec.set_bound_loop(loop)  # same loop: no hook
        assert rec.rebinds == [(None, loop)]
    finally:
        loop.close()


def test_hook_fires_on_each_distinct_rebind_with_old_and_new() -> None:
    """The hook sees the correct ``(old, new)`` pair on every real change."""
    rec = _Recorder()
    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        rec.set_bound_loop(loop_a)
        rec.set_bound_loop(loop_b)
        rec.set_bound_loop(None)
        assert rec.rebinds == [(None, loop_a), (loop_a, loop_b), (loop_b, None)]
    finally:
        loop_a.close()
        loop_b.close()


def test_hook_runs_before_the_store() -> None:
    """``_on_loop_rebind`` must observe the OLD binding (hook before store).

    Clear-on-rebind owners rely on this ordering to discard state captured
    under the old loop before ``_bound_loop`` advances to the new one.
    """
    rec = _Recorder()
    loop_a = asyncio.new_event_loop()
    loop_b = asyncio.new_event_loop()
    try:
        rec.set_bound_loop(loop_a)
        # During the None->A rebind the hook saw the pre-store value (None).
        assert rec.bound_loop_during_hook == [None]
        rec.set_bound_loop(loop_b)
        # During the A->B rebind the hook saw the still-old value (loop_a).
        assert rec.bound_loop_during_hook == [None, loop_a]
        # And the final state is the new loop.
        assert rec._bound_loop is loop_b
    finally:
        loop_a.close()
        loop_b.close()


def test_set_bound_loop_none_on_a_fresh_instance_is_a_noop() -> None:
    """``set_bound_loop(None)`` when already ``None`` is not a change."""
    rec = _Recorder()
    rec.set_bound_loop(None)
    assert rec.rebinds == []
    assert rec._bound_loop is None


def test_epoch_fence_activates_and_retires_one_generation() -> None:
    owner = EpochFenced("test generation is retired")

    owner.activate(7)
    owner.assert_epoch(7)
    owner.fence()

    with pytest.raises(
        RuntimeError,
        match=r"test generation is retired \(expected=7, active=None\)\.",
    ):
        owner.assert_epoch(7)


def test_epoch_fence_uses_the_owner_error_type_and_message() -> None:
    class _Retired(RuntimeError):
        pass

    owner = EpochFenced("custom owner retired", error_type=_Retired)
    owner.activate(3)

    with pytest.raises(_Retired) as raised:
        owner.assert_epoch(4)

    assert str(raised.value) == "custom owner retired (expected=4, active=3)."


def test_epoch_fence_can_preserve_an_exact_fixed_owner_message() -> None:
    owner = EpochFenced(
        "fixed inactive message",
        include_epoch_details=False,
    )
    owner.activate(3)

    with pytest.raises(RuntimeError) as raised:
        owner.assert_epoch(4)

    assert str(raised.value) == "fixed inactive message"
