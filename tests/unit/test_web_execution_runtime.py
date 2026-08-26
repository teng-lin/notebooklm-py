"""Focused ownership tests for the P7 backend web execution runtime."""

from __future__ import annotations

from typing import Any

from notebooklm._deadline import RuntimeDeadline
from notebooklm._rpc_executor import RpcExecutor
from notebooklm._semantic.operations import Operation
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.runtime import WebExecutionRuntime
from notebooklm.rpc import RPCMethod
from tests.unit._rpc_executor_support import _executor, _Owner


def test_rpc_executor_is_behaviorless_raw_compatibility_name() -> None:
    """The old client adapter must not retain a second execution engine."""
    assert issubclass(RpcExecutor, WebExecutionRuntime)
    assert set(RpcExecutor.__dict__) <= {
        "__doc__",
        "__firstlineno__",
        "__module__",
        "__static_attributes__",
    }
    assert "_execute_once" in WebExecutionRuntime.__dict__
    assert "rpc_call" in WebExecutionRuntime.__dict__


async def test_backend_semantic_dispatch_uses_owned_runtime_and_parent_deadline() -> None:
    """Backend dispatch bypasses the compatibility alias and preserves budget identity."""
    owner = _Owner()
    runtime = _executor(owner)
    backend = WebRpcBackend(runtime)

    class _ForbiddenCompatibilityExecutor:
        async def rpc_call(self, *_args: object, **_kwargs: object) -> Any:
            raise AssertionError("semantic dispatch escaped through compatibility executor")

    backend._executor = _ForbiddenCompatibilityExecutor()  # type: ignore[assignment]
    deadline = RuntimeDeadline.start(30.0)

    result = await backend._rpc_call(
        RPCMethod.LIST_NOTEBOOKS,
        [None, 1, None, [2]],
        operation=Operation.NOTEBOOK_LIST,
        deadline=deadline,
    )

    assert result["rpc_id"] == RPCMethod.LIST_NOTEBOOKS.value
    assert len(owner.perform_calls) == 1
    assert owner.perform_calls[0]["retry_deadline"] is deadline
    assert owner.perform_calls[0]["read_timeout"] <= deadline.timeout
