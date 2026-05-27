"""Close-ordering contract for ``TransportDrainTracker`` drain hooks (ADR-014 Rule 1).

Wave 2 of the session-decoupling plan moved the drain-hook storage and firing
from ``Session._drain_hooks`` to ``TransportDrainTracker.register_drain_hook``
/ ``run_drain_hooks``. These tests pin the close-ordering contract that the
post-Wave-0.5 code MUST preserve:

* Hooks fire in registration order (insertion-order dict invariant).
* A hook that raises does not block the rest from firing (other hooks still
  run; ``run_drain_hooks`` does not propagate the exception).
* No hooks ever registered → ``run_drain_hooks`` is a clean no-op.

The lifecycle-level "drain hooks run before transport teardown" assertion
lives in ``test_session_lifecycle.py::test_close_runs_drain_hooks_before_transport_teardown``
— this file is the unit-level contract on the tracker itself.
"""

from __future__ import annotations

import pytest

from notebooklm._transport_drain import TransportDrainTracker


@pytest.mark.asyncio
async def test_run_drain_hooks_fires_in_registration_order() -> None:
    """Hooks fire in the order they were registered (dict insertion order).

    Pinned because feature code may register multiple hooks (artifacts.polls,
    research.polls, etc.) and rely on a deterministic shutdown sequence.
    """
    tracker = TransportDrainTracker()
    fired: list[str] = []

    async def hook_a() -> None:
        fired.append("a")

    async def hook_b() -> None:
        fired.append("b")

    async def hook_c() -> None:
        fired.append("c")

    tracker.register_drain_hook("a", hook_a)
    tracker.register_drain_hook("b", hook_b)
    tracker.register_drain_hook("c", hook_c)

    await tracker.run_drain_hooks()

    assert fired == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_run_drain_hooks_continues_when_one_hook_raises(caplog) -> None:
    """A hook that raises must not block the other hooks AND the exception
    must be logged with the hook's registration name.

    Round-2 reviewer fix (PR #1065 gemini + cubic): the prior implementation
    silently swallowed exceptions despite the docstring promising logging.
    Now: misbehaving feature cannot prevent shutdown AND its error is observable
    via ``logger.warning`` at ``notebooklm._transport_drain``.
    """
    tracker = TransportDrainTracker()
    fired: list[str] = []

    async def hook_a() -> None:
        fired.append("a")

    async def hook_b_raises() -> None:
        fired.append("b_raised")
        raise RuntimeError("intentional test failure")

    async def hook_c() -> None:
        fired.append("c")

    tracker.register_drain_hook("a", hook_a)
    tracker.register_drain_hook("b", hook_b_raises)
    tracker.register_drain_hook("c", hook_c)

    with caplog.at_level("WARNING", logger="notebooklm._transport_drain"):
        # ``run_drain_hooks`` itself must not raise even though hook_b does.
        await tracker.run_drain_hooks()

    assert fired == ["a", "b_raised", "c"]
    matching = [r for r in caplog.records if "intentional test failure" in r.getMessage()]
    assert len(matching) == 1, f"expected exactly one warning log for hook 'b'; got {len(matching)}"
    assert "'b'" in matching[0].getMessage(), (
        f"log message must include hook name 'b'; got: {matching[0].getMessage()!r}"
    )


@pytest.mark.asyncio
async def test_run_drain_hooks_is_noop_when_none_registered() -> None:
    """No hooks registered → ``run_drain_hooks`` returns cleanly without I/O."""
    tracker = TransportDrainTracker()
    # No registrations.
    await tracker.run_drain_hooks()  # must not raise


@pytest.mark.asyncio
async def test_register_drain_hook_overwrites_same_name() -> None:
    """Registering the same name twice replaces the prior hook.

    Pinned because ``register_drain_hook`` is documented as "register or
    replace" — re-instantiating a feature inside one session (rare but
    legal) must not double-fire its drain hook.
    """
    tracker = TransportDrainTracker()
    fired: list[str] = []

    async def first() -> None:
        fired.append("first")

    async def second() -> None:
        fired.append("second")

    tracker.register_drain_hook("artifacts.polls", first)
    tracker.register_drain_hook("artifacts.polls", second)  # replaces first

    await tracker.run_drain_hooks()

    assert fired == ["second"]
