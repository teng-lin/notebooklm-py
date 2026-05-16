"""Tests for the per-loop / per-resolved-storage-path refresh lock registry.

The module-global ``_REFRESH_LOCK = asyncio.Lock()`` was removed because
``asyncio.Lock`` binds to the first event loop that uses it, breaking on
cross-loop / cross-thread usage. The replacement mirrors the keepalive
``_get_poke_lock`` pattern: a ``WeakKeyDictionary`` keyed on the running
loop, with per-resolved-storage-path inner locks. Cross-loop atomicity
for ``_REFRESH_GENERATIONS`` is provided by the sync ``_REFRESH_STATE_LOCK``.
"""

from __future__ import annotations

import asyncio
import gc
import threading
from pathlib import Path

import pytest

from notebooklm import auth as auth_mod


@pytest.fixture(autouse=True)
def _clear_refresh_state():
    """Reset module state between tests."""
    auth_mod._REFRESH_GENERATIONS.clear()
    # The WeakKeyDictionary is mutated indirectly; we don't reset it here so
    # the cleanup test can observe natural GC behavior.
    yield
    auth_mod._REFRESH_GENERATIONS.clear()


class TestPerLoopLockIdentity:
    def test_different_loops_get_different_locks(self):
        """A lock created in loop X must NOT be returned to loop Y.

        Both loops must be kept alive simultaneously so the WeakKeyDictionary
        entries survive long enough to compare the locks by identity. Using
        ``asyncio.run`` for each loop in turn allows Python 3.13's GC to
        reclaim the first loop's entry before the second is created, after
        which ``id()`` of the second lock can collide with the recycled
        memory address of the first.
        """
        path = Path("/tmp/notebooklm-test/storage_state.json")

        loop_a = asyncio.new_event_loop()
        loop_b = asyncio.new_event_loop()
        try:

            async def _grab():
                return auth_mod._get_refresh_lock(path)

            lock_a = loop_a.run_until_complete(_grab())
            lock_b = loop_b.run_until_complete(_grab())
            # Hold strong references to both locks AND both loops until
            # after the assertion so neither can be GC'd / address-recycled.
            assert lock_a is not lock_b, (
                "Different event loops must produce distinct asyncio.Lock instances"
            )
        finally:
            loop_a.close()
            loop_b.close()

    def test_same_loop_same_path_returns_same_lock(self):
        """Repeated calls within one loop for the same path return the same lock."""
        path = Path("/tmp/notebooklm-test/storage_state.json")

        async def capture_two_locks():
            return id(auth_mod._get_refresh_lock(path)), id(auth_mod._get_refresh_lock(path))

        a, b = asyncio.run(capture_two_locks())
        assert a == b

    def test_same_loop_different_paths_get_different_locks(self):
        """Distinct storage paths within one loop get distinct locks."""
        path_a = Path("/tmp/notebooklm-test/profile_a/storage_state.json")
        path_b = Path("/tmp/notebooklm-test/profile_b/storage_state.json")

        async def capture():
            return (
                id(auth_mod._get_refresh_lock(path_a)),
                id(auth_mod._get_refresh_lock(path_b)),
            )

        a, b = asyncio.run(capture())
        assert a != b


