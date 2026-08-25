"""Neutral binding vocabulary shared by every semantic backend.

A binding row ties one closed :class:`~notebooklm._operations.OperationDef` to
the way a backend executes it. Two row kinds exist:

* :class:`CodecBinding` — ``encode → one native call → decode``; the row's
  :class:`NativeCallSpec` is the sole authority for the native method.
* :class:`CustomBinding` — a handler that may sequence several declared
  natives through a row-scoped :class:`RowInvoker`; every such row states a
  one-sentence justification under a closed category so the count can ratchet.

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

from ._backend import BackendContractError, BackendDeadlineExceededError, BackendError
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


@dataclass(frozen=True, slots=True)
class StreamSpec:
    """One declared streamed verb of a custom row.

    A streamed request is not a native method: it never appears in the policy
    ledger's native set, so it lives beside ``native`` rather than inside it.
    ``key`` names the spec for the row-scoped invoker; ``label`` is the
    backend-side parse label the transport attaches to the streamed request.
    """

    key: str
    label: str

    def __post_init__(self) -> None:
        if not self.key or not self.label:
            raise ValueError("a stream spec carries a non-empty key and label")


@dataclass(frozen=True, slots=True)
class StreamPayload:
    """Encoder output for one streamed request: the request builder and its read budget.

    ``build_request`` is the backend-specific request builder the transport
    materialises against its live auth snapshot; ``attempt_timeout`` is the
    per-attempt read window the row computed under the caller's deadline.
    """

    build_request: Callable[..., Any]
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
        outcome_unknown_on_expiry: bool = False,
    ) -> RequestT: ...

    def assemble_stream(
        self,
        definition: OperationDef[Any, Any],
        spec: StreamSpec,
        payload: StreamPayload,
        *,
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
    """Invocation-scoped access to exactly the natives one custom row declared.

    ``disable_internal_retries`` and ``outcome_unknown_on_expiry`` are the two
    request options a composite sets per phase today (the write phase of a
    probe-then-create disables replay; a readback after a dispatched write marks
    a pre-dispatch expiry as commit-uncertain).  ``collaborator`` returns one of
    the row's declared, backend-supplied collaborators (never the transport or
    the runtime).
    """

    async def call(
        self,
        spec_key: str,
        payload: CodecPayload,
        *,
        value: Any = None,
        deadline: RuntimeDeadline | None,
        disable_internal_retries: bool = False,
        outcome_unknown_on_expiry: bool = False,
    ) -> Any: ...

    async def stream(
        self,
        spec_key: str,
        payload: StreamPayload,
        *,
        value: Any = None,
        deadline: RuntimeDeadline | None,
    ) -> Any: ...

    def collaborator(self, name: str) -> Any: ...


class CustomHandler(Protocol[InputT_contra, OutputT_co]):
    """Shape of a custom row body: ``(value, deadline, invoke)``."""

    def __call__(
        self,
        value: InputT_contra,
        deadline: RuntimeDeadline | None,
        invoke: RowInvoker,
    ) -> Awaitable[OutputT_co]: ...


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
    #: Closed, named set of backend collaborators the handler may reach through
    #: ``invoke.collaborator(name)``; audited at construction against what the
    #: backend provides, so a row can never reach an object it did not declare.
    collaborators: tuple[str, ...] = ()
    #: Declared streamed verbs, keyed like natives but never part of the policy
    #: ledger's native set; ``invoke.stream`` resolves only these.
    streams: tuple[StreamSpec, ...] = ()

    def __post_init__(self) -> None:
        if self.category not in CUSTOM_CATEGORIES:
            raise ValueError(f"custom binding category must be one of {CUSTOM_CATEGORIES}")
        if not self.justification.strip():
            raise ValueError("custom bindings state a one-sentence justification")
        keys = [spec.key for spec in self.native]
        if any(key is None for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("custom binding natives carry unique, non-empty keys")
        stream_keys = [spec.key for spec in self.streams]
        if len(set(stream_keys)) != len(stream_keys) or set(stream_keys) & set(keys):
            raise ValueError("custom binding stream keys are unique and distinct from natives")
        names = self.collaborators
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("custom binding collaborators are unique, non-empty names")

    def spec(self, key: str) -> NativeCallSpec[MethodT]:
        for candidate in self.native:
            if candidate.key == key:
                return candidate
        raise BackendContractError(
            f"{self.definition.key.value} declares no native spec {key!r}",
            operation=self.definition.key,
        )

    def stream_spec(self, key: str) -> StreamSpec:
        for candidate in self.streams:
            if candidate.key == key:
                return candidate
        raise BackendContractError(
            f"{self.definition.key.value} declares no stream spec {key!r}",
            operation=self.definition.key,
        )


Binding = CodecBinding[Any, Any, Any] | CustomBinding[Any, Any, Any]


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
            f"BindingTable(rows={len(self)}, codec={self.codec_count}, custom={self.custom_count})"
        )

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


def audit_bindings(
    table: Mapping[Operation, Binding],
    supported: frozenset[Operation],
    *,
    collaborators: frozenset[str] | None = None,
) -> None:
    """Reject a table whose keys differ from the executable dispositions.

    When ``collaborators`` names what the backend provides, every custom row's
    declared collaborator set must be a subset of it, so an undeclared or
    unprovided collaborator fails at construction, never mid-workflow.
    """
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
    if collaborators is not None:
        for key, row in table.items():
            if not isinstance(row, CustomBinding):
                continue
            if unprovided := set(row.collaborators) - collaborators:
                problems.append(
                    f"row {key.value} declares collaborators the backend does not provide: "
                    + ", ".join(sorted(unprovided))
                )
    if problems:
        raise BindingAuditError("; ".join(problems))


def _names(operations: frozenset[Operation]) -> str:
    return ", ".join(sorted(operation.value for operation in operations))


def _native_method_id(native: NativeChoice[Any]) -> Any:
    """The wire identity of a selected native, without naming a wire enum.

    ``MethodT`` is a backend's own method type; every backend models it as an
    enum whose ``value`` is the identity its failures report, so read that when
    it exists and fall back to the method itself when it does not.
    """
    method = native.method
    return getattr(method, "value", method)


def _raise_if_expired(
    operation: Operation,
    deadline: RuntimeDeadline | None,
    *,
    native: NativeChoice[Any] | None,
) -> None:
    """Fail one invocation before dispatch when its deadline is already spent.

    This is the port's single pre-dispatch expiry check.  Every row kind reaches
    it after its :class:`DeadlineMode` has been applied and, for a codec row,
    after the native has been selected — a keyed spec picks the native from the
    input, so the check must run late enough to name the native it blocked.
    A row that declares several natives, or that streams, resolves none here and
    its failure therefore carries no ``method_id``.
    """
    if deadline is None:
        return
    remaining = deadline.remaining()
    if remaining > 0.0:
        return
    # No native call was dispatched. Uncertainty stays false; a composite that
    # may already have committed an earlier phase says so per phase through
    # ``RowInvoker.call(outcome_unknown_on_expiry=True)``.
    diagnostics: dict[str, Any] = {
        "timeout": deadline.timeout,
        "remaining": remaining,
        "timeout_seconds": deadline.timeout,
    }
    if native is not None:
        diagnostics["method_id"] = _native_method_id(native)
    raise BackendDeadlineExceededError(operation, diagnostics=MappingProxyType(diagnostics))


def _tag_native(error: BaseException, native: NativeChoice[Any] | StreamSpec) -> None:
    """Attach the selected native (or stream spec) to a failure for row attribution."""
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

    __slots__ = ("_collaborators", "_errors", "_row", "_transport")

    def __init__(
        self,
        row: CustomBinding[Any, Any, Any],
        transport: Transport[Any, Any],
        errors: ErrorTranslator | None,
        collaborators: Mapping[str, Any] | None = None,
    ) -> None:
        self._row = row
        self._transport = transport
        self._errors = errors
        self._collaborators: Mapping[str, Any] = MappingProxyType(dict(collaborators or {}))

    def collaborator(self, name: str) -> Any:
        """Return one declared, backend-supplied collaborator; reject the rest."""
        if name not in self._row.collaborators:
            raise BackendContractError(
                f"{self._row.definition.key.value} declares no collaborator {name!r}",
                operation=self._row.definition.key,
            )
        if name not in self._collaborators:
            raise BackendContractError(
                f"{self._row.definition.key.value} collaborator {name!r} was not provided",
                operation=self._row.definition.key,
            )
        return self._collaborators[name]

    def _prepare(
        self,
        spec_key: str,
        payload: CodecPayload,
        value: Any,
        deadline: RuntimeDeadline | None,
        *,
        disable_internal_retries: bool,
        outcome_unknown_on_expiry: bool,
    ) -> tuple[NativeChoice[Any], Any]:
        spec = self._row.spec(spec_key)
        choice = spec.select(value)
        request = self._transport.assemble(
            self._row.definition,
            choice,
            payload,
            retry_flag=disable_internal_retries,
            deadline=deadline,
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
        )
        return choice, request

    def _failed(
        self, exc: BaseException, value: Any, choice: NativeChoice[Any] | StreamSpec
    ) -> None:
        """Tag the failure with its native and apply the row's semantic mapper."""
        _tag_native(exc, choice)
        if self._row.map_error is not None and isinstance(exc, Exception):
            mapped = self._row.map_error(value, exc, choice)  # type: ignore[arg-type]
            if mapped is not None:
                raise mapped from exc

    async def call(
        self,
        spec_key: str,
        payload: CodecPayload,
        *,
        value: Any = None,
        deadline: RuntimeDeadline | None,
        disable_internal_retries: bool = False,
        outcome_unknown_on_expiry: bool = False,
    ) -> Any:
        choice, request = self._prepare(
            spec_key,
            payload,
            value,
            deadline,
            disable_internal_retries=disable_internal_retries,
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
        )
        try:
            return await self._transport.call(request, deadline=deadline)
        except BaseException as exc:
            self._failed(exc, value, choice)
            raise

    async def stream(
        self,
        spec_key: str,
        payload: StreamPayload,
        *,
        value: Any = None,
        deadline: RuntimeDeadline | None,
    ) -> Any:
        """Perform one declared streamed verb; failures are tagged with its spec."""
        spec = self._row.stream_spec(spec_key)
        request = self._transport.assemble_stream(
            self._row.definition,
            spec,
            payload,
            deadline=deadline,
        )
        try:
            return await self._transport.stream(request, deadline=deadline)
        except BaseException as exc:
            self._failed(exc, value, spec)
            raise


