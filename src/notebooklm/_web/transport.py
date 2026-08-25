"""Web transport verbs behind the semantic binding table (P9.1).

``WebTransport`` owns the two ways the web backend reaches the wire — one
deadline-bound ``batchexecute`` call over :class:`WebExecutionRuntime` and the
chat-aware streamed POST over the shared :class:`RuntimeTransport`.  It is a
single-consumer collaborator constructed inside ``WebRpcBackend.__init__``;
lifecycle, provider ownership, and byte downloads stay on the shell.

The transport reads the runtime through the shell on every call rather than
copying it, so a test that rebinds ``backend._runtime`` after construction is
observed by dispatch.  Every native exception that escapes the runtime is
tagged ``dispatched = True`` (on the exception and on its ``.original``): the
runtime was entered, whether or not the POST was sent.  Nothing reads that
marker until the P9.2 commit-uncertainty predicate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .._backend import BackendContractError, BackendDeadlineExceededError
from .._binding import CodecPayload, NativeChoice, StreamPayload, StreamSpec
from .._deadline import RuntimeDeadline
from .._operations import Operation, OperationDef
from ..rpc import RPCMethod
from .chat_transport import chat_aware_authed_post

if TYPE_CHECKING:
    from .._request_types import BuildRequest
    from .._runtime.transport import RuntimeTransport
    from .runtime import WebExecutionRuntime


@dataclass(frozen=True, slots=True)
class WebRequest:
    """Every input of one deadline-bound native call, as a frozen value.

    ``operation`` attributes a pre-dispatch deadline failure; the remaining
    fields are forwarded to :meth:`WebExecutionRuntime.rpc_call` verbatim,
    including explicit ``False``/``None`` values.  ``_is_retry`` is not here:
    it is the runtime's own auth-refresh recursion flag, never a handler input.
    """

    operation: Operation
    method: RPCMethod
    params: list[Any] = field(repr=False)
    source_path: str = "/"
    operation_variant: str | None = None
    allow_null: bool = False
    raise_on_null_status: bool = False
    disable_internal_retries: bool = False
    outcome_unknown_on_expiry: bool = False
    attempt_timeout: float | None = None


@dataclass(frozen=True, slots=True)
class WebStreamRequest:
    """One chat-aware streamed POST: the request builder and its read budget."""

    operation: Operation
    build_request: BuildRequest = field(repr=False)
    parse_label: str
    read_timeout: float | None = None


def _mark_dispatched(error: BaseException) -> None:
    """Record that the runtime was entered before ``error`` escaped."""
    # ``TransportServerError`` carries ``.original``; the public ``NetworkError``
    # carries ``.original_error``.  Tag whichever wrapped native is present.
    for target in (
        error,
        getattr(error, "original", None),
        getattr(error, "original_error", None),
    ):
        if not isinstance(target, BaseException):
            continue
        try:
            target.dispatched = True  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass


class WebTransport:
    """The web backend's ``call``/``stream`` verbs over its runtime collaborators."""

    __slots__ = ("_chat_response_max_bytes", "_chat_transport", "_runtime_provider")

    def __init__(
        self,
        *,
        runtime_provider: Callable[[], WebExecutionRuntime],
        chat_transport: RuntimeTransport | None,
        chat_response_max_bytes: int | None,
    ) -> None:
        self._runtime_provider = runtime_provider
        self._chat_transport = chat_transport
        self._chat_response_max_bytes = chat_response_max_bytes

    def __repr__(self) -> str:
        return f"WebTransport(chat={self._chat_transport is not None})"

    def assemble(
        self,
        definition: OperationDef[Any, Any],
        native: NativeChoice[RPCMethod],
        payload: CodecPayload,
        *,
        retry_flag: bool,
        deadline: RuntimeDeadline | None,
        outcome_unknown_on_expiry: bool = False,
    ) -> WebRequest:
        """Build the wire request for one row; only the spec names the native."""
        del deadline
        return WebRequest(
            operation=definition.key,
            method=native.method,
            params=payload.params,
            source_path=payload.source_path,
            operation_variant=native.variant,
            allow_null=payload.allow_null,
            raise_on_null_status=payload.raise_on_null_status,
            disable_internal_retries=retry_flag,
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
            attempt_timeout=payload.attempt_timeout,
        )

    def assemble_stream(
        self,
        definition: OperationDef[Any, Any],
        spec: StreamSpec,
        payload: StreamPayload,
        *,
        deadline: RuntimeDeadline | None,
    ) -> WebStreamRequest:
        """Build the chat-aware streamed request for one custom row's stream spec."""
        del deadline
        return WebStreamRequest(
            operation=definition.key,
            build_request=payload.build_request,
            parse_label=spec.label,
            read_timeout=payload.attempt_timeout,
        )

    async def call(self, request: WebRequest, *, deadline: RuntimeDeadline | None) -> Any:
        """Dispatch one native call under the semantic deadline."""
        read_timeout: float | None = request.attempt_timeout
        if deadline is not None:
            remaining = deadline.remaining()
            if remaining <= 0.0:
                raise BackendDeadlineExceededError(
                    request.operation,
                    # No native call was dispatched in this phase. Uncertainty
                    # is therefore false unless the composite explicitly says
                    # an earlier phase may already have committed.
                    outcome_unknown=request.outcome_unknown_on_expiry,
                    diagnostics=MappingProxyType(
                        {
                            "timeout": deadline.timeout,
                            "remaining": remaining,
                            "timeout_seconds": deadline.timeout,
                            "method_id": request.method.value,
                        }
                    ),
                )
            read_timeout = remaining if read_timeout is None else min(read_timeout, remaining)
        try:
            return await self._runtime_provider().rpc_call(
                request.method,
                request.params,
                source_path=request.source_path,
                allow_null=request.allow_null,
                _is_retry=False,
                disable_internal_retries=request.disable_internal_retries,
                operation_variant=request.operation_variant,
                read_timeout=read_timeout,
                raise_on_null_status=request.raise_on_null_status,
                _retry_deadline=deadline,
            )
        except BaseException as exc:
            _mark_dispatched(exc)
            raise

    async def stream(
        self,
        request: WebRequest | WebStreamRequest,
        *,
        deadline: RuntimeDeadline | None,
    ) -> Any:
        """Perform one chat-aware streamed POST and return the buffered response."""
        if not isinstance(request, WebStreamRequest):
            raise BackendContractError(
                f"{request.operation.value} cannot stream a batchexecute request",
                operation=request.operation,
            )
        if self._chat_transport is None:
            raise BackendContractError(
                f"{request.operation.value} requires the composed chat transport",
                operation=request.operation,
            )
        try:
            return await chat_aware_authed_post(
                self._chat_transport,
                build_request=request.build_request,
                parse_label=request.parse_label,
                read_timeout=request.read_timeout,
                max_response_bytes=self._chat_response_max_bytes,
                disable_read_timeout_retries=True,
                retry_deadline=deadline,
            )
        except BaseException as exc:
            _mark_dispatched(exc)
            raise


__all__ = ["WebRequest", "WebStreamRequest", "WebTransport"]
