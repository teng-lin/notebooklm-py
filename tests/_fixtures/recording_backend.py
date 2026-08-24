"""Recording semantic backend for service-level tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeVar, cast

from notebooklm._backend import (
    BackendCapabilities,
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    BackendKind,
    UnsupportedOperationError,
)
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation, OperationDef

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


@dataclass(frozen=True, slots=True)
class BackendInvocation:
    """One validated fake-backend invocation."""

    operation: Operation
    value: object
    deadline: RuntimeDeadline | None


def scripted_error(
    reason: BackendErrorReason,
    *,
    operation: Operation | None = None,
    dispatched: bool = False,
    outcome_unknown: bool = False,
    message: str | None = None,
    diagnostics: Mapping[str, object] | None = None,
) -> BackendError:
    """Build one neutral failure for :meth:`RecordingBackend.set_sequence`.

    ``dispatched`` and ``outcome_unknown`` are scripted explicitly so service
    tests can drive the commit-uncertainty predicate through every cell of its
    truth table without a web runtime.  A ``TIMEOUT`` reason with an operation
    yields the deadline subclass, matching what a real backend raises.
    """
    if reason is BackendErrorReason.TIMEOUT and operation is not None:
        return BackendDeadlineExceededError(
            operation,
            outcome_unknown=outcome_unknown,
            diagnostics=MappingProxyType(dict(diagnostics or {})),
            dispatched=dispatched,
        )
    return BackendError(
        message=message if message is not None else f"scripted {reason.value} failure",
        operation=operation,
        outcome_unknown=outcome_unknown,
        diagnostics=MappingProxyType(dict(diagnostics or {})),
        reason=reason,
        dispatched=dispatched,
    )


class RecordingBackend:
    """Typed fake that records calls and returns explicitly registered results."""

    def __init__(self, *, kind: BackendKind = BackendKind.WEB) -> None:
        self.kind = kind
        self.capabilities = BackendCapabilities()
        self.invocations: list[BackendInvocation] = []
        self.closed = False
        self._definitions: dict[Operation, OperationDef[object, object]] = {}
        self._results: dict[Operation, object] = {}
        self._errors: dict[Operation, BackendError] = {}
        self._sequences: dict[Operation, list[object]] = {}

    def set_sequence(
        self,
        operation: OperationDef[InputT, OutputT],
        responses: Sequence[OutputT | BaseException],
    ) -> None:
        """Script one response per invocation, consumed in order.

        Each item is either a result of the declared output type or an
        exception instance to raise (typically a :class:`BackendError` built by
        :func:`scripted_error`).  Invoking past the end of the script is a
        contract error so a workflow that issues one call too many fails loudly
        instead of replaying a stale response.
        """
        scripted: list[object] = []
        for response in responses:
            if isinstance(response, BaseException):
                if isinstance(response, BackendError) and response.operation not in {
                    None,
                    operation.key,
                }:
                    raise ValueError(
                        f"{operation.key.value} cannot raise an error for "
                        f"{response.operation.value}"
                    )
            elif not isinstance(response, operation.output_type):
                raise TypeError(
                    f"{operation.key.value} result must be {operation.output_type.__name__}, "
                    f"got {type(response).__name__}"
                )
            scripted.append(response)
        self._definitions[operation.key] = cast(OperationDef[object, object], operation)
        self._sequences[operation.key] = scripted
        self._results.pop(operation.key, None)
        self._errors.pop(operation.key, None)
        self.capabilities = BackendCapabilities(frozenset(self._definitions))

    def set_result(
        self,
        operation: OperationDef[InputT, OutputT],
        result: OutputT,
    ) -> None:
        """Register one result after validating its declared output type."""

        if not isinstance(result, operation.output_type):
            raise TypeError(
                f"{operation.key.value} result must be {operation.output_type.__name__}, "
                f"got {type(result).__name__}"
            )
        self._definitions[operation.key] = cast(OperationDef[object, object], operation)
        self._results[operation.key] = result
        self._errors.pop(operation.key, None)
        self._sequences.pop(operation.key, None)
        self.capabilities = BackendCapabilities(frozenset(self._definitions))

    def set_error(
        self,
        operation: OperationDef[InputT, OutputT],
        error: BackendError,
    ) -> None:
        """Register one neutral failure for an operation.

        Error-path tests use this backend seam instead of inserting a
        test-only stage into the production HTTP middleware chain.  The
        operation field may be omitted by a generic fixture, but a conflicting
        operation is rejected immediately so failure evidence cannot be
        attributed to the wrong semantic call.
        """
        if error.operation not in {None, operation.key}:
            raise ValueError(
                f"{operation.key.value} cannot raise an error for {error.operation.value}"
            )
        self._definitions[operation.key] = cast(OperationDef[object, object], operation)
        self._errors[operation.key] = error
        self._results.pop(operation.key, None)
        self._sequences.pop(operation.key, None)
        self.capabilities = BackendCapabilities(frozenset(self._definitions))

    async def invoke(
        self,
        operation: OperationDef[InputT, OutputT],
        value: InputT,
        *,
        deadline: RuntimeDeadline | None,
    ) -> OutputT:
        """Validate and record one call before returning its registered result."""

        if not self.capabilities.supports(operation.key):
            raise UnsupportedOperationError(operation.key, self.kind)

        registered = self._definitions[operation.key]
        if registered != operation:
            raise BackendContractError(
                f"{operation.key.value} was invoked with an unregistered operation definition",
                operation=operation.key,
            )
        if not isinstance(value, operation.input_type):
            raise BackendContractError(
                f"{operation.key.value} input must be {operation.input_type.__name__}, "
                f"got {type(value).__name__}",
                operation=operation.key,
            )

        self.invocations.append(BackendInvocation(operation.key, value, deadline))
        error = self._errors.get(operation.key)
        if error is not None:
            raise error
        sequence = self._sequences.get(operation.key)
        if sequence is not None:
            if not sequence:
                raise BackendContractError(
                    f"{operation.key.value} scripted sequence is exhausted",
                    operation=operation.key,
                )
            scripted = sequence.pop(0)
            if isinstance(scripted, BaseException):
                raise scripted
            result = scripted
        else:
            result = self._results[operation.key]
        if not isinstance(result, operation.output_type):
            raise BackendContractError(
                f"{operation.key.value} registered result no longer matches "
                f"{operation.output_type.__name__}",
                operation=operation.key,
            )
        return cast(OutputT, result)

    async def close(self) -> None:
        """Record lifecycle closure without constructing runtime resources."""

        self.closed = True


__all__ = ["BackendInvocation", "RecordingBackend", "scripted_error"]
