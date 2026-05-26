"""Pin: Session compat methods that the RpcOwner Protocol or test suite
reach are present on Session AND remain delegates (not real-body code).

Phase 4 / PR 10 deleted the property-bridge layer; without this pin
that surgery could have accidentally re-inlined executor / coordinator
logic into Session. The pin still guards against the reverse direction
(real logic creeping back into Session) for the delegates that remain.

PR #4b of the session-refactor arc (inline-pure-delegates) deleted
most of the previously-pinned delegates entirely — every caller now
talks to the canonical collaborator directly
(``core._auth_coord.snapshot(self)``, ``core._get_rpc_executor().build_url(...)``,
``core._drain_tracker.begin_transport_post(...)``, etc.). The
surviving delegates retained on Session are kept because each has a
structural protocol caller, a Protocol-imposed call site, or an
established test-seam swap point that would require its own follow-up
to migrate:

    Protocol-locked (``RpcOwner`` in ``_rpc_executor.py``)
        _await_refresh
        _rpc_call_impl

    External Protocol surface (``RefreshAuthCore`` in ``_auth/session.py``)
        update_auth_tokens

A delegate body must be at most three top-level statements with at
least one outbound collaborator call AND no forbidden control-flow
nodes (``async with``, ``with``, ``try`` / ``except``, ``for`` / ``while``
loops, ``if`` / ``else`` branching, comprehensions, ``IfExp``). The test
does not (and cannot easily) enforce "exactly one terminal expression"
or "at most one await" — those are advisory contracts; the AST checks
above are the load-bearing constraints. A reviewer noticing a delegate
that violates the advisory contract should fix it manually.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from notebooklm._session import Session

# Delegates that MUST stay on Session because an external protocol or
# load-bearing test seam still resolves them. See module docstring for
# the per-method rationale; the deleted ones (``_build_url``,
# ``_raise_rpc_error_from_http_status``, ``_raise_rpc_error_from_request_error``,
# ``_try_refresh_and_retry``, ``_snapshot``) were inlined when their
# external callers migrated to the canonical collaborator method.
_DELEGATE_METHODS = [
    "_await_refresh",
    "_rpc_call_impl",
    "update_auth_tokens",
]

# AST node classes that indicate real logic, not delegation.
_FORBIDDEN_NODE_TYPES = (
    ast.AsyncWith,
    ast.With,
    ast.Try,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.If,
    ast.IfExp,
    ast.ListComp,
    ast.DictComp,
    ast.SetComp,
    ast.GeneratorExp,
)


def _function_body_without_docstring(method) -> list[ast.stmt]:
    """Return the AST body of ``method`` with any leading docstring removed."""
    src = textwrap.dedent(inspect.getsource(method))
    tree = ast.parse(src)
    # ``inspect.getsource(method)`` returns the method's source with the
    # FunctionDef/AsyncFunctionDef at top level. Read it directly from
    # ``tree.body[0]`` rather than ``ast.walk`` (which yields nodes in
    # unspecified order and could surface a nested function defined
    # inside the body instead of the method itself).
    func = tree.body[0]
    assert isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef))
    body = func.body
    # ``ast.get_docstring`` correctly distinguishes a docstring (a string
    # literal at the start of the body) from a bare constant expression
    # of any other type, which a naive ``isinstance(..., ast.Constant)``
    # check would also strip.
    if ast.get_docstring(func) is not None:
        body = body[1:]
    return body


@pytest.mark.parametrize("name", _DELEGATE_METHODS)
def test_session_method_is_delegate(name: str) -> None:
    """Each pinned Session method must remain a small delegate body."""
    method = getattr(Session, name, None)
    assert callable(method), f"Session.{name} missing"

    body = _function_body_without_docstring(method)

    # Hard cap: delegate bodies are 1-3 statements.
    assert len(body) <= 3, (
        f"Session.{name} has {len(body)} statements; expected <= 3 for a "
        f"delegate. If you re-added logic here, move it to "
        f"RpcExecutor / AuthRefreshCoordinator and keep this method as a "
        f"1-3-stmt delegate."
    )

    # Walk all nested nodes — flag any control-flow construct that would
    # indicate real logic hidden inside a "delegate".
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, _FORBIDDEN_NODE_TYPES):
                pytest.fail(
                    f"Session.{name} contains a {type(node).__name__} node — "
                    f"delegates may not branch, loop, or `async with`. "
                    f"Move the logic to RpcExecutor or AuthRefreshCoordinator."
                )


@pytest.mark.parametrize("name", _DELEGATE_METHODS)
def test_session_delegate_calls_collaborator(name: str) -> None:
    """A delegate must contain at least one call expression that
    dispatches into a collaborator (executor, coordinator, drain tracker,
    etc.).  A delegate body with no outbound call is real logic in
    disguise.
    """
    method = getattr(Session, name)
    body = _function_body_without_docstring(method)
    has_collaborator_call = False
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                # Look for ``self._foo.bar(...)`` or
                # ``self._get_foo().bar(...)`` — both shapes route through
                # an attribute on the result of another expression.
                target = node.func
                if isinstance(target.value, (ast.Attribute, ast.Call)):
                    has_collaborator_call = True
                    break
        if has_collaborator_call:
            break
    assert has_collaborator_call, (
        f"Session.{name} has no outbound collaborator call — it is not "
        f"a delegate.  Move the body to RpcExecutor or AuthRefreshCoordinator."
    )


def test_session_satisfies_rpc_owner_protocol_members() -> None:
    """RpcOwner requires ``_rpc_call_impl``, ``_await_refresh``,
    ``_perform_authed_post``, ``rpc_call``, ``_increment_metrics`` and
    ``_emit_rpc_event`` as methods (per ``src/notebooklm/_rpc_executor.py``
    lines 54-97).  All must be present and callable; only the first two
    are checked for delegate-shape because the others are facade
    methods with legitimate logic bodies.
    """
    for name in (
        "_rpc_call_impl",
        "_await_refresh",
        "_perform_authed_post",
        "rpc_call",
        "_increment_metrics",
        "_emit_rpc_event",
    ):
        assert hasattr(Session, name), f"Session missing RpcOwner member: {name}"
        assert callable(getattr(Session, name)), f"Session.{name} not callable"


def test_session_keeps_drain_tracker_seam() -> None:
    """The ``_drain_tracker`` collaborator must remain reachable as an
    attribute on Session so feature code and tests can address it
    directly. The ``_begin_transport_post`` / ``_begin_transport_task``
    / ``_finish_transport_post`` thin wrappers that previously lived
    here were inlined in PR #4b (inline-pure-delegates); every caller
    now talks to ``core._drain_tracker.begin_transport_post(...)`` and
    friends. This pin protects the underlying attribute seam — both
    that Session exposes ``_drain_tracker`` and that the tracker has
    the methods the inlined call sites now reach for.
    """
    from notebooklm._transport_drain import TransportDrainTracker
    from notebooklm.auth import AuthTokens

    core = Session(
        AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="sid"),
    )
    assert isinstance(core._drain_tracker, TransportDrainTracker), (
        "Session must expose ``_drain_tracker`` as a ``TransportDrainTracker`` "
        "instance — feature code and tests address the tracker through this "
        "attribute now that the Session-level wrappers are gone."
    )
    for method in (
        "begin_transport_post",
        "begin_transport_task",
        "finish_transport_post",
    ):
        assert hasattr(core._drain_tracker, method), (
            f"TransportDrainTracker.{method} missing — Session call sites "
            f"that were inlined in PR #4b now resolve to this method."
        )
