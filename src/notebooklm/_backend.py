"""Private semantic backend boundary.

The port is intentionally transport-neutral.  It accepts closed semantic
operation definitions and typed values, carries one caller-owned absolute
deadline through unchanged, and returns the operation's declared result type.
Concrete wire adapters live above this module and are not part of the protocol.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import Enum, unique
from types import MappingProxyType
from typing import Final, Protocol, TypeVar, runtime_checkable

from ._deadline import RuntimeDeadline
from ._operations import Operation, OperationDef

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


@unique
class BackendKind(str, Enum):
    """Protocol families understood by the private semantic boundary."""

    WEB = "web"
    MOBILE = "mobile"


@unique
class BackendErrorReason(str, Enum):
    """Closed neutral reasons emitted by reviewed semantic web bindings."""

    AUTH = "auth"
    ARTIFACT_FEATURE_UNAVAILABLE = "artifact_feature_unavailable"
    ARTIFACT_NOT_FOUND = "artifact_not_found"
    CHAT = "chat"
    CHAT_RESPONSE_PARSE = "chat_response_parse"
    CLIENT = "client"
    DECODING = "decoding"
    IDEMPOTENCY_VARIANT = "idempotency_variant"
    LABEL_AMBIGUOUS_CREATE = "label_ambiguous_create"
    LABEL_NOT_FOUND = "label_not_found"
    NETWORK = "network"
    NOTEBOOK_LIMIT = "notebook_limit"
    NOTEBOOK_NOT_FOUND = "notebook_not_found"
    SOURCE_NOT_FOUND = "source_not_found"
    RATE_LIMIT = "rate_limit"
    RESEARCH_START_UNAVAILABLE = "research_start_unavailable"
    RESPONSE_TOO_LARGE = "response_too_large"
    RPC = "rpc"
    SERVER = "server"
    SOURCE_ADD = "source_add"
    TIMEOUT = "timeout"
    UNKNOWN_RPC_METHOD = "unknown_rpc_method"


#: Reasons under which a dispatched mutation may have committed server-side
#: before the failure surfaced. This reproduces the web adapter's class tuple
#: ``(RateLimitError, ServerError, NetworkError)`` — ``RPCTimeoutError`` is a
#: ``NetworkError`` subclass there — as a closed reason set. Exact; never
#: widened by an adapter.
COMMIT_UNCERTAIN_REASONS: Final[frozenset[BackendErrorReason]] = frozenset(
    {
        BackendErrorReason.SERVER,
        BackendErrorReason.NETWORK,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.TIMEOUT,
    }
)


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Closed set of semantic operations implemented by one backend."""

    supported_operations: frozenset[Operation] = frozenset()

    def supports(self, operation: Operation) -> bool:
        """Return whether the backend implements ``operation``."""

        return operation in self.supported_operations


@runtime_checkable
class BackendAdapter(Protocol):
    """Neutral unary semantic backend port.

    Streaming operations may grow a separate typed protocol when migrated;
    forcing a stream through this unary method would weaken the boundary.
    """

    @property
    def kind(self) -> BackendKind:
        """Backend protocol family."""

        ...

    @property
    def capabilities(self) -> BackendCapabilities:
        """Semantic operations implemented by this backend."""

        ...

    async def invoke(
        self,
        operation: OperationDef[InputT, OutputT],
        value: InputT,
        *,
        deadline: RuntimeDeadline | None,
    ) -> OutputT:
        """Invoke one supported operation with its declared input type."""

        ...

    async def close(self) -> None:
        """Release backend-owned resources."""

        ...


@dataclass(frozen=True, slots=True)
class BackendError(Exception):
    """Smallest transport-neutral failure record returned by a backend.

    ``diagnostics`` is an opaque, already-scrubbed mapping.  The backend owns
    its contents; later compatibility projectors replay the same mapping rather
    than interpreting wire-specific fields in semantic services.

    ``outcome_unknown`` keeps its broad meaning: the workflow's requested final
    outcome is not fully confirmed and is unsafe to retry.  ``dispatched`` is a
    narrower, mechanical marker — the native runtime was entered for the failing
    call ("the runtime was entered", not "the POST was sent") — and is the
    commit-uncertainty *trigger* consumed by :func:`may_have_committed`.
    """

    message: str
    operation: Operation | None = None
    outcome_unknown: bool = False
    diagnostics: Mapping[str, object] | None = field(default=None, repr=False, hash=False)
    reason: BackendErrorReason | None = None
    dispatched: bool = False

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def _message_for(self, operation: Operation | None) -> str:
        """Return the message this error would carry if bound to ``operation``.

        Subclasses whose message is derived from the operation override this so
        :func:`rebind_operation` rebuilds it exactly as construction would.
        """
        del operation
        return self.message


class BackendContractError(BackendError):
    """A backend registration, input, or result violated its typed contract."""

    __slots__ = ()


