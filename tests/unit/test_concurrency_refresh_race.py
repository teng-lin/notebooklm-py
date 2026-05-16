"""Snapshot-invariant for the shared POST helper.

httpx merges the cookie jar into the outgoing ``httpx.Request`` synchronously
in ``build_request()``, before any ``await``. ``_perform_authed_post`` reads
the auth snapshot (via ``self._snapshot()``) and assembles ``(url, body,
headers)`` via ``build_request(snapshot)`` synchronously before
``await client.post(...)``. Therefore, within a single retry iteration the
entire ``(csrf, session_id, cookies)`` snapshot is atomic from a concurrent
coroutine standpoint: no other task can mutate state between read and the
wire.

The POST was extracted out of ``_rpc_call_impl`` into
``_perform_authed_post`` so chat can share the same transport pipeline.
The AST guard below follows the POST; the invariant still belongs at the
shared site.

The auth-snapshot lock hardened the invariant by:

- making ``_snapshot()`` ``async def`` and acquiring a dedicated
  ``_auth_snapshot_lock`` for the read, so the four scalar fields
  (``csrf_token``/``session_id``/``authuser``/``account_email``) are
  observed atomically with respect to ``refresh_auth``'s
  write-block; and
- refactoring ``_build_url()`` to consume the resulting
  ``_AuthSnapshot`` rather than reading ``self.auth`` LIVE — that
  prior live-read was the actual torn-read hazard, since it let a
  refresh's write to ``self.auth.session_id`` slip into the URL between
  snapshot capture and request build.

This file *locks* the invariant in three ways:

1. ``test_perform_authed_post_has_no_await_before_post_per_iteration`` —
   static AST guard against an ``await`` inside the retry loop's try
   body of ``_perform_authed_post`` that precedes the iteration's
   ``client.post(...)`` call. The ``await self._snapshot()`` lives
   *before* the try block (so the lock acquisition itself isn't a
   regression).

2. ``test_build_url_does_not_read_self_auth`` — static AST guard
   against any ``self.auth.<field>`` attribute access in
   ``RpcExecutor.build_url``. The method MUST consume only its
   ``snapshot: _AuthSnapshot`` parameter; reverting to ``self.auth``
   would silently un-do the atomicity fix.

3. ``test_concurrent_refresh_does_not_corrupt_inflight_rpc_request`` —
   runtime self-consistency. Drives concurrent ``refresh_auth`` against
   an in-flight ``rpc_call`` (both orderings) and asserts the captured
   ``httpx.Request`` is never observed with mixed-generation (csrf,
   session_id, cookies) state.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib.util
import inspect
import json
import textwrap
from pathlib import Path

import httpx
import pytest

from notebooklm._core import ClientCore
from notebooklm._core_rpc import RpcExecutor
from notebooklm._core_transport import AuthedTransport
from notebooklm.rpc import RPCMethod

_UNIT_CONFTEST_SPEC = importlib.util.spec_from_file_location(
    "unit_conftest_make_core",
    Path(__file__).resolve().parent / "conftest.py",
)
assert _UNIT_CONFTEST_SPEC is not None and _UNIT_CONFTEST_SPEC.loader is not None
_unit_conftest = importlib.util.module_from_spec(_UNIT_CONFTEST_SPEC)
_UNIT_CONFTEST_SPEC.loader.exec_module(_unit_conftest)
make_core = _unit_conftest.make_core

# Test-side deadline for any single asyncio.Event in the race scaffolding.
# Generous enough not to flake on slow CI, tight enough that a regression
# (e.g., POST never reached the transport) fails fast instead of hanging.
EVENT_TIMEOUT_S = 5.0


def test_perform_authed_post_has_no_await_before_post_per_iteration():
    """Within a single retry iteration of ``_perform_authed_post``, no
    ``await`` may sit lexically inside the ``try:`` block before the
    ``client.post(...)`` call.

    Each retry iteration begins with ``snapshot = await self._snapshot()``
    + a synchronous ``build_request(snapshot)`` — both immediately
    *before* the try block. The try body's first statement is the actual
    POST. If anyone introduces an ``await`` between ``build_request`` and
    the POST (or any new await inside the try body's prologue), a
    concurrent ``refresh_auth`` could update the httpx cookie jar
    between the snapshot read and the wire, producing a mismatched-
    generation request on the cookie axis (the ``_auth_snapshot_lock``
    covers csrf/sid coherence; cookies still rely on this no-await rule).

    The check is per-iteration: awaits *between* iterations (e.g.
    ``await self._await_refresh()`` followed by ``continue``) are fine
    because each iteration takes a fresh snapshot.

    Note: the lock acquisition inside ``_snapshot`` is the only
    pre-POST ``await``, and it lives *before* the try block — so this
    guard (which only walks the try body) remains valid.
    """
    src = textwrap.dedent(inspect.getsource(AuthedTransport.perform_authed_post))
    tree = ast.parse(src)
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))

    # Locate the retry loop (the outermost ``while`` in the body). The loop
    # may sit directly under the function body OR one level deeper inside
    # the RPC-semaphore ``async with self._get_rpc_semaphore():`` wrapper — both
    # structures preserve the snapshot→POST invariant this guard checks
    # (the semaphore acquire happens once per call, not per iteration).
    def _find_first_while(parent: ast.AST) -> ast.While | None:
        for child in ast.iter_child_nodes(parent):
            if isinstance(child, ast.While):
                return child
            if isinstance(child, ast.AsyncWith | ast.With):
                # Descend through context managers so the guard survives
                # wrapping the loop in ``async with semaphore``.
                found = _find_first_while(child)
                if found is not None:
                    return found
        return None

    while_loop = _find_first_while(func)
    assert while_loop is not None, (
        "Could not locate the retry-loop ``while`` in AuthedTransport.perform_authed_post. "
        "If the loop was restructured, update this guard to match."
    )

    def is_post_await(node):
        if not isinstance(node, ast.Await):
            return False
        call = node.value
        if not isinstance(call, ast.Call):
            return False
        attr = call.func
        return isinstance(attr, ast.Attribute) and attr.attr == "post"

    def _walk_outer(parent):
        """Yield nodes lexically inside ``parent`` itself (skip nested defs).

        ``ast.walk`` descends into nested ``FunctionDef`` / ``AsyncFunctionDef``
        / ``Lambda`` bodies — that would let a future helper coroutine
        smuggle the matching ``await ...post(...)`` past this guard. We only
        want statements at this lexical level. We DO descend into the loop's
        own ``try`` / ``except`` / ``if`` blocks so awaits in retry-branch
        bookkeeping (post-error handlers) remain visible to the guard.
        """
        boundaries = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        for child in ast.iter_child_nodes(parent):
            if isinstance(child, boundaries):
                continue
            yield child
            yield from _walk_outer(child)

    # Walk only the ``try`` block at the top of the while body — that's the
    # critical prologue → POST window. Awaits in ``except`` handlers are by
    # definition AFTER the POST (post-error path) and don't violate the
    # invariant. ``self._snapshot()`` and ``build_request(snapshot)`` are
    # synchronous assignments inside the while body before the try, so
    # they're irrelevant to this guard.
    try_node = next(
        (n for n in ast.iter_child_nodes(while_loop) if isinstance(n, ast.Try)),
        None,
    )
    assert try_node is not None, (
        "Could not locate the ``try:`` block guarding the POST in "
        "AuthedTransport.perform_authed_post. Update this guard if the structure changed."
    )
    # We only walk the try body, NOT its handlers.
    try_body_nodes: list[ast.AST] = []
    for stmt in try_node.body:
        try_body_nodes.append(stmt)
        try_body_nodes.extend(_walk_outer(stmt))

    post_await_positions = [(n.lineno, n.col_offset) for n in try_body_nodes if is_post_await(n)]
    post_await_position = min(post_await_positions, default=None)
    assert post_await_position is not None, (
        "Could not locate `await ...post(...)` in the try body of "
        "AuthedTransport.perform_authed_post. If the call site was refactored (e.g. to "
        "``client.request(...)``), update this guard to match — the "
        "invariant is 'no await between snapshot read and the POST per "
        "iteration', not specifically the `.post` attribute."
    )

    earlier_awaits = [
        n
        for n in try_body_nodes
        if isinstance(n, ast.Await) and (n.lineno, n.col_offset) < post_await_position
    ]
    assert not earlier_awaits, (
        f"AuthedTransport.perform_authed_post gained an await before the per-iteration POST "
        f"at {post_await_position}: "
        f"{[(n.lineno, ast.dump(n)) for n in earlier_awaits]}. "
        "This breaks the snapshot-invariant — auth state could be mutated "
        "between the snapshot read and the actual send."
    )


def test_build_url_does_not_read_self_auth():
    """``RpcExecutor.build_url`` must consume only its ``snapshot`` parameter.

    pre-fix, ``_build_url`` reached into ``self.auth``
    on every call to read ``session_id``, ``authuser``, and
    ``account_email``. With ``_snapshot()`` and ``_build_url`` running
    on separate Python statements, a concurrent ``refresh_auth`` could
    flip ``self.auth.session_id`` between snapshot capture and URL build
    — producing a request whose URL was stamped with the *new*
    generation while the body still carried the *old* CSRF.

    The fix made ``_build_url`` accept ``snapshot: _AuthSnapshot`` and
    read every auth scalar off the snapshot. This guard asserts that
    contract statically so a future "convenience" refactor (e.g.
    "let's just read ``self.auth`` again, it's right there") can't
    silently re-introduce the torn read.

    Allowed reads inside ``_build_url``: ``snapshot.session_id``,
    ``snapshot.authuser``, ``snapshot.account_email``, anything not
    rooted at ``self.auth``. Forbidden: any ``self.auth.<field>``
    attribute access, regardless of which field.
    """
    src = textwrap.dedent(inspect.getsource(RpcExecutor.build_url))
    tree = ast.parse(src)
    # ``_build_url`` is a sync method, not async.
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))

    forbidden: list[tuple[int, str]] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Attribute):
            continue
        # Looking for ``self.auth`` (Attribute whose .value is Name "self"
        # and .attr is "auth"). That's the immediate parent of any
        # ``self.auth.<field>`` read.
        if isinstance(node.value, ast.Name) and node.value.id == "self" and node.attr == "auth":
            forbidden.append((node.lineno, ast.dump(node)))

    assert not forbidden, (
        f"RpcExecutor.build_url reads self.auth — torn-read regression. "
        f"Read every auth scalar off the ``snapshot`` parameter instead. "
        f"Occurrences: {forbidden}"
    )


def test_snapshot_acquires_auth_snapshot_lock():
    """``ClientCore._snapshot`` must acquire ``_auth_snapshot_lock``.

    The lock is the only thing that serializes the four-scalar
    snapshot read with the matching two-scalar write in
    ``NotebookLMClient.refresh_auth``. Removing the ``async with`` block
    here would re-open the torn-read window between
    ``self.auth.csrf_token`` and ``self.auth.session_id`` reads, even
    though those two attribute reads are individually atomic at the
    Python bytecode level.

    This guard asserts that ``_snapshot``'s body contains an
    ``async with`` whose context expression resolves to
    ``self._get_auth_snapshot_lock()`` (or, defensively, anything
    referencing ``_auth_snapshot_lock`` so a maintainer who inlines the
    lazy accessor doesn't trip the guard).
    """
    src = textwrap.dedent(inspect.getsource(ClientCore._snapshot))
    tree = ast.parse(src)
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))

    has_lock_acquisition = False
    for node in ast.walk(func):
        if not isinstance(node, ast.AsyncWith):
            continue
        # Each ``async with X`` may chain multiple items; check each.
        for item in node.items:
            ctx = item.context_expr
            # Match both call form ``self._get_auth_snapshot_lock()`` and
            # direct attribute ``self._auth_snapshot_lock``.
            if isinstance(ctx, ast.Call):
                ctx = ctx.func
            if isinstance(ctx, ast.Attribute) and "auth_snapshot_lock" in ctx.attr:
                has_lock_acquisition = True
                break

    assert has_lock_acquisition, (
        "_snapshot() no longer acquires _auth_snapshot_lock. Atomicity "
        "contract broken — the four-scalar snapshot read is no longer atomic with the "
        "refresh-side write block in NotebookLMClient.refresh_auth, exposing "
        "torn (csrf, sid) reads."
    )


def test_update_auth_tokens_has_no_await_inside_mutation_block():
    """``update_auth_tokens`` may await lock acquisition, but not while mutating."""
    src = textwrap.dedent(inspect.getsource(ClientCore.update_auth_tokens))
    tree = ast.parse(src)
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))

    mutation_try = next(
        (
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Try)
            and any(
                isinstance(stmt, ast.Assign)
                and any(
                    isinstance(target, ast.Attribute)
                    and target.attr in {"csrf_token", "session_id"}
                    for target in stmt.targets
                )
                for stmt in node.body
            )
        ),
        None,
    )
    assert mutation_try is not None, (
        "Could not locate the guarded csrf/session_id mutation block in "
        "ClientCore.update_auth_tokens."
    )

    awaits = [node for node in ast.walk(mutation_try) if isinstance(node, ast.Await)]
    assert awaits == [], (
        "ClientCore.update_auth_tokens must not await inside the critical "
        "mutation block; doing so would let snapshots observe torn auth tokens."
    )


def _synthetic_rpc_response_text(rpc_id: str) -> str:
    """Build a minimal valid batchexecute response that decodes to []."""
    inner = json.dumps([])
    chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


@pytest.mark.parametrize("rpc_first", [True, False], ids=["rpc-first", "refresh-first"])
async def test_concurrent_refresh_does_not_corrupt_inflight_rpc_request(rpc_first):
    """Every outgoing RPC must carry a coherent (csrf, session_id, cookies) tuple.

    On current code both parameterizations observe OLD/OLD/OLD: the RPC's
    request is fully built (synchronously) while refresh is still suspended
    in its GET, so all three values are captured from the pre-rotation state.
    The assertion below catches the broken case where a future refactor
    introduces a yield point between auth read and ``post()`` — letting
    refresh complete in between would produce mixed generations.
    """
    captured_post: list[dict] = []
    rpc_send_entered = asyncio.Event()
    let_rpc_send_complete = asyncio.Event()
    get_entered = asyncio.Event()
    let_get_complete = asyncio.Event()

    rpc_method_id = RPCMethod.LIST_NOTEBOOKS.value

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured_post.append(
                {
                    "url": str(request.url),
                    "cookie": request.headers.get("cookie", ""),
                    "body": bytes(request.content),
                }
            )
            rpc_send_entered.set()
            await let_rpc_send_complete.wait()
            return httpx.Response(200, text=_synthetic_rpc_response_text(rpc_method_id))
        get_entered.set()
        await let_get_complete.wait()
        body = '<script>"SNlM0e":"CSRF_NEW","FdrFJe":"SID_NEW"</script>'
        return httpx.Response(
            200,
            text=body,
            headers={"set-cookie": "SID=new_sid_cookie; Path=/; Domain=.google.com"},
        )

    transport = httpx.MockTransport(handler)

    async with make_core(transport=transport) as core:
        # NotebookLMClient.__new__ skips __init__ side effects — we only need a
        # shell whose .auth property routes to our test core.
        from notebooklm.client import NotebookLMClient

        client = NotebookLMClient.__new__(NotebookLMClient)
        client._core = core

        # try/finally ensures the mock-transport handlers are unblocked even
        # if a wait_for times out — otherwise pending tasks dangle in the
        # event loop and the test hangs until pytest's own timeout fires.
        rpc_task: asyncio.Task | None = None
        refresh_task: asyncio.Task | None = None
        try:
            if rpc_first:
                rpc_task = asyncio.create_task(core.rpc_call(RPCMethod.LIST_NOTEBOOKS, []))
                await asyncio.wait_for(rpc_send_entered.wait(), EVENT_TIMEOUT_S)
                refresh_task = asyncio.create_task(client.refresh_auth())
                await asyncio.wait_for(get_entered.wait(), EVENT_TIMEOUT_S)
                let_get_complete.set()
                await asyncio.wait_for(refresh_task, EVENT_TIMEOUT_S)
                let_rpc_send_complete.set()
                await asyncio.wait_for(rpc_task, EVENT_TIMEOUT_S)
            else:
                refresh_task = asyncio.create_task(client.refresh_auth())
                await asyncio.wait_for(get_entered.wait(), EVENT_TIMEOUT_S)
                rpc_task = asyncio.create_task(core.rpc_call(RPCMethod.LIST_NOTEBOOKS, []))
                await asyncio.wait_for(rpc_send_entered.wait(), EVENT_TIMEOUT_S)
                let_get_complete.set()
                await asyncio.wait_for(refresh_task, EVENT_TIMEOUT_S)
                let_rpc_send_complete.set()
                await asyncio.wait_for(rpc_task, EVENT_TIMEOUT_S)
        finally:
            # Always release the mock-transport gates so any in-flight handlers
            # can return — even if the test errored above.
            let_get_complete.set()
            let_rpc_send_complete.set()
            pending = [t for t in (rpc_task, refresh_task) if t is not None and not t.done()]
            for t in pending:
                t.cancel()
            # Bounded join so cancelled tasks actually settle before the
            # ``async with make_core(...)`` block exits. Narrow to
            # ``(CancelledError, Exception)`` so KeyboardInterrupt / SystemExit
            # during the test still propagate.
            if pending:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        EVENT_TIMEOUT_S,
                    )

    assert len(captured_post) == 1, (
        f"Expected exactly one POST on the wire, got {len(captured_post)}: {captured_post!r}"
    )
    seen = captured_post[0]
    cookie_is_new = "new_sid_cookie" in seen["cookie"]
    cookie_is_old = "old_sid_cookie" in seen["cookie"]
    csrf_is_new = b"CSRF_NEW" in seen["body"]
    csrf_is_old = b"CSRF_OLD" in seen["body"]
    sid_is_new = "SID_NEW" in seen["url"]
    sid_is_old = "SID_OLD" in seen["url"]

    # Sanity: each indicator is unambiguous (exactly one of old/new per axis).
    # Without this, the coherence check below could false-pass when both
    # "is_new" indicators are False simply because the markers weren't injected.
    assert cookie_is_old ^ cookie_is_new, (
        f"Cookie axis ambiguous (old={cookie_is_old}, new={cookie_is_new}): {seen['cookie']!r}"
    )
    assert csrf_is_old ^ csrf_is_new, (
        f"CSRF axis ambiguous (old={csrf_is_old}, new={csrf_is_new}): body did not contain "
        f"a recognizable CSRF marker"
    )
    assert sid_is_old ^ sid_is_new, (
        f"Session-ID axis ambiguous (old={sid_is_old}, new={sid_is_new}): {seen['url']!r}"
    )

    # The invariant: all three axes must agree (all-OLD or all-NEW). Any mix
    # indicates an unexpected yield in the prologue.
    assert cookie_is_new == csrf_is_new == sid_is_new, (
        f"Mixed-generation request observed (cookie_new={cookie_is_new}, "
        f"csrf_new={csrf_is_new}, sid_new={sid_is_new}). A yield point was "
        f"introduced between auth read and post() in _rpc_call_impl — re-run "
        f"the AST guard above to find the offending await."
    )
