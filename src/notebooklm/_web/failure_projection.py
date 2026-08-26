"""Project public exception graphs into transport-neutral failure records.

The semantic backend uses this adapter when an operation must carry a public
library exception across the backend boundary.  It owns only the bounded,
serializable projection policy; wire execution and backend dispatch remain in
``backend.py``.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from .._backend import BackendContractError, BackendError
from .._operations import Operation
from .._semantic.records import SourceAddFailureKind, SourceAddFailureRecord
from .._transport_errors import (
    TransportAuthExpired,
    TransportRateLimited,
    TransportServerError,
)
from ..exceptions import (
    AuthError,
    ChatError,
    ChatResponseParseError,
    ClientError,
    DecodingError,
    IdempotencyVariantError,
    NetworkError,
    NonIdempotentRetryError,
    NotebookLMError,
    RateLimitError,
    RPCError,
    RPCResponseTooLargeError,
    RPCTimeoutError,
    ServerError,
    SourceAddError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
    UnknownRPCMethodError,
    ValidationError,
)

_CHAT_OPERATIONS = frozenset(
    {
        Operation.CHAT_ASK,
        Operation.CHAT_STREAM_ANSWER,
        Operation.CHAT_GET_CONVERSATION,
        Operation.CHAT_GET_HISTORY,
        Operation.CHAT_DELETE_HISTORY,
        Operation.CHAT_CONFIGURE,
        Operation.CHAT_SAVE_NOTE,
    }
)


def _capture_public_failure(
    exc: Exception,
    *,
    operation: Operation,
    _seen: frozenset[int] = frozenset(),
    scrub_request_urls: bool = False,
) -> SourceAddFailureRecord:
    """Capture the bounded, serializable public library-error graph."""
    if id(exc) in _seen or len(_seen) >= 8:
        raise BackendContractError(
            "public failure graph is cyclic or exceeds eight nodes",
            operation=operation,
        ) from exc
    seen = _seen | {id(exc)}

    kind_by_type: dict[type[BaseException], SourceAddFailureKind] = {
        SourceAddError: SourceAddFailureKind.SOURCE_ADD,
        SourceNotFoundError: SourceAddFailureKind.SOURCE_NOT_FOUND,
        ValidationError: SourceAddFailureKind.VALIDATION,
        NonIdempotentRetryError: SourceAddFailureKind.NON_IDEMPOTENT_RETRY,
        IdempotencyVariantError: SourceAddFailureKind.IDEMPOTENCY_VARIANT,
        SourceProcessingError: SourceAddFailureKind.SOURCE_PROCESSING,
        SourceTimeoutError: SourceAddFailureKind.SOURCE_TIMEOUT,
        AuthError: SourceAddFailureKind.AUTH,
        ChatError: SourceAddFailureKind.CHAT,
        ChatResponseParseError: SourceAddFailureKind.CHAT_RESPONSE_PARSE,
        ClientError: SourceAddFailureKind.CLIENT,
        DecodingError: SourceAddFailureKind.DECODING,
        NetworkError: SourceAddFailureKind.NETWORK,
        RateLimitError: SourceAddFailureKind.RATE_LIMIT,
        RPCResponseTooLargeError: SourceAddFailureKind.RESPONSE_TOO_LARGE,
        RPCError: SourceAddFailureKind.RPC,
        RPCTimeoutError: SourceAddFailureKind.RPC_TIMEOUT,
        ServerError: SourceAddFailureKind.SERVER,
        UnknownRPCMethodError: SourceAddFailureKind.UNKNOWN_RPC_METHOD,
        ConnectionError: SourceAddFailureKind.BUILTIN_CONNECTION,
        BrokenPipeError: SourceAddFailureKind.BUILTIN_BROKEN_PIPE,
        ConnectionAbortedError: SourceAddFailureKind.BUILTIN_CONNECTION_ABORTED,
        ConnectionRefusedError: SourceAddFailureKind.BUILTIN_CONNECTION_REFUSED,
        ConnectionResetError: SourceAddFailureKind.BUILTIN_CONNECTION_RESET,
        OSError: SourceAddFailureKind.BUILTIN_OS,
        IndexError: SourceAddFailureKind.BUILTIN_INDEX,
        KeyError: SourceAddFailureKind.BUILTIN_KEY,
        RuntimeError: SourceAddFailureKind.BUILTIN_RUNTIME,
        TimeoutError: SourceAddFailureKind.BUILTIN_TIMEOUT,
        TypeError: SourceAddFailureKind.BUILTIN_TYPE,
        ValueError: SourceAddFailureKind.BUILTIN_VALUE,
        httpx.HTTPStatusError: SourceAddFailureKind.HTTPX_STATUS,
        httpx.RequestError: SourceAddFailureKind.HTTPX_REQUEST,
        httpx.TransportError: SourceAddFailureKind.HTTPX_TRANSPORT,
        httpx.TimeoutException: SourceAddFailureKind.HTTPX_TIMEOUT,
        httpx.ConnectTimeout: SourceAddFailureKind.HTTPX_CONNECT_TIMEOUT,
        httpx.ReadTimeout: SourceAddFailureKind.HTTPX_READ_TIMEOUT,
        httpx.WriteTimeout: SourceAddFailureKind.HTTPX_WRITE_TIMEOUT,
        httpx.PoolTimeout: SourceAddFailureKind.HTTPX_POOL_TIMEOUT,
        httpx.NetworkError: SourceAddFailureKind.HTTPX_NETWORK,
        httpx.ConnectError: SourceAddFailureKind.HTTPX_CONNECT,
        httpx.ReadError: SourceAddFailureKind.HTTPX_READ,
        httpx.WriteError: SourceAddFailureKind.HTTPX_WRITE,
        httpx.CloseError: SourceAddFailureKind.HTTPX_CLOSE,
        httpx.ProxyError: SourceAddFailureKind.HTTPX_PROXY,
        httpx.ProtocolError: SourceAddFailureKind.HTTPX_PROTOCOL,
        httpx.LocalProtocolError: SourceAddFailureKind.HTTPX_LOCAL_PROTOCOL,
        httpx.RemoteProtocolError: SourceAddFailureKind.HTTPX_REMOTE_PROTOCOL,
        httpx.UnsupportedProtocol: SourceAddFailureKind.HTTPX_UNSUPPORTED_PROTOCOL,
        httpx.TooManyRedirects: SourceAddFailureKind.HTTPX_TOO_MANY_REDIRECTS,
        httpx.DecodingError: SourceAddFailureKind.HTTPX_DECODING,
        TransportAuthExpired: SourceAddFailureKind.TRANSPORT_AUTH_EXPIRED,
        TransportRateLimited: SourceAddFailureKind.TRANSPORT_RATE_LIMITED,
        TransportServerError: SourceAddFailureKind.TRANSPORT_SERVER,
    }
    kind = kind_by_type.get(type(exc))
    if kind is None:
        raise BackendContractError(
            f"unsupported public failure type {type(exc).__module__}.{type(exc).__qualname__}",
            operation=operation,
        ) from exc

    scalar_args = tuple(exc.args)
    if not all(isinstance(item, (str, int, float, bool, type(None))) for item in scalar_args):
        raise BackendContractError(
            "public failure args are not scalar",
            operation=operation,
        ) from exc

    # Preserve the public library graph. Builtin/httpx leaf internals can
    # contain arbitrary third-party exception objects; their exact reviewed
    # leaf type/data is retained below, but that unbounded internal graph is
    # intentionally not replayed.
    capture_links = isinstance(exc, NotebookLMError)
    explicit = exc.__cause__ if capture_links else None
    context = exc.__context__ if capture_links else None
    # A leaf invoked while a semantic workflow handles an earlier leaf inherits
    # that private BackendError as Python's implicit context. The workflow
    # carries the earlier public failure separately as bounded neutral evidence;
    # never descend back into a private backend record here.
    if isinstance(context, BackendError):
        context = None
    # ``WebExecutionRuntime`` raises the public RPC error explicitly from the
    # original httpx leaf while a private ``TransportServerError`` is the
    # suppressed implicit context. The neutral record preserves the explicit
    # public leaf and must not serialize that runtime implementation type.
    if (
        context is not None
        and exc.__suppress_context__
        and explicit is not None
        and type(context) not in kind_by_type
    ):
        context = None
    source_add_cause = exc.cause if isinstance(exc, SourceAddError) else None
    if source_add_cause is not None and explicit is not None and source_add_cause is not explicit:
        raise BackendContractError(
            "SourceAddError has different cause attribute and explicit cause",
            operation=operation,
        ) from exc
    cause = source_add_cause or explicit
    # Public translations retain the explicit HTTPX cause and suppress the
    # private transport wrapper stored as implicit context. Keep failing closed
    # for unsupported observable contexts.
    if (
        context is not None
        and (
            type(context) not in kind_by_type
            or (
                isinstance(
                    context,
                    (TransportAuthExpired, TransportRateLimited, TransportServerError),
                )
                and operation not in _CHAT_OPERATIONS
            )
        )
        and explicit is not None
        and type(explicit) in kind_by_type
        and exc.__suppress_context__
    ):
        context = None
    original_error = getattr(exc, "original_error", None)
    if isinstance(exc, (TransportAuthExpired, TransportRateLimited, TransportServerError)):
        original_error = exc.original
    if original_error is not None and not isinstance(original_error, Exception):
        raise BackendContractError(
            "public failure original_error is not an exception",
            operation=operation,
        ) from exc
    cause_is_original = cause is not None and cause is original_error
    cause_original_is_original_error = (
        cause is not None
        and original_error is not None
        and getattr(cause, "original", None) is original_error
    )
    context_is_cause = context is not None and context is cause
    context_is_original = context is not None and context is original_error

    found_ids = tuple(getattr(exc, "found_ids", ()) or ())
    if not all(isinstance(item, (str, int)) for item in found_ids):
        raise BackendContractError(
            "public failure found_ids are not strings or integers",
            operation=operation,
        ) from exc

    raw_response = getattr(exc, "raw_response", None)
    if raw_response is not None and not isinstance(raw_response, str):
        raw_response = repr(raw_response)
    data_at_failure = getattr(exc, "data_at_failure", None)
    if data_at_failure is not None and not isinstance(data_at_failure, str):
        data_at_failure = repr(data_at_failure)
    request: httpx.Request | None = None
    if isinstance(exc, (httpx.HTTPStatusError, httpx.RequestError)):
        try:
            request = exc.request
        except RuntimeError:
            pass

    request_url = str(request.url) if request is not None else None
    if request_url is not None and scrub_request_urls:
        parsed = urlsplit(request_url)
        hostname = parsed.hostname or ""
        if parsed.port is not None:
            hostname = f"{hostname}:{parsed.port}"
        request_url = urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))

    return SourceAddFailureRecord(
        kind=kind,
        message=str(exc.args[0]) if exc.args else "",
        args=scalar_args,
        url=(exc.url if isinstance(exc, SourceAddError) else None),
        unconfirmed=bool(getattr(exc, "unconfirmed", False)),
        source_id=getattr(exc, "source_id", None),
        stage=getattr(exc, "stage", None),
        method_id=getattr(exc, "method_id", None),
        raw_response=raw_response,
        rpc_code=getattr(exc, "rpc_code", None),
        found_ids=found_ids,
        recoverable=(getattr(exc, "recoverable", None) if isinstance(exc, AuthError) else None),
        retry_after=(
            getattr(exc, "retry_after", None)
            if isinstance(exc, (RateLimitError, TransportRateLimited))
            else None
        ),
        status_code=(
            getattr(exc, "status_code", None)
            if isinstance(exc, (ClientError, ServerError))
            else (
                exc.response.status_code
                if isinstance(exc, httpx.HTTPStatusError)
                else (exc.status_code if isinstance(exc, TransportServerError) else None)
            )
        ),
        timeout_seconds=(exc.timeout_seconds if isinstance(exc, RPCTimeoutError) else None),
        limit_bytes=(exc.limit_bytes if isinstance(exc, RPCResponseTooLargeError) else None),
        bytes_read=(exc.bytes_read if isinstance(exc, RPCResponseTooLargeError) else None),
        status=(exc.status if isinstance(exc, SourceProcessingError) else None),
        timeout=(exc.timeout if isinstance(exc, SourceTimeoutError) else None),
        last_status=(exc.last_status if isinstance(exc, SourceTimeoutError) else None),
        path=(exc.path if isinstance(exc, UnknownRPCMethodError) else None),
        source=(exc.source if isinstance(exc, UnknownRPCMethodError) else None),
        data_at_failure=data_at_failure,
        request_method=request.method if request is not None else None,
        request_url=request_url,
        original_error=(
            _capture_public_failure(
                original_error,
                operation=operation,
                _seen=seen,
                scrub_request_urls=scrub_request_urls,
            )
            if isinstance(original_error, Exception)
            else None
        ),
        cause=(
            _capture_public_failure(
                cause,
                operation=operation,
                _seen=seen,
                scrub_request_urls=scrub_request_urls,
            )
            if isinstance(cause, Exception) and not cause_is_original
            else None
        ),
        context=(
            _capture_public_failure(
                context,
                operation=operation,
                _seen=seen,
                scrub_request_urls=scrub_request_urls,
            )
            if isinstance(context, Exception) and not context_is_cause and not context_is_original
            else None
        ),
        cause_is_original=cause_is_original,
        cause_original_is_original_error=cause_original_is_original_error,
        context_is_cause=context_is_cause,
        context_is_original=context_is_original,
        explicit_cause=explicit is not None,
        suppress_context=exc.__suppress_context__,
    )


__all__ = ["_CHAT_OPERATIONS", "_capture_public_failure"]
