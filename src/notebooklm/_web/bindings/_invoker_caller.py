"""Row-scoped ``RpcCaller`` for the legacy note-backed helpers (P9.4b).

``LegacyNoteBackedService`` and ``NoteBackedMindMapService`` take the narrow
:class:`~notebooklm._runtime.contracts.RpcCaller` capability and select their
own ``RPCMethod``.  Below the port a custom row may only reach the natives it
declared, so this caller maps each legacy ``RPCMethod`` onto one of the row's
spec keys and dispatches through the row-scoped invoker — never the transport.
The mapping is closed: a method the row did not declare is a contract error,
which is what makes the catalog's row-derived natives exact.

A semantic deadline expiry surfaces to the legacy helpers as the
``RPCTimeoutError`` they expect (the P6 ``DeadlineRpcCaller`` contract), raised
outside the private deadline-error frame so the closed public failure graph never
sees a ``BackendError``; the selected native's tag is copied onto it so
attribution survives the rethrow (plan open item 2).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..._backend import BackendContractError, BackendDeadlineExceededError
from ..._binding import CodecPayload, RowInvoker
from ..._deadline import RuntimeDeadline
from ..._operations import Operation
from ...exceptions import RPCTimeoutError
from ...rpc import RPCMethod


class InvokerRpcCaller:
    """Bind one row's invoker and deadline to the legacy ``RpcCaller`` signature."""

    __slots__ = ("_deadline", "_invoke", "_operation", "_spec_keys")

    def __init__(
        self,
        invoke: RowInvoker,
        deadline: RuntimeDeadline | None,
        *,
        operation: Operation,
        spec_keys: Mapping[RPCMethod, tuple[str, str | None]],
    ) -> None:
        self._invoke = invoke
        self._deadline = deadline
        self._operation = operation
        # method -> (spec key, declared variant)
        self._spec_keys = spec_keys

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
        # The semantic deadline is the only timeout authority for this composite;
        # ``_is_retry`` is the runtime's own auth-refresh recursion flag.
        del read_timeout, _is_retry
        binding = self._spec_keys.get(method)
        if binding is None:
            raise BackendContractError(
                f"{self._operation.value} declares no native spec for {method.name}",
                operation=self._operation,
            )
        spec_key, declared_variant = binding
        if operation_variant != declared_variant:
            raise BackendContractError(
                f"{self._operation.value} declares {method.name} with variant "
                f"{declared_variant!r}, not {operation_variant!r}",
                operation=self._operation,
            )
        payload = CodecPayload(
            params=params,
            source_path=source_path,
            allow_null=allow_null,
            raise_on_null_status=raise_on_null_status,
        )
        timeout_error: RPCTimeoutError | None = None
        try:
            return await self._invoke.call(
                spec_key,
                payload,
                deadline=self._deadline,
                disable_internal_retries=disable_internal_retries,
            )
        except BackendDeadlineExceededError as exc:
            timeout_error = RPCTimeoutError(
                f"Request timed out calling {method.name}",
                method_id=method.value,
                timeout_seconds=(self._deadline.timeout if self._deadline is not None else None),
            )
            native = getattr(exc, "binding_native", None)
            if native is not None:
                timeout_error.binding_native = native  # type: ignore[attr-defined]
        # Raise outside the private deadline-error frame: the legacy composite
        # applies its ordinary uncertainty policy and the public failure
        # projector never sees a BackendError in the chain.
        assert timeout_error is not None
        raise timeout_error


__all__ = ["InvokerRpcCaller"]