class TestResolvedPathEquivalence:
    def test_none_with_profile_and_explicit_path_share_lock(self, monkeypatch, tmp_path):
        """``(None, profile="foo")`` and the explicit path resolve to the same key.

        ``_fetch_tokens_with_refresh`` computes
        ``refresh_storage_path = storage_path or get_storage_path(profile=profile)``
        and keys the lock on that resolved Path, so two callers using either
        form share the same lock.
        """
        resolved = tmp_path / "profile_foo" / "storage_state.json"

        async def capture():
            # Both calls pass the SAME resolved Path object the production code
            # would compute. The registry keys on Path equality, not identity.
            lock_via_none = auth_mod._get_refresh_lock(resolved)
            # Build an equivalent Path object (different instance, same value).
            equivalent = Path(str(resolved))
            lock_via_explicit = auth_mod._get_refresh_lock(equivalent)
            return id(lock_via_none), id(lock_via_explicit)

        a, b = asyncio.run(capture())
        assert a == b, "Equal Path values must hash to the same registry slot"

    def test_symlink_and_real_path_share_lock_via_refresh(self, monkeypatch, tmp_path):
        """Two different surface representations of the same physical file
        (symlink vs canonical absolute path, and relative vs absolute) must
        flow through ``_fetch_tokens_with_refresh``'s path canonicalization
        and end up sharing a single lock / generation-key.

        The production fix is ``.expanduser().resolve()`` applied to
        ``refresh_storage_path`` before it is used as a key. Without that
        normalization, two callers referring to the same on-disk file by
        different paths would each get their own lock — defeating the
        cross-loop / cross-thread refresh coalescing.
        """
        # Set up a real file plus a symlink pointing at it.
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        real_path = real_dir / "storage_state.json"
        real_path.write_text('{"cookies": [], "origins": []}')

        link_dir = tmp_path / "via_link"
        link_dir.symlink_to(real_dir, target_is_directory=True)
        symlinked_path = link_dir / "storage_state.json"

        # Sanity: surface paths differ, resolved targets match.
        assert symlinked_path != real_path
        assert symlinked_path.resolve() == real_path.resolve()

        captured_keys: list[str] = []
        original_get_refresh_lock = auth_mod._get_refresh_lock

        def spy_get_refresh_lock(p):
            captured_keys.append(str(p))
            return original_get_refresh_lock(p)

        # Force a refresh attempt and short-circuit downstream side effects.
        monkeypatch.setenv(auth_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "dummy")
        monkeypatch.setattr(auth_mod, "_get_refresh_lock", spy_get_refresh_lock)

        call_phase = {"first": True}

        async def fake_fetch_tokens_with_jar(jar, path, **kwargs):
            if call_phase["first"]:
                call_phase["first"] = False
                raise ValueError("Authentication expired. Run 'notebooklm login'.")
            return "csrf-token", "session-id"

        async def fake_run_refresh_cmd(storage_path, profile):
            return None

        import httpx

        def fake_build(_p):
            return httpx.Cookies()

        def fake_snapshot(_j):
            return None

        monkeypatch.setattr(auth_mod, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
        monkeypatch.setattr(auth_mod, "_run_refresh_cmd", fake_run_refresh_cmd)
        monkeypatch.setattr(auth_mod, "build_httpx_cookies_from_storage", fake_build)
        monkeypatch.setattr(auth_mod, "snapshot_cookie_jar", fake_snapshot)

        async def drive(path: Path):
            jar = httpx.Cookies()
            return await auth_mod._fetch_tokens_with_refresh(jar, storage_path=path)

        # First caller uses the symlinked path.
        asyncio.run(drive(symlinked_path))
        # Reset phase so the second caller also triggers the refresh branch.
        call_phase["first"] = True
        # Second caller uses the canonical real path.
        asyncio.run(drive(real_path))

        # Both calls must have canonicalized to the same key.
        assert len(captured_keys) == 2
        assert captured_keys[0] == captured_keys[1], (
            f"Symlinked and direct paths produced distinct keys: {captured_keys!r}"
        )
        # And the canonical key must equal the resolved real path.
        assert captured_keys[0] == str(real_path.resolve())


class TestWeakKeyDictionaryCleanup:
    def test_loops_are_garbage_collected(self):
        """When loops go out of scope, their inner dict is reclaimed."""
        path = Path("/tmp/notebooklm-test/storage_state.json")

        def spawn_and_drop():
            """Run a short-lived loop, return; do not retain a reference."""

            async def inner():
                auth_mod._get_refresh_lock(path)

            asyncio.run(inner())

        baseline_len = len(auth_mod._REFRESH_LOCKS_BY_LOOP)
        for _ in range(5):
            spawn_and_drop()

        # Force collection of the closed loops.
        gc.collect()

        post_len = len(auth_mod._REFRESH_LOCKS_BY_LOOP)
        # The WeakKeyDictionary should reclaim entries for collected loops.
        # We assert the registry didn't grow unboundedly across iterations.
        assert post_len <= baseline_len + 1, (
            f"WeakKeyDictionary did not reclaim closed-loop entries "
            f"(baseline={baseline_len}, post={post_len})"
        )


class TestCrossLoopGenerationGuard:
    def test_two_loops_at_most_two_refreshes(self, monkeypatch, tmp_path):
        """Two concurrent event loops both call ``_fetch_tokens_with_refresh``;
        AT MOST 2 subprocess invocations occur (each loop runs once at the
        worst), and crucially BOTH calls succeed without raising.

        Race model: both loops observe an auth-expiry failure, each
        capture the same pre-refresh generation, each acquire their OWN
        per-loop asyncio lock (the registry hands out distinct locks per
        loop), then race the check-and-claim under ``_REFRESH_STATE_LOCK``.

        Legacy contract (before the gated-generation fix): this test
        asserted ``run_count == 1`` because the old code bumped
        ``_REFRESH_GENERATIONS`` EAGERLY pre-subprocess; the cross-loop
        loser saw the bump and skipped. That eager-bump behavior was the
        root cause of the phantom-bump failure — when the subprocess
        failed, the bump fooled concurrent waiters into skipping with
        stale storage.

        Current contract (gated-generation fix): generation is bumped
        ONLY after the subprocess succeeds. Cross-loop callers cannot
        signal "in flight" to each other (``asyncio.Future`` is
        loop-bound). In the rare
        cross-loop-concurrent-refresh case both loops may run their own
        subprocess — equivalent to two ``RotateCookies`` POSTs against
        the same storage. The end-state is correct (fresh cookies on
        disk; last writer wins, but both write the same fresh data).

        For SAME-LOOP coalescing (the dominant real-world case), the
        per-loop in-flight future ensures exactly-once subprocess
        execution; see ``tests/integration/concurrency/test_refresh_cmd_race.py``.

        To force a deterministic race in a unit test, we use a barrier at
        the point AFTER both threads have captured the generation but
        BEFORE either has entered the inner sync mutex.
        """
        import httpx

        storage = tmp_path / "storage_state.json"
        storage.write_text('{"cookies": [], "origins": []}')

        # Force ``_should_try_refresh`` to return True.
        monkeypatch.setenv(auth_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "dummy")

        run_count = 0
        run_count_lock = threading.Lock()

        # Barrier #1: align both fakes at the failure point so neither
        # captures the generation before the other has had a chance to
        # observe gen 0.
        fail_barrier = threading.Barrier(2, timeout=5)
        # Barrier #2: align both threads INSIDE their per-loop asyncio lock
        # but BEFORE the inner sync-mutex check-and-claim. This guarantees
        # both have already captured gen=0 pre-lock.
        post_lock_barrier = threading.Barrier(2, timeout=5)

        async def fake_run_refresh_cmd(storage_path, profile):
            nonlocal run_count
            with run_count_lock:
                run_count += 1
            await asyncio.sleep(0.01)

        async def fake_fetch_tokens_with_jar(cookie_jar, storage_path, **kwargs):
            if not getattr(cookie_jar, "_refresh_done", False):
                cookie_jar._refresh_done = True
                try:
                    fail_barrier.wait()
                except threading.BrokenBarrierError:
                    pass
                raise ValueError("Authentication expired. Run 'notebooklm login'.")
            return "csrf-token", "session-id"

        def fake_build_httpx_cookies(path):
            return httpx.Cookies()

        def fake_snapshot(jar):
            return None

        # Wrap ``_get_refresh_lock`` so we can interpose a barrier between
        # the (pre-lock) generation capture and the (post-lock) inner
        # sync-mutex check-and-claim.
        original_get_refresh_lock = auth_mod._get_refresh_lock

        class _BarrierLock:
            def __init__(self, inner):
                self._inner = inner

            async def __aenter__(self):
                await self._inner.acquire()
                # Yield to a thread so the OTHER loop can also acquire its
                # (distinct) per-loop lock before either touches the
                # generation dict.
                await asyncio.to_thread(post_lock_barrier.wait)
                return self

            async def __aexit__(self, exc_type, exc, tb):
                self._inner.release()
                return False

        def wrapped_get_refresh_lock(p):
            return _BarrierLock(original_get_refresh_lock(p))

        monkeypatch.setattr(auth_mod, "_run_refresh_cmd", fake_run_refresh_cmd)
        monkeypatch.setattr(auth_mod, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
        monkeypatch.setattr(auth_mod, "build_httpx_cookies_from_storage", fake_build_httpx_cookies)
        monkeypatch.setattr(auth_mod, "snapshot_cookie_jar", fake_snapshot)
        monkeypatch.setattr(auth_mod, "_get_refresh_lock", wrapped_get_refresh_lock)

        results: list[BaseException | tuple] = []
        results_lock = threading.Lock()

        def run_in_own_loop():
            async def _work():
                jar = httpx.Cookies()
                return await auth_mod._fetch_tokens_with_refresh(jar, storage_path=storage)

            try:
                res = asyncio.run(_work())
                with results_lock:
                    results.append(res)
            except BaseException as exc:  # noqa: BLE001
                with results_lock:
                    results.append(exc)

        thread_a = threading.Thread(target=run_in_own_loop)
        thread_b = threading.Thread(target=run_in_own_loop)
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=15)
        thread_b.join(timeout=15)

        assert not thread_a.is_alive() and not thread_b.is_alive(), (
            "Threads failed to terminate; likely a deadlock"
        )
        # Both calls must have completed successfully.
        assert len(results) == 2
        for r in results:
            assert not isinstance(r, BaseException), f"Refresh raised: {r!r}"

        # The critical assertion under the gated-generation contract:
        # AT MOST one subprocess invocation per loop (== 2 across two
        # loops). Under the legacy eager-bump contract this asserted
        # ``run_count == 1`` (cross-loop coalescing); the fix dropped
        # eager-bump to close the phantom-bump failure mode, accepting
        # that two cross-loop callers may both run their subprocess in
        # the rare concurrent-refresh case (correct end-state: fresh
        # cookies on disk).
        assert 1 <= run_count <= 2, (
            f"Expected 1–2 refresh invocations across loops, observed "
            f"{run_count}. ``run_count == 0`` would mean the cross-loop "
            "generation guard SKIPPED both refreshes (phantom-bump regression). "
            "``run_count > 2`` means each loop ran refresh more than once "
            "(per-loop coalescing broken)."
        )
