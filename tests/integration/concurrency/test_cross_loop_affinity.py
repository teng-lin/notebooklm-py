"""Regression test for the event-loop affinity guard.

Audit item §14 (`thread-safety-concurrency-audit.md` §14):
Pre-fix, ``NotebookLMClient`` carried asyncio primitives (``_reqid_lock``,
``_refresh_lock``, the underlying ``httpx.AsyncClient``'s connection
pool, and any spawned ``asyncio.Task``s) that are silently bound to
whichever event loop was current when they were constructed or first
awaited. A caller who instantiates a client under ``asyncio.run(...)``
in one thread and then hands it to another thread's loop hits opaque
``RuntimeError: ... is bound to a different event loop`` deep inside
httpx, or — worse — a hang on a never-acquired lock that belongs to
a dead loop.

Post-fix: ``NotebookLMClient.open()`` captures
``asyncio.get_running_loop()`` on the lifecycle (read via
``core._collaborators.lifecycle.get_bound_loop()``) and
``RuntimeTransport.perform_authed_post`` asserts the running loop matches
via a cheap ``is`` comparison through ``assert_bound_loop``. On mismatch
we raise an actionable ``RuntimeError`` at the call site instead of
letting the failure escalate into the httpx pool or asyncio.Lock internals.

The test exercises the surgical contract:

1. **Cross-loop use raises early** — open the core under one loop, then
   call ``rpc_call`` (which routes through
   ``RuntimeTransport.perform_authed_post``) under a *different* loop
   and assert the loop-affinity ``RuntimeError`` surfaces with the
   documented message. The error must come from G2's guard, not from a
   downstream httpx symptom.
2. **Same-loop use is unaffected** — open + dispatch under one loop and
   confirm 100 fan-out calls succeed (no false positive on the
   ``is`` comparison).
3. **No binding before open()** — a freshly-constructed ``NotebookLMClient``
   that has never been ``open()``ed has
   ``core._collaborators.lifecycle.get_bound_loop() is None``; the check inside
   ``RuntimeTransport.perform_authed_post`` already asserts
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

from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.rpc import RPCMethod
from tests._fixtures.kernel_test_helpers import install_http_client_for_test
from tests._helpers.client_factory import build_client_shell_for_tests

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


async def _open_core_with_transport(transport: ConcurrentMockTransport) -> NotebookLMClient:
    """Open a ``NotebookLMClient`` and swap in the mock transport.

    Mirrors the documented pattern from ``test_harness_smoke.py``:
    ``NotebookLMClient.open()`` builds its own ``httpx.AsyncClient`` and we
    can't override the transport via the constructor. So we open
    normally — which is the moment the loop affinity is captured —
    then close-and-replace the underlying client with one that routes
    through our recording transport. The replacement keeps
    ``self._lifecycle.get_bound_loop()`` unchanged because we don't call
    ``open()`` again.
    """
    core = build_client_shell_for_tests(auth=_make_auth())
    await core.__aenter__()
    assert core._collaborators.kernel.http_client is not None
    prior_cookies = core._collaborators.kernel.get_http_client().cookies
    await core._collaborators.kernel.get_http_client().aclose()
    install_http_client_for_test(
        core._collaborators.kernel,
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
    core: NotebookLMClient = asyncio.run(_open_core_with_transport(transport))

    # Loop A is now closed; loop B is the fresh loop ``asyncio.run``
    # below will construct. Both ``open`` and ``call_under_loop_b`` must
    # see two distinct loop objects via ``is``.
    async def call_under_loop_b() -> None:
        # The guard fires inside ``_perform_authed_post``. ``rpc_call``
        # wraps transport errors into ``RPCError``-family exceptions —
        # but our ``RuntimeError`` is not a transport error, so it
        # propagates unchanged.
        with pytest.raises(RuntimeError, match="bound to a different event loop"):
            await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

        # Confirm the actionable second sentence is in the message so
        # users know what to do — not just that *something* went wrong.
        try:
            await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
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
        if core._collaborators.kernel.http_client is not None:
            await core._collaborators.kernel.get_http_client().aclose()
            install_http_client_for_test(core._collaborators.kernel, None)

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
            *[core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, []) for _ in range(100)]
        )
    finally:
        await core.close()

    assert len(results) == 100
    assert all(r == [] for r in results)
    assert transport.request_count() == 100


def test_capped_client_reopen_on_new_loop_rebinds_semaphore(
    mock_transport_concurrent: ConcurrentMockTransport,
) -> None:
    """Issue #1169: close on loop A, reopen on loop B → RPC semaphore rebinds.

    Pre-fix the RPC concurrency semaphore was the only loop-bound primitive
    without a close→reopen reset, so a capped client reopened on a different
    loop reused a stale ``asyncio.Semaphore`` bound to the dead loop — which
    on Python 3.10/3.11 raised "bound to a different event loop" or misparked
    waiters when the slot was acquired. Post-fix ``ClientLifecycle.open``
    calls ``ClientComposed.reset_after_open`` so the semaphore is rebuilt on
    the new loop and a fan-out still completes (and is still gated by the cap).

    Like the cross-loop test above, this is intentionally NOT ``async def``:
    we own two ``asyncio.run`` calls explicitly so the open and the reopen
    happen on two genuinely distinct loop objects.
    """
    transport = mock_transport_concurrent
    transport.set_delay(0.01)

    # Build a capped client once; reuse the instance across two loops.
    core = build_client_shell_for_tests(auth=_make_auth(), max_concurrent_rpcs=2)

    async def _open_swap_and_close_under_loop_a() -> None:
        await core.__aenter__()
        prior_cookies = core._collaborators.kernel.get_http_client().cookies
        await core._collaborators.kernel.get_http_client().aclose()
        install_http_client_for_test(
            core._collaborators.kernel,
            httpx.AsyncClient(
                cookies=prior_cookies,
                transport=transport,
                timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
            ),
        )
        # One dispatch on loop A so the semaphore is actually constructed and
        # bound to loop A — that is the stale primitive a naive reopen reuses.
        await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
        await core.close()

    asyncio.run(_open_swap_and_close_under_loop_a())
    # The reset happens on open(), not close(): the stale semaphore is still
    # cached here, bound to the now-dead loop A.
    assert core._composed._rpc_semaphore is not None

    async def _reopen_and_dispatch_under_loop_b() -> None:
        await core.__aenter__()
        # reset_after_open() must have discarded the loop-A semaphore so the
        # next get_rpc_semaphore() rebuilds it on loop B.
        assert core._composed._rpc_semaphore is None
        prior_cookies = core._collaborators.kernel.get_http_client().cookies
        await core._collaborators.kernel.get_http_client().aclose()
        install_http_client_for_test(
            core._collaborators.kernel,
            httpx.AsyncClient(
                cookies=prior_cookies,
                transport=transport,
                timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
            ),
        )
        try:
            # Fan-out on loop B. Pre-fix this would surface the cross-loop
            # RuntimeError (3.10/3.11) when acquiring the stale slot; post-fix
            # the semaphore is fresh and bound to loop B, so all calls succeed.
            results = await asyncio.gather(
                *[core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, []) for _ in range(8)]
            )
        finally:
            await core.close()
        assert len(results) == 8
        assert all(r == [] for r in results)
        # The cap was still honoured on the rebound semaphore.
        assert transport.get_peak_inflight() <= 2

    asyncio.run(_reopen_and_dispatch_under_loop_b())


def test_upload_pipeline_reopen_on_new_loop_rebinds_semaphore(
    mock_transport_concurrent: ConcurrentMockTransport,
) -> None:
    """Issue #1196 (upload variant): close on loop A, reopen on loop B → upload semaphore rebinds.

    The Sources upload semaphore (``SourceUploadPipeline._upload_semaphore``)
    is the second lazily-built loop-bound ``asyncio.Semaphore`` in the client,
    with the exact close→reopen hazard #1196 fixed for the RPC semaphore: it is
    bound to whichever loop it was first constructed under, but
    ``ClientLifecycle.open`` did not reset it, so a client reopened on a
    *different* loop reused a semaphore bound to the now-dead loop — which on
    Python 3.10/3.11 raises "bound to a different event loop" or misparks
    waiters on acquire.

    Post-fix ``ClientLifecycle.open`` calls
    ``SourceUploadPipeline.set_bound_loop`` + ``reset_after_open`` (mirroring
    the RPC-semaphore reset above), so the upload semaphore is discarded on a
    loop change and rebuilt fresh on the new loop.

    Like the RPC-semaphore test above, this is intentionally NOT ``async def``:
    we own two ``asyncio.run`` calls explicitly so the open and the reopen
    happen on two genuinely distinct loop objects.

    The semaphore is capped at 1 and each loop forces a *blocked* second
    acquire: ``asyncio.Semaphore.acquire`` only consults ``_get_loop()`` (and
    thus binds the primitive to a loop) on the contended waiter path — the
    uncontended fast path returns before touching the loop. Mirroring the
    cap-2 fan-out the RPC test above uses, the contention here makes the
    cross-loop binding actually exercise the stale-loop hazard pre-fix.
    """
    transport = mock_transport_concurrent
    transport.set_delay(0.0)

    core = build_client_shell_for_tests(auth=_make_auth(), max_concurrent_uploads=1)
    uploader = core._source_uploader

    async def _force_contended_acquire(sem: asyncio.Semaphore) -> None:
        """Drive the blocked-waiter path so ``sem`` binds to the running loop.

        Hold the single slot, start a second ``acquire`` that must block
        (creating a waiter future via ``_get_loop()`` — the loop-binding
        step), then release so the waiter proceeds. On a stale cross-loop
        semaphore this is where 3.10/3.11 raise "bound to a different event
        loop".
        """
        await sem.acquire()
        waiter = asyncio.ensure_future(sem.acquire())
        # Yield so the waiter runs far enough to park on the locked semaphore.
        await asyncio.sleep(0)
        assert not waiter.done()
        sem.release()
        await waiter
        sem.release()

    async def _open_force_semaphore_and_close_under_loop_a() -> None:
        await core.__aenter__()
        prior_cookies = core._collaborators.kernel.get_http_client().cookies
        await core._collaborators.kernel.get_http_client().aclose()
        install_http_client_for_test(
            core._collaborators.kernel,
            httpx.AsyncClient(
                cookies=prior_cookies,
                transport=transport,
                timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
            ),
        )
        # Force the upload semaphore to actually be constructed and bound to
        # loop A — that is the stale primitive a naive reopen reuses. The
        # contended acquire binds it to loop A via the waiter path.
        await _force_contended_acquire(uploader.get_upload_semaphore())
        await core.close()

    asyncio.run(_open_force_semaphore_and_close_under_loop_a())
    # The reset happens on open(), not close(): the stale semaphore is still
    # cached here, bound to the now-dead loop A.
    assert uploader._upload_semaphore is not None

    async def _reopen_and_use_semaphore_under_loop_b() -> None:
        await core.__aenter__()
        # reset_after_open() must have discarded the loop-A semaphore so the
        # next get_upload_semaphore() rebuilds it on loop B.
        assert uploader._upload_semaphore is None
        prior_cookies = core._collaborators.kernel.get_http_client().cookies
        await core._collaborators.kernel.get_http_client().aclose()
        install_http_client_for_test(
            core._collaborators.kernel,
            httpx.AsyncClient(
                cookies=prior_cookies,
                transport=transport,
                timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
            ),
        )
        try:
            # Drive a contended acquire on the rebuilt semaphore under loop B.
            # Pre-fix this would reuse the stale loop-A semaphore and (on
            # 3.10/3.11) raise the cross-loop RuntimeError on the waiter path;
            # post-fix the semaphore is fresh and binds cleanly to loop B.
            sem_b = uploader.get_upload_semaphore()
            assert sem_b is not None
            await _force_contended_acquire(sem_b)
        finally:
            await core.close()

    asyncio.run(_reopen_and_use_semaphore_under_loop_b())


def test_chat_locks_reopen_on_new_loop_rebind(
    mock_transport_concurrent: ConcurrentMockTransport,
) -> None:
    """Issue #1225: close on loop A, reopen on loop B → ChatAPI conversation locks rebind.

    The ``ChatAPI`` per-conversation / per-notebook locks
    (``ChatAPI._conversation_locks`` / ``_new_conversation_locks``, two
    ``WeakValueDictionary`` maps of lazily-built ``asyncio.Lock``) were the
    last lazy loop-bound primitives in the client without the owner-level
    ``set_bound_loop`` + ``reset_after_open`` protocol. Each lock binds to
    whichever loop is running the first time it is awaited; a client closed on
    loop A and reopened on loop B that reused a lock bound to the now-dead loop
    A raises "bound to a different event loop" (Python 3.10/3.11) or misparks
    waiters on the contended-acquire path.

    Post-fix ``ClientLifecycle.open`` calls ``ChatAPI.set_bound_loop`` +
    ``reset_after_open`` (mirroring the RPC- and upload-semaphore resets), so
    the lock maps are cleared on a loop change and each per-key lock is rebuilt
    fresh on the new loop.

    Like the semaphore tests above, this is intentionally NOT ``async def``: we
    own two ``asyncio.run`` calls explicitly so the open and the reopen happen
    on two genuinely distinct loop objects.

    A bare ``asyncio.Lock`` only consults ``_get_loop()`` (and thus binds to a
    loop) on the contended waiter path — the uncontended fast path returns
    before touching the loop. So we drive a *blocked* second acquire on each
    loop to force the binding that exercises the stale-loop hazard pre-fix.
    """
    transport = mock_transport_concurrent
    transport.set_delay(0.0)

    core = build_client_shell_for_tests(auth=_make_auth())
    chat = core.chat
    conv_id = "conv-1225"

    async def _force_contended_acquire(lock: asyncio.Lock) -> None:
        """Drive the blocked-waiter path so ``lock`` binds to the running loop.

        Hold the lock, start a second ``acquire`` that must block (creating a
        waiter future via ``_get_loop()`` — the loop-binding step), then
        release so the waiter proceeds. On a stale cross-loop lock this is
        where 3.10/3.11 raise "bound to a different event loop".
        """
        await lock.acquire()
        waiter = asyncio.ensure_future(lock.acquire())
        # Yield so the waiter runs far enough to park on the held lock.
        await asyncio.sleep(0)
        assert not waiter.done()
        lock.release()
        await waiter
        lock.release()

    async def _open_force_lock_and_close_under_loop_a() -> asyncio.Lock:
        await core.__aenter__()
        prior_cookies = core._collaborators.kernel.get_http_client().cookies
        await core._collaborators.kernel.get_http_client().aclose()
        install_http_client_for_test(
            core._collaborators.kernel,
            httpx.AsyncClient(
                cookies=prior_cookies,
                transport=transport,
                timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
            ),
        )
        # Force a conversation lock to actually be constructed and bound to
        # loop A — that is the stale primitive a naive reopen reuses. The lock
        # is RETURNED so the caller keeps a strong reference; without it the
        # WeakValueDictionary entry would GC the moment this coroutine's locals
        # are dropped, masking whether the reset (vs. plain GC) cleared the map.
        lock = chat._get_conversation_lock(conv_id)
        await _force_contended_acquire(lock)
        assert conv_id in chat._conversation_locks
        await core.close()
        return lock

    # Keep a strong reference across the loop boundary so the WeakValueDictionary
    # entry bound to loop A is still present when we assert the reset cleared it.
    pinned_lock = asyncio.run(_open_force_lock_and_close_under_loop_a())
    # The reset happens on open(), not close(): the stale loop-A lock is still
    # cached here (pinned by ``pinned_lock``), bound to the now-dead loop A.
    assert pinned_lock is not None
    assert chat._conversation_locks.get(conv_id) is pinned_lock

    async def _reopen_and_use_lock_under_loop_b() -> None:
        await core.__aenter__()
        # reset_after_open() must have cleared the loop-A lock map so the next
        # _get_conversation_lock() rebuilds a fresh lock on loop B (even though
        # ``pinned_lock`` still holds a strong ref to the old one).
        assert chat._conversation_locks.get(conv_id) is None
        prior_cookies = core._collaborators.kernel.get_http_client().cookies
        await core._collaborators.kernel.get_http_client().aclose()
        install_http_client_for_test(
            core._collaborators.kernel,
            httpx.AsyncClient(
                cookies=prior_cookies,
                transport=transport,
                timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
            ),
        )
        try:
            # Drive a contended acquire on the rebuilt lock under loop B.
            # Pre-fix this would reuse the stale loop-A lock and (on 3.10/3.11)
            # raise the cross-loop RuntimeError on the waiter path; post-fix the
            # lock is fresh and binds cleanly to loop B.
            lock_b = chat._get_conversation_lock(conv_id)
            assert lock_b is not pinned_lock
            await _force_contended_acquire(lock_b)
        finally:
            await core.close()

    asyncio.run(_reopen_and_use_lock_under_loop_b())


async def test_bound_loop_captured_on_open(
    mock_transport_concurrent: ConcurrentMockTransport,
) -> None:
    """Sanity check: ``_bound_loop`` is ``None`` pre-open, set to the running loop post-open.

    Pins the contract that ``open()`` is the binding moment. A future
    refactor that moves the capture to ``__init__`` (which can be called
    outside a running loop) would break the audit-§14 fix because the
    construction-time loop may not be the dispatch-time loop.
    """
    core = build_client_shell_for_tests(auth=_make_auth())
    assert core._collaborators.lifecycle.get_bound_loop() is None, (
        "NotebookLMClient must not bind to a loop at construction time — open() is the binding moment."
    )

    await core.__aenter__()
    try:
        assert core._collaborators.lifecycle.get_bound_loop() is asyncio.get_running_loop(), (
            "open() must capture the *running* loop, not a stored or module-level reference."
        )

        # Swap in the mock transport so close() doesn't make real HTTP
        # requests for cookie persistence (auth has no storage_path so
        # save_cookies is already a no-op, but route everything through
        # the recorder to keep the test deterministic).
        assert core._collaborators.kernel.http_client is not None
        prior_cookies = core._collaborators.kernel.get_http_client().cookies
        await core._collaborators.kernel.get_http_client().aclose()
        install_http_client_for_test(
            core._collaborators.kernel,
            httpx.AsyncClient(
                cookies=prior_cookies,
                transport=mock_transport_concurrent,
                timeout=httpx.Timeout(connect=1.0, read=5.0, write=5.0, pool=1.0),
            ),
        )
    finally:
        await core.close()
