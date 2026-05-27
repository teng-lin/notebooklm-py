"""Regression test for the event-loop affinity guard.

Audit item §14 (`thread-safety-concurrency-audit.md` §14):
Pre-fix, ``Session`` carried asyncio primitives (``_reqid_lock``,
``_refresh_lock``, the underlying ``httpx.AsyncClient``'s connection
pool, and any spawned ``asyncio.Task``s) that are silently bound to
whichever event loop was current when they were constructed or first
awaited. A caller who instantiates a client under ``asyncio.run(...)``
in one thread and then hands it to another thread's loop hits opaque
``RuntimeError: ... is bound to a different event loop`` deep inside
httpx, or — worse — a hang on a never-acquired lock that belongs to
a dead loop.

Post-fix: ``Session.open()`` captures
``asyncio.get_running_loop()`` on the lifecycle (read via
``core._lifecycle.get_bound_loop()``) and
``SessionTransport.perform_authed_post`` asserts the running loop matches
via a cheap ``is`` comparison through ``assert_bound_loop``. On mismatch
we raise an actionable ``RuntimeError`` at the call site instead of
letting the failure escalate into the httpx pool or asyncio.Lock internals.

The test exercises the surgical contract:

1. **Cross-loop use raises early** — open the core under one loop, then
   call ``rpc_call`` (which routes through
   ``SessionTransport.perform_authed_post``) under a *different* loop
   and assert the loop-affinity ``RuntimeError`` surfaces with the
   documented message. The error must come from G2's guard, not from a
   downstream httpx symptom.
2. **Same-loop use is unaffected** — open + dispatch under one loop and
   confirm 100 fan-out calls succeed (no false positive on the
   ``is`` comparison).
3. **No binding before open()** — a freshly-constructed ``Session``
   that has never been ``open()``ed has
   ``core._lifecycle.get_bound_loop() is None``; the check inside
   ``SessionTransport.perform_authed_post`` already asserts
   ``self._kernel.http_client is not None``, so an "unopened client"
   caller sees the existing assertion error, not the loop guard.

Why this lives under ``tests/integration/concurrency/`` and not
``tests/unit/``: the regression requires a real ``httpx.AsyncClient``
that has actually been opened, so we reuse the ``ConcurrentMockTransport``
swap-in pattern documented in ``test_harness_smoke.py``. The fix
itself is a one-line ``is`` comparison — but verifying it requires
two distinct event loops, which is integration-shaped.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from _fixtures.kernel_test_helpers import install_http_client_for_test
from notebooklm._session import Session
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod

from .conftest import ConcurrentMockTransport

# affinity-guard tests against a mock transport; no HTTP, no
# cassette. Opt out of the tier-enforcement hook in
# tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


def _make_auth() -> AuthTokens:
    """Synthetic auth tokens — values don't matter, the mock transport
    ignores them. Mirrors ``test_harness_smoke.py::_make_auth`` so a
    regression in either place surfaces consistently.
    """
    return AuthTokens(
        csrf_token="CSRF_TEST",
        session_id="SID_TEST",
        cookies={"SID": "test_sid_cookie"},
    )


async def _open_core_with_transport(transport: ConcurrentMockTransport) -> Session:
    """Open a ``Session`` and swap in the mock transport.

    Mirrors the documented pattern from ``test_harness_smoke.py``:
    ``Session.open()`` builds its own ``httpx.AsyncClient`` and we
    can't override the transport via the constructor. So we open
    normally — which is the moment the loop affinity is captured —
    then close-and-replace the underlying client with one that routes
    through our recording transport. The replacement keeps
    ``self._lifecycle.get_bound_loop()`` unchanged because we don't call
    ``open()`` again.
    """
    core = Session(auth=_make_auth())
    await core.open()
    assert core._kernel.http_client is not None
    prior_cookies = core._kernel.get_http_client().cookies
    await core._kernel.get_http_client().aclose()
    install_http_client_for_test(
        core._kernel,
        httpx.AsyncClient(
            cookies=prior_cookies,
            transport=transport,
            timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
        ),
    )
    return core


def test_cross_loop_use_raises_actionable_runtime_error(
    mock_transport_concurrent: ConcurrentMockTransport,
) -> None:
    """Open the core under loop A, dispatch under loop B → ``RuntimeError``.

    Two independent ``asyncio.run`` invocations give us two genuinely
    distinct event loops in the same thread (each ``asyncio.run`` builds
    a fresh loop, runs to completion, then closes it). The ``is``
    comparison in ``_perform_authed_post`` is what we care about — these
    two loops are not the same object, so the guard must fire.

    Note: this test is intentionally *not* ``async def``. We need to own
    the two ``asyncio.run`` calls explicitly so they construct distinct
    loops. An ``async def`` test would run inside a single pytest-asyncio
    loop and we'd have to spin up a second loop manually, which is
    exactly what ``asyncio.run`` does for us.
    """
    transport = mock_transport_concurrent
    # No artificial delay — the guard fires before any wire request would
    # be issued, so the per-request stacking the smoke test relies on
    # doesn't matter here.
    transport.set_delay(0.0)

    # Loop A: construct + open the core. The core's ``_bound_loop`` is
    # bound to this loop. We deliberately don't ``close()`` here because
    # ``close()`` is also async and would need yet another loop — we
    # rely on the test's terminal ``asyncio.run`` for the second-loop
    # close.
    core: Session = asyncio.run(_open_core_with_transport(transport))

    # Loop A is now closed; loop B is the fresh loop ``asyncio.run``
    # below will construct. Both ``open`` and ``call_under_loop_b`` must
    # see two distinct loop objects via ``is``.
    async def call_under_loop_b() -> None:
        # The guard fires inside ``_perform_authed_post``. ``rpc_call``
        # wraps transport errors into ``RPCError``-family exceptions —
        # but our ``RuntimeError`` is not a transport error, so it
        # propagates unchanged.
        with pytest.raises(RuntimeError, match="bound to a different event loop"):
            await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        # Confirm the actionable second sentence is in the message so
        # users know what to do — not just that *something* went wrong.
        try:
            await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
        except RuntimeError as exc:
            assert "create a new client in the target loop" in str(exc), (
                f"loop-affinity RuntimeError should tell users how to fix it; got message: {exc!s}"
            )
        else:  # pragma: no cover — defensive
            raise AssertionError("expected RuntimeError on cross-loop reuse")

        # No wire requests were issued — the guard fired before any
        # ``client.post(...)`` could run.
        assert transport.request_count() == 0, (
            f"expected guard to fire before any wire request; "
            f"transport saw {transport.request_count()} request(s)"
        )

        # Tear the (now-orphaned) httpx client down on *this* loop so we
        # don't leak the transport. We deliberately go around
        # ``core.close()`` because that path also touches asyncio
        # primitives bound to loop A.
        if core._kernel.http_client is not None:
            await core._kernel.get_http_client().aclose()
            install_http_client_for_test(core._kernel, None)

    asyncio.run(call_under_loop_b())


async def test_same_loop_use_unaffected(
    mock_transport_concurrent: ConcurrentMockTransport,
) -> None:
    """100-way fan-out on the *same* loop must complete without the guard firing.

    Mirrors ``test_harness_smoke.py``'s 100-way gather to confirm the
    cheap ``is`` comparison does not produce false positives under
    realistic dispatch shapes. If the guard fired here, the ``rpc_call``
    would surface ``RuntimeError`` instead of returning ``[]``.
    """
    transport = mock_transport_concurrent
    transport.set_delay(0.0)  # speed only — peak-inflight isn't asserted here

    core = await _open_core_with_transport(transport)
    try:
        results = await asyncio.gather(
            *[core.rpc_call(RPCMethod.LIST_NOTEBOOKS, []) for _ in range(100)]
        )
    finally:
        await core.close()

    assert len(results) == 100
    assert all(r == [] for r in results)
    assert transport.request_count() == 100


async def test_bound_loop_captured_on_open(
    mock_transport_concurrent: ConcurrentMockTransport,
) -> None:
    """Sanity check: ``_bound_loop`` is ``None`` pre-open, set to the running loop post-open.

    Pins the contract that ``open()`` is the binding moment. A future
    refactor that moves the capture to ``__init__`` (which can be called
    outside a running loop) would break the audit-§14 fix because the
    construction-time loop may not be the dispatch-time loop.
    """
    core = Session(auth=_make_auth())
    assert core._lifecycle.get_bound_loop() is None, (
        "Session must not bind to a loop at construction time — open() is the binding moment."
    )

    await core.open()
    try:
        assert core._lifecycle.get_bound_loop() is asyncio.get_running_loop(), (
            "open() must capture the *running* loop, not a stored or module-level reference."
        )

        # Swap in the mock transport so close() doesn't make real HTTP
        # requests for cookie persistence (auth has no storage_path so
        # save_cookies is already a no-op, but route everything through
        # the recorder to keep the test deterministic).
        assert core._kernel.http_client is not None
        prior_cookies = core._kernel.get_http_client().cookies
        await core._kernel.get_http_client().aclose()
        install_http_client_for_test(
            core._kernel,
            httpx.AsyncClient(
                cookies=prior_cookies,
                transport=mock_transport_concurrent,
                timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
            ),
        )
    finally:
        await core.close()
