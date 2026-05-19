"""Unit tests for the standalone :class:`ReqidCounter` helper.

Exercises the class in isolation from :class:`Session` so the
counter's invariants (monotonicity, lazy lock allocation, type/value
validation, mutator semantics, optional ``on_lock_wait`` hook) are pinned
down without dragging in the full client surface. The ``Session``-shaped
``DeprecationWarning`` contract continues to live in
``tests/unit/test_core_reqid.py``; the concurrent-contention pin continues
to live in ``tests/unit/test_core_reqid_concurrent.py``.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from notebooklm._core_reqid import DEFAULT_BASELINE, DEFAULT_STEP, ReqidCounter

# ---------------------------------------------------------------------------
# Construction / sync accessors
# ---------------------------------------------------------------------------


def test_default_baseline_matches_constant() -> None:
    """Newly-constructed counter starts at :data:`DEFAULT_BASELINE`."""
    counter = ReqidCounter()
    assert counter.value == DEFAULT_BASELINE
    # Also pin the literal so a silent change to ``DEFAULT_BASELINE`` is loud.
    assert DEFAULT_BASELINE == 100000


def test_default_step_constant_matches_signature_default() -> None:
    """Module-level :data:`DEFAULT_STEP` mirrors the method-signature default.

    Introspects ``next_reqid``'s ``step`` parameter to pin the two sources of
    truth together — silently changing one without the other would break the
    chat-API ``_reqid`` contract.
    """
    sig = inspect.signature(ReqidCounter.next_reqid)
    assert sig.parameters["step"].default == DEFAULT_STEP
    # And the literal — so a silent ``DEFAULT_STEP = 1`` is loud.
    assert DEFAULT_STEP == 100000


def test_client_core_next_reqid_default_matches_default_step() -> None:
    """``Session.next_reqid``'s ``step`` default is sourced from
    :data:`notebooklm._core_reqid.DEFAULT_STEP`.

    Pins the facade signature to the helper's constant so a silent change to
    one cannot drift past the other. ``Session`` imports
    ``DEFAULT_STEP as _REQID_DEFAULT_STEP``; this test introspects the bound
    default to confirm the wiring stays in lockstep.
    """
    from notebooklm._session import Session

    sig = inspect.signature(Session.next_reqid)
    assert sig.parameters["step"].default == DEFAULT_STEP


def test_custom_baseline() -> None:
    """Constructor accepts a non-default baseline (used by future fixtures)."""
    counter = ReqidCounter(baseline=42)
    assert counter.value == 42


def test_lock_not_allocated_at_construction() -> None:
    """``asyncio.Lock()`` would bind to a running loop in some Python versions;
    constructing a ``ReqidCounter`` outside one must NOT instantiate the lock.
    """
    counter = ReqidCounter()
    assert counter._lock is None


def test_set_value_mutates_counter() -> None:
    """``set_value`` is the property-setter delegation target."""
    counter = ReqidCounter()
    counter.set_value(0)
    assert counter.value == 0
    counter.set_value(999_999)
    assert counter.value == 999_999


@pytest.mark.asyncio
async def test_set_value_resets_baseline_for_next_reqid() -> None:
    """After ``set_value(N)``, the next ``next_reqid()`` returns ``N + step``.

    Pins the contract between the sync mutator and the async increment path:
    test fixtures that seed the counter to a deterministic value (via
    ``Session._reqid_counter_value = N``) expect subsequent reqids to
    walk forward from there, not from the original baseline.
    """
    counter = ReqidCounter()
    counter.set_value(42)
    assert await counter.next_reqid(step=1) == 43
    assert await counter.next_reqid(step=100) == 143


# ---------------------------------------------------------------------------
# next_reqid — success path + monotonicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_reqid_post_increment_default_step() -> None:
    """Default-step bumps return strictly monotonic post-increment values."""
    counter = ReqidCounter()
    assert await counter.next_reqid() == DEFAULT_BASELINE + DEFAULT_STEP
    assert await counter.next_reqid() == DEFAULT_BASELINE + 2 * DEFAULT_STEP
    assert await counter.next_reqid() == DEFAULT_BASELINE + 3 * DEFAULT_STEP
    assert counter.value == DEFAULT_BASELINE + 3 * DEFAULT_STEP


@pytest.mark.asyncio
async def test_next_reqid_custom_step() -> None:
    """Custom ``step`` parameter is honoured (small + large)."""
    counter = ReqidCounter()
    assert await counter.next_reqid(step=1) == DEFAULT_BASELINE + 1
    assert await counter.next_reqid(step=7) == DEFAULT_BASELINE + 8
    assert await counter.next_reqid(step=1000) == DEFAULT_BASELINE + 1008


@pytest.mark.asyncio
async def test_next_reqid_allocates_lock_on_first_call() -> None:
    """Lock is created on first invocation and re-used across calls."""
    counter = ReqidCounter()
    assert counter._lock is None
    await counter.next_reqid()
    first_lock = counter._lock
    assert isinstance(first_lock, asyncio.Lock)
    await counter.next_reqid()
    # Same lock instance — never re-allocated on subsequent calls.
    assert counter._lock is first_lock


# ---------------------------------------------------------------------------
# next_reqid — validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_reqid_rejects_zero_step() -> None:
    """``step=0`` would produce duplicate reqids — must raise."""
    counter = ReqidCounter()
    with pytest.raises(ValueError, match="step must be positive"):
        await counter.next_reqid(step=0)
    assert counter.value == DEFAULT_BASELINE


@pytest.mark.asyncio
async def test_next_reqid_rejects_negative_step() -> None:
    """``step<0`` would break monotonicity — must raise."""
    counter = ReqidCounter()
    with pytest.raises(ValueError, match="step must be positive"):
        await counter.next_reqid(step=-5)
    assert counter.value == DEFAULT_BASELINE


@pytest.mark.asyncio
async def test_next_reqid_rejects_bool_step() -> None:
    """``bool`` is a subclass of ``int``; guard must reject ``True`` / ``False``
    explicitly so ``step=True`` cannot silently degrade to ``step=1``.
    """
    counter = ReqidCounter()
    with pytest.raises(TypeError, match="step must be int"):
        await counter.next_reqid(step=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="step must be int"):
        await counter.next_reqid(step=False)  # type: ignore[arg-type]
    assert counter.value == DEFAULT_BASELINE


@pytest.mark.asyncio
async def test_next_reqid_rejects_non_int_step() -> None:
    """Non-``int`` ``step`` (e.g. ``str``) raises ``TypeError`` before any
    state mutation.
    """
    counter = ReqidCounter()
    with pytest.raises(TypeError, match="step must be int"):
        await counter.next_reqid(step="100")  # type: ignore[arg-type]
    assert counter.value == DEFAULT_BASELINE


# ---------------------------------------------------------------------------
# Concurrency — class-in-isolation pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_next_reqid_concurrent_unique_and_monotonic() -> None:
    """50 concurrent ``next_reqid()`` calls produce 50 distinct values that
    sort into a contiguous arithmetic progression.
    """
    counter = ReqidCounter()
    step = 100000
    baseline = counter.value

    results = await asyncio.gather(*[counter.next_reqid(step=step) for _ in range(50)])

    assert len(set(results)) == 50
    assert sorted(results) == [baseline + step * (i + 1) for i in range(50)]
    assert counter.value == baseline + step * 50


# ---------------------------------------------------------------------------
# on_lock_wait callback wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_lock_wait_called_each_acquisition() -> None:
    """The injected ``on_lock_wait`` recorder fires once per ``next_reqid``."""
    samples: list[float] = []

    counter = ReqidCounter(on_lock_wait=samples.append)
    await counter.next_reqid()
    await counter.next_reqid()
    await counter.next_reqid()

    assert len(samples) == 3
    # Wait times are non-negative finite floats (perf_counter delta).
    assert all(isinstance(s, float) and s >= 0.0 for s in samples)


@pytest.mark.asyncio
async def test_default_on_lock_wait_is_noop() -> None:
    """Constructing without ``on_lock_wait`` still works — the default is a
    silent no-op so unit tests don't have to wire up a metrics sink.
    """
    counter = ReqidCounter()
    # Must not raise.
    value = await counter.next_reqid()
    assert value == DEFAULT_BASELINE + DEFAULT_STEP
