"""Phase 1.5 P1.5.1 — snapshot-invariant for `_rpc_call_impl`.

httpx merges the cookie jar into the outgoing ``httpx.Request`` synchronously
in ``build_request()``, before any ``await``. ``_rpc_call_impl`` reads
``auth.csrf_token`` and ``auth.session_id`` synchronously before
``await self._http_client.post(url, content=body)``. Therefore, the entire
``(csrf, session_id, cookies)`` snapshot is atomic from a concurrent-coroutine
standpoint: no other task can mutate state between read and the wire.

This file *locks* that invariant in two ways:

1. ``test_rpc_call_impl_has_no_await_before_post`` — static AST guard that
   fails the moment someone adds an ``await`` to the currently-synchronous
   prologue of ``_rpc_call_impl``. Conservative: any await before ``post()``
   is rejected, even if it precedes the auth reads.

2. ``test_concurrent_refresh_does_not_corrupt_inflight_rpc_request`` — runtime
   self-consistency. Drives concurrent ``refresh_auth`` against an in-flight
   ``rpc_call`` (both orderings) and asserts the captured ``httpx.Request``
   is never observed with mixed-generation (csrf, session_id, cookies) state.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap

import httpx
import pytest

from conftest import make_core  # type: ignore[import-not-found]
from notebooklm._core import ClientCore
from notebooklm.rpc import RPCMethod

# Test-side deadline for any single asyncio.Event in the race scaffolding.
# Generous enough not to flake on slow CI, tight enough that a regression
# (e.g., POST never reached the transport) fails fast instead of hanging.
EVENT_TIMEOUT_S = 5.0


def test_rpc_call_impl_has_no_await_before_post():
    """``_rpc_call_impl`` must not contain any ``await`` before its ``post()``.

    If anyone adds an ``await`` to the prologue of ``_rpc_call_impl``, a
    concurrent ``refresh_auth`` could mutate ``auth.csrf_token`` /
    ``auth.session_id`` / the cookie jar between the read and the wire,
    producing a mismatched-generation request. The rule is conservative:
    any earlier ``await`` is rejected, not just ones that fall between the
    auth reads and the ``post()`` call.
    """
    src = textwrap.dedent(inspect.getsource(ClientCore._rpc_call_impl))
    tree = ast.parse(src)
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))

    def is_post_await(node):
        if not isinstance(node, ast.Await):
            return False
        call = node.value
        if not isinstance(call, ast.Call):
            return False
        attr = call.func
        return isinstance(attr, ast.Attribute) and attr.attr == "post"

    # ast.walk does not guarantee source order, so pick the earliest match by
    # line number — robust to future code that contains multiple post() calls.
    post_await_linenos = [n.lineno for n in ast.walk(func) if is_post_await(n)]
    post_await_lineno = min(post_await_linenos, default=None)
    assert post_await_lineno is not None, (
        "Could not locate `await ...post(...)` in _rpc_call_impl. If you "
        "refactored the call site (e.g., to `self._http_client.request(...)`), "
        "update this guard to match — the invariant is 'no await before the "
        "RPC send', not specifically the `.post` attribute."
    )

    earlier_awaits = [
        n for n in ast.walk(func) if isinstance(n, ast.Await) and n.lineno < post_await_lineno
    ]
    assert not earlier_awaits, (
        f"_rpc_call_impl gained an await before the POST at line {post_await_lineno}: "
        f"{[(n.lineno, ast.dump(n)) for n in earlier_awaits]}. "
        "This breaks the snapshot-invariant — auth state could be mutated between "
        "the read and the actual send."
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

        if rpc_first:
            rpc_task = asyncio.create_task(core.rpc_call(RPCMethod.LIST_NOTEBOOKS, []))
            await asyncio.wait_for(rpc_send_entered.wait(), EVENT_TIMEOUT_S)
            refresh_task = asyncio.create_task(client.refresh_auth())
            await asyncio.wait_for(get_entered.wait(), EVENT_TIMEOUT_S)
            let_get_complete.set()
            await refresh_task
            let_rpc_send_complete.set()
            await rpc_task
        else:
            refresh_task = asyncio.create_task(client.refresh_auth())
            await asyncio.wait_for(get_entered.wait(), EVENT_TIMEOUT_S)
            rpc_task = asyncio.create_task(core.rpc_call(RPCMethod.LIST_NOTEBOOKS, []))
            await asyncio.wait_for(rpc_send_entered.wait(), EVENT_TIMEOUT_S)
            let_get_complete.set()
            await refresh_task
            let_rpc_send_complete.set()
            await rpc_task

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
