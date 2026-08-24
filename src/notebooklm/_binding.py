"""Neutral binding vocabulary shared by every semantic backend.

A binding row ties one closed :class:`~notebooklm._operations.OperationDef` to
the way a backend executes it.  Three row kinds exist:

* :class:`CodecBinding` — ``encode → one native call → decode``; the row's
  :class:`NativeCallSpec` is the sole authority for the native method.
* :class:`CustomBinding` — a handler that may sequence several declared
  natives through a row-scoped :class:`RowInvoker`; every such row states a
  one-sentence justification under a closed category so the count can ratchet.
* :class:`ResolvedHandlerBinding` — a P1–P6 handler method resolved once at
  construction; it exists only while the P9.3/P9.4 conversions replace it.

This module is deliberately backend-agnostic: it names no wire enum, no HTTP
client, and no ``_web`` module.  Request assembly is delegated to the backend's
:class:`Transport`, so :func:`invoke_binding` never sees a wire-specific request
type.  The neutrality is what lets ``mypy`` check dispatch end to end.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum, unique
from types import MappingProxyType
from typing import Any, Final, Generic, Literal, Protocol, TypeVar

from ._backend import BackendContractError, BackendError
from ._deadline import RuntimeDeadline
from ._operations import Operation, OperationDef

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")
MethodT = TypeVar("MethodT")
# ``RequestT`` is invariant: ``Transport.assemble`` produces it and
# ``Transport.call``/``stream`` consume it.
RequestT = TypeVar("RequestT")
InputT_contra = TypeVar("InputT_contra", contravariant=True)
OutputT_co = TypeVar("OutputT_co", covariant=True)


@unique
class OperationDisposition(str, Enum):
    """How one backend positions an operation relative to its binding table."""

    SUPPORTED_DIRECT = "supported_direct"
    SERVICE_OWNED = "service_owned"
    UNSUPPORTED = "unsupported"


@unique
class DeadlineMode(str, Enum):
    """Whether a row inherits the caller's deadline or deliberately ignores it."""

    INHERIT = "inherit"
    IGNORE = "ignore"


@unique
class ErrorMode(str, Enum):
    """Per-row failure projection applied at translation time."""

    TRANSLATE = "translate"
    RAW_PASSTHROUGH = "raw_passthrough"
    TRANSLATE_SCRUBBED = "translate_scrubbed"


CustomCategory = Literal["protocol", "compatibility", "deferred-product"]
CUSTOM_CATEGORIES: Final[tuple[str, ...]] = ("protocol", "compatibility", "deferred-product")


class BindingAuditError(Exception):
    """A binding table disagrees with the registry dispositions it must mirror."""


@dataclass(frozen=True, slots=True)
class NativeChoice(Generic[MethodT]):
    """One selected ``(method, variant)`` pair."""

    method: MethodT
    variant: str | None = None


@dataclass(frozen=True, slots=True)
class NativeCallSpec(Generic[MethodT]):
    """Sole authority for the native a row dispatches.

    Either a constant (one choice, no selector) or a finite input-keyed choice
    whose ``selector`` must return one of the declared ``choices``.  ``key``
    names the spec inside a :class:`CustomBinding` so its handler can address
    it through the row-scoped invoker.
    """

    choices: tuple[NativeChoice[MethodT], ...]
    selector: Callable[[Any], NativeChoice[MethodT]] | None = None
    key: str | None = None

    def __post_init__(self) -> None:
        if not self.choices:
            raise ValueError("a native call spec declares at least one choice")
        if self.selector is None and len(self.choices) != 1:
            raise ValueError("a constant native call spec declares exactly one choice")
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("native call spec choices must be distinct")

    @classmethod
    def constant(
        cls,
        method: MethodT,
        variant: str | None = None,
        *,
        key: str | None = None,
    ) -> NativeCallSpec[MethodT]:
        return cls(choices=(NativeChoice(method, variant),), key=key)

    @classmethod
    def keyed(
        cls,
        selector: Callable[[Any], NativeChoice[MethodT]],
        *choices: NativeChoice[MethodT],
        key: str | None = None,
    ) -> NativeCallSpec[MethodT]:
        return cls(choices=tuple(choices), selector=selector, key=key)

    @property
    def is_constant(self) -> bool:
        return self.selector is None

    def select(self, value: Any) -> NativeChoice[MethodT]:
        """Return the declared choice for ``value``; reject undeclared natives."""
        if self.selector is None:
            return self.choices[0]
        choice = self.selector(value)
        if choice not in self.choices:
            raise BackendContractError(
                f"native call spec selected an undeclared native {choice!r}",
            )
        return choice


