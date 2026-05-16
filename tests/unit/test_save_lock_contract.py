"""Regression guard for the ``ClientCore._save_lock`` contract.

Contract (documented at ``_core.py`` next to the lock definition):
``_save_lock`` is acquired ONLY inside ``CookiePersistence.save``'s ``_save()``
closure, which runs on a worker thread via ``asyncio.to_thread``. It is never
held by an async context — a
blocking ``threading.Lock`` taken on the event-loop thread would stall every
other coroutine (keepalive, RPCs, cancellation) while a sibling worker thread
does file I/O. That is the priority-inversion failure mode this contract
exists to prevent.

These tests are deliberately structural: they verify the rule end-to-end
(via the live ``save_cookies`` path) AND statically (by scanning the cookie
persistence collaborator for any unexpected acquisition site), so a future
refactor that adds an event-loop acquisition fails fast in CI.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import threading
from pathlib import Path

import httpx
import pytest

from notebooklm._core import ClientCore
from notebooklm._core_cookie_persistence import CookiePersistence
from notebooklm.auth import AuthTokens


def _make_core(tmp_path: Path) -> ClientCore:
    """Build a minimal ``ClientCore`` whose ``save_cookies`` is safe to call.

    Order matters: ``AuthTokens.__post_init__`` calls ``build_cookie_jar``,
    which loads from ``storage_path`` if it exists and enforces the cookie-set rule. We want it to take the in-memory ``cookies={...}``
    branch (file absent) so construction succeeds, THEN write the baseline
    file so the subsequent ``save_cookies`` call has something to merge
    against.
    """
    storage_path = tmp_path / "storage_state.json"
    auth = AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y"},
        csrf_token="t",
        session_id="s",
        storage_path=storage_path,
    )
    storage_path.write_text('{"cookies": []}')
    return ClientCore(auth)


@pytest.mark.asyncio
async def test_save_lock_acquired_off_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thread that holds ``_save_lock`` MUST NOT be the event-loop thread.

    We spy on ``save_cookies_to_storage`` — which the production ``_save()``
    closure calls from inside ``with lock:`` — and record the thread it runs
    on. If a future refactor accidentally moves the ``with self._save_lock:``
    onto the loop thread (e.g. by inlining the closure into ``save_cookies``
    without ``asyncio.to_thread``), the spy will see the loop thread holding
    the lock, and this assertion will fail.
    """
    core = _make_core(tmp_path)

    loop_thread = threading.current_thread()
    observed: dict[str, object] = {}

    def spy(jar, path, **kwargs):  # type: ignore[no-untyped-def]
        # ``save_cookies_to_storage`` is called from inside ``with lock:``
        # in ``_save()``. Whichever thread runs this spy is, by definition,
        # the thread currently holding ``_save_lock``.
        observed["lock_held"] = core._save_lock.locked()
        observed["holder_thread"] = threading.current_thread()
        return True

    monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", spy)

    await core.save_cookies(httpx.Cookies())

    assert observed["lock_held"] is True, (
        "save_cookies must hold _save_lock for the duration of "
        "save_cookies_to_storage (precondition for the contract test)"
    )
    holder = observed["holder_thread"]
    assert isinstance(holder, threading.Thread)
    assert holder is not loop_thread, (
        "_save_lock contract violation: the lock was acquired on the "
        "event-loop thread. It must only be acquired inside the _save() "
        "closure dispatched via asyncio.to_thread, otherwise a blocking "
        "threading.Lock on the loop will stall every other coroutine "
        "(priority inversion)."
    )
    # Belt-and-braces: also compare ident in case some future Thread
    # subclass overrides ``__eq__``/``is`` semantics around object identity.
    # asyncio.to_thread dispatches onto the default ThreadPoolExecutor whose
    # workers are named ``asyncio_n``; we don't pin the exact name because
    # that's an implementation detail.
    assert holder.ident != loop_thread.ident