def row_invoker(
    table: Mapping[Operation, Binding],
    transport: Transport[Any, Any],
    errors: ErrorTranslator | None,
    operation: Operation,
    *,
    collaborators: Mapping[str, Any] | None = None,
) -> RowInvoker:
    """Return the row-scoped invoker of one custom row for use outside ``invoke``.

    A backend uses this when a collaborator it owns (the upload pipeline's
    default callbacks) must execute under a row's declared natives and failure
    tagging even when the row itself is not being invoked; the row's own
    invocations still receive their own, invocation-scoped invoker.
    """
    row = table.get(operation)
    if not isinstance(row, CustomBinding):
        raise BackendContractError(
            f"{operation.value} has no custom binding row to scope an invoker to",
            operation=operation,
        )
    return _RowScopedInvoker(row, transport, errors, collaborators)


async def invoke_binding(
    table: Mapping[Operation, Binding],
    transport: Transport[Any, Any] | None,
    errors: ErrorTranslator | None,
    operation: OperationDef[InputT, OutputT],
    value: InputT,
    *,
    deadline: RuntimeDeadline | None,
    collaborators: Mapping[str, Any] | None = None,
) -> OutputT:
    """Dispatch one validated operation through its row — a function, never a base class.

    ``errors`` is the shared translator that row-level ``error_mode`` projection
    consumes (the backend head applies the projection); ``collaborators`` is the
    backend-supplied, named set a custom row may reach through its invoker.

    This is also where an already-spent deadline fails: one check, applied to
    every row kind after its ``DeadlineMode`` and native selection, so a backend
    head never needs a second one.
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
    if transport is None:
        raise BackendContractError(
            f"{operation.key.value} requires a transport for its binding row",
            operation=operation.key,
        )
    row_deadline = None if row.deadline is DeadlineMode.IGNORE else deadline
    if isinstance(row, CodecBinding):
        payload = row.encode(value)
        choice = row.native.select(value)
        try:
            # Inside the row's failure handling so an expiry is attributed and
            # mapped exactly like any other failure of this native.
            _raise_if_expired(operation.key, row_deadline, native=choice)
            request = transport.assemble(
                row.definition,
                choice,
                payload,
                retry_flag=row.forward_disable_internal_retries,
                deadline=row_deadline,
            )
            raw = await transport.call(request, deadline=row_deadline)
        except BaseException as exc:
            _tag_native(exc, choice)
            if row.map_error is not None and isinstance(exc, Exception):
                mapped = row.map_error(value, exc, choice)
                if mapped is not None:
                    raise mapped from exc
            raise
        return row.decode(value, raw)
    # A custom row resolves no single native here: its handler chooses per phase.
    _raise_if_expired(operation.key, row_deadline, native=None)
    invoker = _RowScopedInvoker(row, transport, errors, collaborators)
    return await row.handler(value, row_deadline, invoker)


__all__ = [
    "Binding",
    "BindingAuditError",
    "BindingTable",
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
    "RowInvoker",
    "StreamPayload",
    "StreamSpec",
    "Transport",
    "audit_bindings",
    "invoke_binding",
    "row_invoker",
]
