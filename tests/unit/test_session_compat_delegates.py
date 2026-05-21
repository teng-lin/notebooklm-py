"""Pin: Session compat methods that the RpcOwner Protocol or test suite
reach are present on Session AND remain delegates (not real-body code).

Phase 4 / PR 10 will delete the property-bridge layer; without this pin
that surgery could accidentally re-inline executor / coordinator logic
into Session.  The eight methods locked here are:

    six RpcExecutor-adjacent delegates
        _build_url
        _await_refresh
        _rpc_call_impl
        _raise_rpc_error_from_http_status
        _raise_rpc_error_from_request_error
        _try_refresh_and_retry

    two AuthRefreshCoordinator-adjacent delegates collapsed in PR 8
        _snapshot
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

# Six RpcExecutor-adjacent delegates + two AST-guard-relocated delegates
# (after PR 8 collapses them).
_DELEGATE_METHODS = [
    "_build_url",
    "_await_refresh",
    "_rpc_call_impl",
    "_raise_rpc_error_from_http_status",
    "_raise_rpc_error_from_request_error",
    "_try_refresh_and_retry",
    "_snapshot",  # post-PR-8 delegate to AuthRefreshCoordinator.snapshot
    "update_auth_tokens",  # post-PR-8 delegate to AuthRefreshCoordinator.update_auth_tokens
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


def test_session_keeps_transport_wrappers() -> None:
    """The three ``_ensure_observability_state()`` + ``_drain_tracker``
    wrappers must stay — the ``__new__``-fixture path relies on them
    for lazy observability init.
    """
    for name in (
        "_begin_transport_post",
        "_begin_transport_task",
        "_finish_transport_post",
    ):
        assert callable(getattr(Session, name)), f"Session.{name} missing"