class UnsupportedOperationError(BackendContractError):
    """The selected backend does not implement a semantic operation."""

    __slots__ = ("backend_kind",)

    backend_kind: BackendKind

    def __init__(self, operation: Operation, backend_kind: BackendKind) -> None:
        BackendError.__init__(
            self,
            message=f"{backend_kind.value} backend does not support {operation.value}",
            operation=operation,
        )
        object.__setattr__(self, "backend_kind", backend_kind)

    def _message_for(self, operation: Operation | None) -> str:
        if operation is None:
            return self.message
        return f"{self.backend_kind.value} backend does not support {operation.value}"


class BackendDeadlineExceededError(BackendError):
    """A semantic invocation exhausted the caller-owned absolute deadline."""

    __slots__ = ()

    def __init__(
        self,
        operation: Operation,
        *,
        outcome_unknown: bool = False,
        diagnostics: Mapping[str, object] | None = None,
        dispatched: bool = False,
    ) -> None:
        BackendError.__init__(
            self,
            message=f"{operation.value} exceeded its deadline",
            operation=operation,
            outcome_unknown=outcome_unknown,
            diagnostics=diagnostics,
            reason=BackendErrorReason.TIMEOUT,
            dispatched=dispatched,
        )

    def _message_for(self, operation: Operation | None) -> str:
        if operation is None:
            return self.message
        return f"{operation.value} exceeded its deadline"


_LEAF_OPERATION_DIAGNOSTIC: Final = "leaf_operation"


def _replace_backend_error(error: BackendError, **changes: object) -> BackendError:
    """Copy ``error`` with ``changes`` applied, preserving its concrete subclass.

    ``BackendError`` is a frozen, slotted dataclass whose subclasses take
    constructor arguments the base fields cannot reproduce, and
    ``BaseException.__reduce__`` would rebuild only from ``args``.  Copy field by
    field instead, including subclass-declared slots, then re-run the
    ``Exception`` initialiser so ``args`` tracks the (possibly rebuilt) message.
    """
    cls = type(error)
    clone = cls.__new__(cls)
    field_names = {item.name for item in fields(error)}
    for name in field_names:
        object.__setattr__(clone, name, changes.get(name, getattr(error, name)))
    for klass in cls.__mro__:
        for slot in getattr(klass, "__slots__", ()):
            if slot in field_names or slot in {"__dict__", "__weakref__"}:
                continue
            if hasattr(error, slot):
                object.__setattr__(clone, slot, getattr(error, slot))
    Exception.__init__(clone, clone.message)
    return clone


def mark_backend_outcome_unknown(error: BackendError) -> BackendError:
    """Return closed neutral evidence for a write whose outcome is unconfirmed.

    The returned error keeps its concrete subclass, ``dispatched`` marker,
    reason, diagnostics and message; only ``outcome_unknown`` changes.
    """
    if error.outcome_unknown:
        return error
    return _replace_backend_error(error, outcome_unknown=True)


def rebind_operation(error: BackendError, operation: Operation) -> BackendError:
    """Return ``error`` attributed to the workflow ``operation``.

    A service that sequences several leaf operations re-raises a leaf failure as
    its own operation so public exception identity and catalog attribution do
    not change.  The leaf operation is retained under the ``leaf_operation``
    diagnostics key (the innermost leaf wins across repeated rebinding); the
    subclass, ``dispatched``, ``outcome_unknown`` and reason are preserved, and
    subclasses whose message names the operation have it rebuilt.
    """
    if error.operation is operation:
        return error
    diagnostics: dict[str, object] = dict(error.diagnostics or {})
    if error.operation is not None and _LEAF_OPERATION_DIAGNOSTIC not in diagnostics:
        diagnostics[_LEAF_OPERATION_DIAGNOSTIC] = error.operation
    return _replace_backend_error(
        error,
        operation=operation,
        message=error._message_for(operation),
        diagnostics=MappingProxyType(diagnostics),
    )


def may_have_committed(error: BackendError) -> bool:
    """Whether a dispatched mutation behind ``error`` may have committed.

    Exact predicate: the native runtime was entered (``dispatched``) *and* the
    reason is one of :data:`COMMIT_UNCERTAIN_REASONS`.  A pre-dispatch deadline
    expiry, a rejected input, or a decode failure never satisfies it, whatever
    ``outcome_unknown`` says about the surrounding workflow.
    """
    return error.dispatched and error.reason in COMMIT_UNCERTAIN_REASONS


def require_leaves(backend: BackendAdapter, *operations: Operation) -> None:
    """Reject a workflow whose leaf conjunction the backend cannot execute.

    Called before a service's first credential, file or network side effect so
    an unsupported leaf is never discovered mid-workflow.  Raises
    :class:`UnsupportedOperationError` for the first unsupported leaf.
    """
    capabilities = backend.capabilities
    for operation in operations:
        if not capabilities.supports(operation):
            raise UnsupportedOperationError(operation, backend.kind)


__all__ = [
    "BackendAdapter",
    "BackendCapabilities",
    "BackendContractError",
    "BackendDeadlineExceededError",
    "BackendError",
    "BackendErrorReason",
    "BackendKind",
    "COMMIT_UNCERTAIN_REASONS",
    "mark_backend_outcome_unknown",
    "may_have_committed",
    "rebind_operation",
    "require_leaves",
    "UnsupportedOperationError",
]