@dataclass(frozen=True, slots=True)
class CodecPayload:
    """Encoder output: params plus the typed request options a row may set.

    ``method`` and ``operation_variant`` are never here — only the selected
    :class:`NativeChoice` supplies them, so the native the ledger audits is the
    native that dispatches.
    """

    params: list[Any]
    source_path: str = "/"
    allow_null: bool = False
    raise_on_null_status: bool = False
    attempt_timeout: float | None = None


class Transport(Protocol[MethodT, RequestT]):
    """Backend-specific request assembly and the two transport verbs."""

    def assemble(
        self,
        definition: OperationDef[Any, Any],
        native: NativeChoice[MethodT],
        payload: CodecPayload,
        *,
        retry_flag: bool,
        deadline: RuntimeDeadline | None,
    ) -> RequestT: ...

    async def call(self, request: RequestT, *, deadline: RuntimeDeadline | None) -> Any: ...

    async def stream(self, request: RequestT, *, deadline: RuntimeDeadline | None) -> Any: ...


class ErrorTranslator(Protocol):
    """Shared native-to-neutral failure translation for one operation."""

    def __call__(self, operation: Operation, error: Exception) -> BackendError: ...


class ErrorMapper(Protocol[InputT_contra, MethodT]):
    """Row-level semantic translation, run only when a native call fails."""

    def __call__(
        self,
        value: InputT_contra,
        raw: Exception,
        native: NativeChoice[MethodT],
    ) -> BackendError | None: ...


class RowInvoker(Protocol):
    """Invocation-scoped access to exactly the natives one custom row declared."""

    async def call(
        self,
        spec_key: str,
        payload: CodecPayload,
        *,
        value: Any = None,
        deadline: RuntimeDeadline | None,
    ) -> Any: ...

    async def stream(
        self,
        spec_key: str,
        payload: CodecPayload,
        *,
        value: Any = None,
        deadline: RuntimeDeadline | None,
    ) -> Any: ...


class BoundHandler(Protocol[InputT_contra, OutputT_co]):
    """Shape of a resolved P1–P6 handler method: ``(value, *, deadline)``."""

    def __call__(
        self,
        value: InputT_contra,
        *,
        deadline: RuntimeDeadline | None,
    ) -> Awaitable[OutputT_co]: ...


class CustomHandler(Protocol[InputT_contra, OutputT_co]):
    """Shape of a custom row body: ``(value, deadline, invoke)``."""

    def __call__(
        self,
        value: InputT_contra,
        deadline: RuntimeDeadline | None,
        invoke: RowInvoker,
    ) -> Awaitable[OutputT_co]: ...


@dataclass(frozen=True, slots=True)
class ResolvedHandlerBinding(Generic[InputT, OutputT]):
    """A handler method resolved once at construction.

    Explicitly tagged so the construction audit can count it: P9.3 and P9.4
    replace these rows one operation at a time with :class:`CodecBinding` or
    :class:`CustomBinding` until the count reaches zero.
    """

    definition: OperationDef[InputT, OutputT]
    handler: BoundHandler[InputT, OutputT]


@dataclass(frozen=True, slots=True)
class CodecBinding(Generic[InputT, OutputT, MethodT]):
    """``encode → one native call → decode`` with the native fixed by the spec."""

    definition: OperationDef[InputT, OutputT]
    encode: Callable[[InputT], CodecPayload]
    decode: Callable[[InputT, Any], OutputT]
    native: NativeCallSpec[MethodT]
    deadline: DeadlineMode = DeadlineMode.INHERIT
    map_error: ErrorMapper[InputT, MethodT] | None = None
    forward_disable_internal_retries: bool = False