@pytest.mark.asyncio
async def test_save_lock_does_not_block_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While ``_save_lock`` is held by a worker thread, the event loop must
    remain responsive.

    Direct proof of the no-priority-inversion property: hold the worker
    inside ``save_cookies_to_storage`` (which is called from inside
    ``with self._save_lock:``) and concurrently schedule loop work. If the
    loop were blocked on the lock, the heartbeat coroutine wouldn't run
    until the worker released; with the contract intact, the heartbeat
    observes the lock IS held while the loop is still scheduling.
    """
    core = _make_core(tmp_path)

    in_save = threading.Event()
    release_save = threading.Event()
    loop_observations: list[bool] = []

    def spy(jar, path, **kwargs):  # type: ignore[no-untyped-def]
        in_save.set()
        # Hold the worker thread (and thus _save_lock) until the loop has
        # demonstrated it can still schedule coroutines. Bounded to avoid a
        # hung test if the contract is ever violated and the loop deadlocks.
        assert release_save.wait(timeout=5.0), (
            "Loop never signalled release — likely event-loop blocked on "
            "_save_lock (contract violation)."
        )
        return True

    monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", spy)

    async def heartbeat() -> None:
        # Wait for the worker to enter the spy by polling — using asyncio.sleep
        # (not run_in_executor) so we don't contend for the default executor
        # that asyncio.to_thread also uses.
        for _ in range(500):  # ~5s ceiling at 10ms cadence
            if in_save.is_set():
                break
            await asyncio.sleep(0.01)
        # If we reach here, the loop is still scheduling tasks AND the
        # worker thread is inside the spy (lock held).
        loop_observations.append(core._save_lock.locked())
        release_save.set()

    await asyncio.gather(core.save_cookies(httpx.Cookies()), heartbeat())

    assert loop_observations == [True], (
        "Event loop must remain responsive while _save_lock is held by a "
        "worker thread. If the loop blocks on the lock, heartbeat() can't "
        "observe `locked() is True` because it can't run at all — "
        f"observations={loop_observations!r}"
    )


def test_save_lock_only_acquired_inside_save_closure() -> None:
    """Static guard: the blocking lock is acquired inside the worker closure.

    This catches a refactor that adds a second ``with self.save_lock:`` or
    aliased ``with lock:`` elsewhere (e.g. inside an async method) before such
    a change can ship — static-only, so it has zero runtime cost and runs even
    when the async test infrastructure is offline.
    """

    source_path = Path(inspect.getsourcefile(CookiePersistence) or "")
    assert source_path.is_file(), (
        f"could not locate _core_cookie_persistence.py source: {source_path!r}"
    )
    tree = ast.parse(source_path.read_text())

    # Find every ``with self.save_lock:`` or closure-aliased ``with lock:`` by
    # walking the collaborator AST.
    acquisition_sites: list[tuple[str, int]] = []

    class _Visitor(ast.NodeVisitor):
        """Walk the module, tracking the enclosing function chain so any
        ``with self._save_lock:`` site can be attributed to the function
        that contains it (lets us check whether it sits inside ``_save``).
        """

        def __init__(self) -> None:
            self._enclosing_func: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._enclosing_func.append(node.name)
            self.generic_visit(node)
            self._enclosing_func.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._enclosing_func.append(node.name)
            self.generic_visit(node)
            self._enclosing_func.pop()

        def _record_if_save_lock(self, expr: ast.expr) -> None:
            # Match ``self.save_lock`` directly.
            if (
                isinstance(expr, ast.Attribute)
                and expr.attr == "save_lock"
                and isinstance(expr.value, ast.Name)
                and expr.value.id == "self"
            ) or (isinstance(expr, ast.Name) and expr.id == "lock"):
                where = ".".join(self._enclosing_func) or "<module>"
                acquisition_sites.append((where, expr.lineno))

        def visit_With(self, node: ast.With) -> None:
            for item in node.items:
                self._record_if_save_lock(item.context_expr)
            self.generic_visit(node)

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
            for item in node.items:
                # An ``async with self._save_lock:`` would itself be a contract
                # violation (lock is sync), so flag it the same way.
                self._record_if_save_lock(item.context_expr)
            self.generic_visit(node)

    _Visitor().visit(tree)

    offenders = [
        (where, lineno) for (where, lineno) in acquisition_sites if "_save" not in where.split(".")
    ]
    assert offenders == [], (
        "_save_lock contract violation: blocking lock acquisition found "
        f"outside the ``_save`` closure: {offenders!r}. The lock must "
        "ONLY be acquired inside ``_save()`` (run via asyncio.to_thread). "
        "See ``_core_cookie_persistence.py`` "
        "for the contract details."
    )
    assert acquisition_sites, "expected CookiePersistence.save to acquire the save lock"
