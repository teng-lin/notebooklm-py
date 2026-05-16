"""Regression test for the ``max_concurrent_rpcs`` semaphore at
``_perform_authed_post``.

Pre-fix, ``NotebookLMClient`` exposed no ceiling on simultaneous
in-flight RPC POSTs. A FastAPI handler that fanned out a couple
hundred ``client.notebooks.list()`` calls in parallel would push
all of them through ``_perform_authed_post`` together, exceeding
the underlying httpx connection-pool budget and tripping
``httpx.PoolTimeout``. The companion connection-pool tuning raised
the default ``max_connections`` to 100, but a default *upstream*
gate is still needed because (1) connection-pool saturation
surfaces as opaque timeouts rather than clear back-pressure, and
(2) batchexecute itself rate-limits heavy fan-out so an explicit
knob lets callers tune for their account tier.

Post-fix: a per-instance ``asyncio.Semaphore`` is acquired at
the top of ``_perform_authed_post`` and released on every exit path.
Defaults to ``16`` — well below the default ``max_connections=100`` so
there's headroom for short-lived helper requests (refresh GETs, upload
preflights) that aren't gated by the same semaphore.

Architectural decision (locked iter-1):
---------------------------------------

The semaphore is placed at ``_perform_authed_post`` **only**:

- NOT at ``rpc_call`` — the decode-time retry path recursively calls
  ``rpc_call(..., _is_retry=True)`` (``_core.py:1642``). A semaphore
  there would have the outer call hold one permit while waiting for
  the inner call to release one → deadlock under any cap < 2, and
  permit-fragmentation risk under any cap.
- NOT at ``refresh_auth`` — that path uses a raw ``http_client.get(...)``
  on the homepage URL, not the batchexecute POST pipeline. Wrapping it
  would double-gate the refresh-then-retry waterfall and let one slow
  refresh starve in-flight RPCs.

The semaphore is also lazily constructed (``asyncio.Semaphore()`` binds
to the running loop in older Python versions; ``ClientCore`` can be
constructed outside one). Mirrors the lazy-init pattern of
``_reqid_lock`` / ``_auth_snapshot_lock``.

Test scenarios
--------------

1. ``max_concurrent_rpcs=16`` + 100-way fan-out → peak in-flight ≤ 16.
2. ``max_concurrent_rpcs=1`` + 10-way fan-out → fully serialized
   (peak == 1).
3. ``max_concurrent_rpcs=None`` + 50-way fan-out → no cap; peak
   approaches 50 (allowing the usual asyncio-scheduling slack).
4. ``max_concurrent_rpcs > limits.max_connections`` → ``ValueError`` at
   ``NotebookLMClient`` construction (B3 cross-validation guard
   pre-declared in ``ConnectionLimits``'s docstring).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from notebooklm import NotebookLMClient
from notebooklm._core import ClientCore
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod
from notebooklm.types import ConnectionLimits

from .conftest import ConcurrentMockTransport

# concurrency-harness tests against a mock transport; no HTTP,
# no cassette. Opt out of the tier-enforcement hook in
# tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


def _make_auth() -> AuthTokens:
    """Synthetic auth tokens — values don't matter, the mock transport
    ignores them. Mirrors ``test_harness_smoke.py::_make_auth``.
    """
    return AuthTokens(
        csrf_token="CSRF_TEST",
        session_id="SID_TEST",
        cookies={"SID": "test_sid_cookie"},
    )


async def _open_core_with_transport(
    transport: ConcurrentMockTransport,
    *,
    max_concurrent_rpcs: int | None,
) -> ClientCore:
    """Open a ``ClientCore`` with the mock transport swapped in.

    Mirrors ``test_harness_smoke.py::_open_core_with_transport`` plus the
    new ``max_concurrent_rpcs`` knob exercised here. ``ClientCore.open()``
    builds its own ``httpx.AsyncClient``; we close it and replace with
    one routing through the recording transport so the in-flight peak
    is observable.
    """
    core = ClientCore(auth=_make_auth(), max_concurrent_rpcs=max_concurrent_rpcs)
    await core.open()
    assert core._http_client is not None
    prior_cookies = core._http_client.cookies
    await core._http_client.aclose()
    core._http_client = httpx.AsyncClient(
        cookies=prior_cookies,
        transport=transport,
        timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
    )
    return core


async def test_default_16_caps_peak_inflight_at_16_under_100_way_fanout(
    mock_transport_concurrent: ConcurrentMockTransport,
) -> None:
    """``max_concurrent_rpcs=16`` (the production default) + 100-way fan-out.

    Peak in-flight POSTs at the transport must NOT exceed 16. All 100
    requests still complete (the semaphore is a throttle, not a
    rejector). Wall-clock cost: 100 requests / 16 slots × 50ms ≈ 320ms
    serialized through the semaphore, well under the 5s CI budget.
    """
    transport = mock_transport_concurrent
    transport.set_delay(0.05)  # 50ms per request — long enough that
    # batches accumulate at the semaphore checkpoint before any release

    core = await _open_core_with_transport(transport, max_concurrent_rpcs=16)
    try:
        results = await asyncio.gather(
            *[core.rpc_call(RPCMethod.LIST_NOTEBOOKS, []) for _ in range(100)]
        )
    finally:
        await core.close()

    assert len(results) == 100, f"expected 100 results, got {len(results)}"
    assert transport.request_count() == 100, (
        f"transport saw {transport.request_count()} requests, expected 100"
    )

    peak = transport.get_peak_inflight()
    # Hard ceiling: the semaphore must NOT let more than 16 through.
    # A peak above 16 indicates the semaphore isn't held across the
    # ``client.post(...)`` (e.g. released too early, or wrapping the
    # wrong scope).
    assert peak <= 16, (
        f"peak in-flight was {peak}, expected <= 16 under max_concurrent_rpcs=16. "
        f"The semaphore must remain held for the duration of the HTTP POST, not "
        f"just the snapshot/build-request prologue."
    )
    # Soft lower bound: the semaphore must actually be saturating.
    # Anything <= 8 would mean the gate isn't kicking in (the harness
    # default-delay test sees peak >= 80 without a cap), so we'd be
    # asserting a vacuous ceiling. 12 leaves modest slack for asyncio
    # scheduling jitter while staying tight enough to catch a "permit
    # is granted but never released" leak.
    assert peak >= 12, (
        f"peak in-flight was {peak}, expected >= 12 — the semaphore should be "
        f"saturating under 100-way fan-out. A low peak indicates the throttle "
        f"is too aggressive or the harness isn't actually fanning out."
    )
    assert transport.get_inflight_count() == 0


async def test_cap_of_one_fully_serializes_fanout(
    mock_transport_concurrent: ConcurrentMockTransport,
) -> None:
    """``max_concurrent_rpcs=1`` + 10-way fan-out → peak in-flight == 1.

    The strictest possible cap — proves serialization. If the semaphore
    were wrapping the wrong scope (e.g. only the snapshot, not the
    POST), the peak could still be > 1 because two coroutines could
    both pass the snapshot before either entered the transport.
    """
    transport = mock_transport_concurrent
    transport.set_delay(0.02)  # 20ms × 10 serialized ≈ 200ms total

    core = await _open_core_with_transport(transport, max_concurrent_rpcs=1)
    try:
        results = await asyncio.gather(
            *[core.rpc_call(RPCMethod.LIST_NOTEBOOKS, []) for _ in range(10)]
        )
    finally:
        await core.close()

    assert len(results) == 10
    assert transport.request_count() == 10
    peak = transport.get_peak_inflight()
    assert peak == 1, (
        f"peak in-flight was {peak} under max_concurrent_rpcs=1, expected exactly 1. "
        f"A peak > 1 means two coroutines were inside the transport simultaneously, "
        f"which is impossible under a 1-permit semaphore IF the acquire-site spans "
        f"the entire HTTP POST."
    )


async def test_none_disables_cap_and_allows_full_fanout(
    mock_transport_concurrent: ConcurrentMockTransport,
) -> None:
    """``max_concurrent_rpcs=None`` + 50-way fan-out → no cap.

    Peak in-flight should reach near 50 (asyncio scheduling isn't
    perfectly parallel, but >= 40 is a clear "unthrottled" signal).
    The opt-out is for callers who have their own external semaphore
    or who are doing single-shot CLI work where the cap is overhead.
    """
    transport = mock_transport_concurrent
    transport.set_delay(0.05)  # 50ms — long enough to stack 50

    core = await _open_core_with_transport(transport, max_concurrent_rpcs=None)
    try:
        results = await asyncio.gather(
            *[core.rpc_call(RPCMethod.LIST_NOTEBOOKS, []) for _ in range(50)]
        )
    finally:
        await core.close()

    assert len(results) == 50
    assert transport.request_count() == 50
    peak = transport.get_peak_inflight()
    # >= 40 leaves ~20% slack for asyncio scheduling (mirrors the
    # smoke test's 100-way >= 80 framing). A peak near 1 would mean
    # the ``None`` sentinel is being mistakenly converted into a
    # default cap; a peak >= 40 proves the opt-out really opted out.
    assert peak >= 40, (
        f"peak in-flight was {peak} under max_concurrent_rpcs=None; expected >= 40. "
        f"A low peak indicates the ``None`` sentinel is being resolved to a "
        f"non-None default — the contract is ``None`` = unbounded."
    )
    # Upper bound for sanity: a peak above 50 means the in-flight counter
    # is double-incrementing.
    assert peak <= 50


def test_cap_above_pool_max_connections_raises_at_construction(
    auth_tokens: AuthTokens,
) -> None:
    """``max_concurrent_rpcs > limits.max_connections`` → ValueError.

    B3 cross-validation: the semaphore would let requests through that
    the underlying httpx pool can't fulfill, surfacing as opaque
    ``PoolTimeout``s. The constructor catches the misconfiguration
    eagerly. The check is at the ``NotebookLMClient`` boundary because
    ``ClientCore`` synthesizes its own ``ConnectionLimits()`` when
    ``limits=None`` is passed — the client-layer enforcement keeps the
    invariant consistent regardless of how the limits are supplied.
    """
    limits = ConnectionLimits(max_connections=10)
    with pytest.raises(ValueError, match="max_concurrent_rpcs.*max_connections"):
        NotebookLMClient(
            auth_tokens,
            limits=limits,
            max_concurrent_rpcs=11,
        )


def test_cap_equal_to_pool_max_connections_is_allowed(
    auth_tokens: AuthTokens,
) -> None:
    """``max_concurrent_rpcs == limits.max_connections`` is on-boundary OK.

    The constraint is ``<=`` not ``<`` — exactly matching the pool size
    is the canonical "max throughput, no over-subscription" configuration.
    """
    limits = ConnectionLimits(max_connections=10)
    # Construction must succeed; no need to enter the context manager.
    client = NotebookLMClient(
        auth_tokens,
        limits=limits,
        max_concurrent_rpcs=10,
    )
    assert client is not None


def test_default_cap_passes_default_pool_validation(
    auth_tokens: AuthTokens,
) -> None:
    """Default ``max_concurrent_rpcs=16`` < default ``max_connections=100``.

    Sanity check that the production defaults are coherent — a regression
    that swaps either default to a colliding pair would silently fail
    until users hit the cross-validation in production.
    """
    # No explicit limits → ``ConnectionLimits()`` defaults (100/50/30.0).
    client = NotebookLMClient(auth_tokens)
    assert client is not None  # smoke; no ValueError