@dataclass(frozen=True, slots=True)
class CustomBinding(Generic[InputT, OutputT, MethodT]):
    """A justified multi-native row whose handler only sees a :class:`RowInvoker`."""

    definition: OperationDef[InputT, OutputT]
    handler: CustomHandler[InputT, OutputT]
    native: tuple[NativeCallSpec[MethodT], ...]
    justification: str
    category: CustomCategory
    deadline: DeadlineMode = DeadlineMode.INHERIT
    error_mode: ErrorMode = ErrorMode.TRANSLATE
    map_error: ErrorMapper[InputT, MethodT] | None = None

    def __post_init__(self) -> None:
        if self.category not in CUSTOM_CATEGORIES:
            raise ValueError(f"custom binding category must be one of {CUSTOM_CATEGORIES}")
        if not self.justification.strip():
            raise ValueError("custom bindings state a one-sentence justification")
        keys = [spec.key for spec in self.native]
        if any(key is None for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("custom binding natives carry unique, non-empty keys")

    def spec(self, key: str) -> NativeCallSpec[MethodT]:
        for candidate in self.native:
            if candidate.key == key:
                return candidate
        raise BackendContractError(
            f"{self.definition.key.value} declares no native spec {key!r}",
            operation=self.definition.key,
        )


Binding = (
    ResolvedHandlerBinding[Any, Any] | CodecBinding[Any, Any, Any] | CustomBinding[Any, Any, Any]
)


def bind(
    definition: OperationDef[InputT, OutputT],
    handler: BoundHandler[InputT, OutputT],
) -> ResolvedHandlerBinding[InputT, OutputT]:
    """Typed constructor: pairing a handler with a foreign input type is a mypy error."""
    return ResolvedHandlerBinding(definition=definition, handler=handler)


class BindingTable(Mapping[Operation, Binding]):
    """Immutable ``Operation -> row`` mapping with per-kind counts for ratchets."""

    __slots__ = ("_rows",)

    def __init__(self, rows: Mapping[Operation, Binding]) -> None:
        self._rows: Mapping[Operation, Binding] = MappingProxyType(dict(rows))

    def __getitem__(self, key: Operation) -> Binding:
        return self._rows[key]

    def __iter__(self) -> Iterator[Operation]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __repr__(self) -> str:
        return (
            f"BindingTable(rows={len(self)}, resolved_handlers={self.resolved_handler_count}, "
            f"codec={self.codec_count}, custom={self.custom_count})"
        )

    @property
    def resolved_handler_count(self) -> int:
        return sum(1 for row in self._rows.values() if isinstance(row, ResolvedHandlerBinding))

    @property
    def codec_count(self) -> int:
        return sum(1 for row in self._rows.values() if isinstance(row, CodecBinding))

    @property
    def custom_count(self) -> int:
        return sum(1 for row in self._rows.values() if isinstance(row, CustomBinding))

    def custom_count_by_category(self) -> Mapping[str, int]:
        counts = dict.fromkeys(CUSTOM_CATEGORIES, 0)
        for row in self._rows.values():
            if isinstance(row, CustomBinding):
                counts[row.category] += 1
        return MappingProxyType(counts)


def audit_bindings(table: Mapping[Operation, Binding], supported: frozenset[Operation]) -> None:
    """Reject a table whose keys differ from the executable dispositions."""
    keys = frozenset(table)
    problems: list[str] = []
    if missing := supported - keys:
        problems.append("supported operations without a row: " + _names(missing))
    if extra := keys - supported:
        problems.append("rows for operations not supported: " + _names(extra))
    problems.extend(
        f"row {key.value} binds definition {row.definition.key.value}"
        for key, row in table.items()
        if row.definition.key is not key
    )
    if problems:
        raise BindingAuditError("; ".join(problems))


def _names(operations: frozenset[Operation]) -> str:
    return ", ".join(sorted(operation.value for operation in operations))


def _tag_native(error: BaseException, native: NativeChoice[Any]) -> None:
    """Attach the selected native to a failure so multi-native rows can attribute it."""
    try:
        error.binding_native = native  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass
    original = getattr(error, "original", None)
    if isinstance(original, BaseException):
        try:
            original.binding_native = native  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass


class _RowScopedInvoker:
    """The only transport view a custom handler receives."""

    __slots__ = ("_errors", "_row", "_transport")

    def __init__(
        self,
        row: CustomBinding[Any, Any, Any],
        transport: Transport[Any, Any],
        errors: ErrorTranslator | None,
    ) -> None:
        self._row = row
        self._transport = transport
        self._errors = errors

    def _prepare(
        self,
        spec_key: str,
        payload: CodecPayload,
        value: Any,
        deadline: RuntimeDeadline | None,
    ) -> tuple[NativeChoice[Any], Any]:
        spec = self._row.spec(spec_key)
        choice = spec.select(value)
        request = self._transport.assemble(
            self._row.definition,
            choice,
            payload,
            retry_flag=False,
            deadline=deadline,
        )
        return choice, request

    async def call(
        self,
        spec_key: str,
        payload: CodecPayload,
        *,
        value: Any = None,
        deadline: RuntimeDeadline | None,
    ) -> Any:
        choice, request = self._prepare(spec_key, payload, value, deadline)
        try:
            return await self._transport.call(request, deadline=deadline)
        except BaseException as exc:
            _tag_native(exc, choice)
            raise

    async def stream(
        self,
        spec_key: str,
        payload: CodecPayload,
        *,
        value: Any = None,
        deadline: RuntimeDeadline | None,
    ) -> Any:
        choice, request = self._prepare(spec_key, payload, value, deadline)
        try:
            return await self._transport.stream(request, deadline=deadline)
        except BaseException as exc:
            _tag_native(exc, choice)
            raise


async def invoke_binding(
    table: Mapping[Operation, Binding],
    transport: Transport[Any, Any] | None,
    errors: ErrorTranslator | None,
    operation: OperationDef[InputT, OutputT],
    value: InputT,
    *,
    deadline: RuntimeDeadline | None,
) -> OutputT:
    """Dispatch one validated operation through its row — a function, never a base class.

    ``errors`` is the shared translator that row-level ``error_mode`` projection
    consumes; until the residual custom rows land, the backend head still owns
    translation and passes it through unchanged.
    """
    row = table.get(operation.key)
    if row is None:
        raise BackendContractError(
            f"{operation.key.value} has no binding row",
            operation=operation.key,
        )
    if row.definition != operation:
        raise BackendContractError(
            f"non-canonical definition supplied for {operation.key.value}",
            operation=operation.key,
        )
    if isinstance(row, ResolvedHandlerBinding):
        return await row.handler(value, deadline=deadline)
    if transport is None:
        raise BackendContractError(
            f"{operation.key.value} requires a transport for its binding row",
            operation=operation.key,
        )
    row_deadline = None if row.deadline is DeadlineMode.IGNORE else deadline
    if isinstance(row, CodecBinding):
        payload = row.encode(value)
        choice = row.native.select(value)
        request = transport.assemble(
            row.definition,
            choice,
            payload,
            retry_flag=row.forward_disable_internal_retries,
            deadline=row_deadline,
        )
        try:
            raw = await transport.call(request, deadline=row_deadline)
        except BaseException as exc:
            _tag_native(exc, choice)
            if row.map_error is not None and isinstance(exc, Exception):
                mapped = row.map_error(value, exc, choice)
                if mapped is not None:
                    raise mapped from exc
            raise
        return row.decode(value, raw)
    invoker = _RowScopedInvoker(row, transport, errors)
    return await row.handler(value, row_deadline, invoker)


__all__ = [
    "Binding",
    "BindingAuditError",
    "BindingTable",
    "BoundHandler",
    "CUSTOM_CATEGORIES",
    "CodecBinding",
    "CodecPayload",
    "CustomBinding",
    "CustomCategory",
    "CustomHandler",
    "DeadlineMode",
    "ErrorMapper",
    "ErrorMode",
    "ErrorTranslator",
    "NativeCallSpec",
    "NativeChoice",
    "OperationDisposition",
    "ResolvedHandlerBinding",
    "RowInvoker",
    "Transport",
    "audit_bindings",
    "bind",
    "invoke_binding",
]
