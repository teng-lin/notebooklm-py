"""Deadline-bound compatibility caller for legacy web workflow helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._backend import BackendDeadlineExceededError
from .._deadline import RuntimeDeadline
from .._operations import Operation
from ..exceptions import RPCTimeoutError
from ..rpc import RPCMethod

if TYPE_CHECKING:
    from .backend import WebRpcBackend


class DeadlineRpcCaller:
    """Bind one semantic operation and absolute deadline to legacy RPC helpers."""

    __slots__ = ("_backend", "_deadline", "_operation")

    def __init__(
        self,
        backend: WebRpcBackend,
        deadline: RuntimeDeadline | None,
        operation: Operation,
    ) -> None:
        self._backend = backend
        self._deadline = deadline
        self._operation = operation

    async def rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
        read_timeout: float | None = None,
        raise_on_null_status: bool = False,
    ) -> Any:
        # The semantic deadline is the only timeout authority for this composite.
        # A feature helper cannot replace it with a fresh relative timeout.
        # ``_is_retry`` is the runtime's own auth-refresh recursion flag; it is
        # accepted only so this caller keeps the ``RpcCaller`` signature.
        del read_timeout, _is_retry
        timeout_error: RPCTimeoutError | None = None
        try:
            return await self._backend._rpc_call(
                method,
                params,
                operation=self._operation,
                deadline=self._deadline,
                source_path=source_path,
                allow_null=allow_null,
                disable_internal_retries=disable_internal_retries,
                operation_variant=operation_variant,
                raise_on_null_status=raise_on_null_status,
            )
        except BackendDeadlineExceededError:
            timeout_error = RPCTimeoutError(
                f"Request timed out calling {method.name}",
                method_id=method.value,
                timeout_seconds=(self._deadline.timeout if self._deadline is not None else None),
            )
        # Raise outside the private deadline-error frame. The legacy composite
        # can now apply its ordinary uncertainty policy without leaking a
        # BackendError into the closed public failure graph.
        assert timeout_error is not None
        raise timeout_error


__all__ = ["DeadlineRpcCaller"]
