"""Sanitized Android transport error projection."""

from __future__ import annotations

import traceback
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import wraps
from typing import Any, NoReturn, ParamSpec, TypeVar

from ..exceptions import (
    AuthError,
    ClientError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
)


@dataclass(frozen=True)
class GrpcStatus:
    """A safe copy of a gRPC status without details or the raw exception."""

    name: str
    code: int


_STATUS_CODES = {
    "OK": 0,
    "CANCELLED": 1,
    "UNKNOWN": 2,
    "INVALID_ARGUMENT": 3,
    "DEADLINE_EXCEEDED": 4,
    "NOT_FOUND": 5,
    "ALREADY_EXISTS": 6,
    "PERMISSION_DENIED": 7,
    "RESOURCE_EXHAUSTED": 8,
    "FAILED_PRECONDITION": 9,
    "ABORTED": 10,
    "OUT_OF_RANGE": 11,
    "UNIMPLEMENTED": 12,
    "INTERNAL": 13,
    "UNAVAILABLE": 14,
    "DATA_LOSS": 15,
    "UNAUTHENTICATED": 16,
}
_STATUS_NAMES = {code: name for name, code in _STATUS_CODES.items()}

_P = ParamSpec("_P")
_T = TypeVar("_T")


def grpc_status(error: Exception) -> GrpcStatus:
    """Extract only a known status name/number from a gRPC-shaped error.

    ``AioRpcError.details()`` and the exception's string representation are
    intentionally never inspected. Unknown/future values collapse to
    ``UNKNOWN`` so dependency-controlled objects cannot enter public text.
    """

    code_method = getattr(error, "code", None)
    if not callable(code_method):
        return GrpcStatus("UNKNOWN", _STATUS_CODES["UNKNOWN"])
    try:
        raw_code = code_method()
        raw_name = getattr(raw_code, "name", None)
        raw_value = getattr(raw_code, "value", None)
    except BaseException:
        return GrpcStatus("UNKNOWN", _STATUS_CODES["UNKNOWN"])

    if isinstance(raw_name, str) and raw_name in _STATUS_CODES:
        return GrpcStatus(raw_name, _STATUS_CODES[raw_name])
    if isinstance(raw_value, tuple) and raw_value:
        first_value, *_remaining_values = raw_value
        raw_value = first_value
    if type(raw_value) is int and raw_value in _STATUS_NAMES:
        return GrpcStatus(_STATUS_NAMES[raw_value], raw_value)
    if type(raw_code) is int and raw_code in _STATUS_NAMES:
        return GrpcStatus(_STATUS_NAMES[raw_code], raw_code)
    return GrpcStatus("UNKNOWN", _STATUS_CODES["UNKNOWN"])


def is_grpc_status(error: BaseException, code: int) -> bool:
    """Return whether a mapped public RPC exception carries ``code``."""

    return isinstance(error, RPCError) and error.rpc_code == code


def sanitize_escaping_exception(error: BaseException) -> BaseException:
    """Detach completed inner frames and exception chains before publication."""

    captured = error.__traceback__
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    error.__suppress_context__ = False
    if captured is not None:
        traceback.clear_frames(captured)
    return error


def sanitize_async_boundary(
    function: Callable[_P, Coroutine[Any, Any, _T]],
) -> Callable[_P, Coroutine[Any, Any, _T]]:
    """Scrub failures after the decorated coroutine releases its arguments."""

    @wraps(function)
    async def sanitized(*args: _P.args, **kwargs: _P.kwargs) -> _T:
        try:
            return await function(*args, **kwargs)
        except BaseException as caught:
            failure = sanitize_escaping_exception(caught)
        del args, kwargs
        raise failure from None

    return sanitized


def raise_grpc_status(
    status: GrpcStatus,
    *,
    method: str,
    timeout_seconds: float | None,
) -> NoReturn:
    """Raise the existing public exception for one safe gRPC status copy."""

    message = f"Android RPC {method} failed with {status.name}."
    if status.name == "UNAUTHENTICATED":
        error: Exception = AuthError(message, method_id=method, rpc_code=status.code)
    elif status.name == "RESOURCE_EXHAUSTED":
        error = RateLimitError(message, method_id=method, rpc_code=status.code)
    elif status.name == "DEADLINE_EXCEEDED":
        error = RPCTimeoutError(
            message,
            timeout_seconds=timeout_seconds,
            method_id=method,
        )
    elif status.name in {"UNAVAILABLE", "INTERNAL"}:
        error = ServerError(message, method_id=method, rpc_code=status.code)
    elif status.name in {"INVALID_ARGUMENT", "FAILED_PRECONDITION", "PERMISSION_DENIED"}:
        error = ClientError(message, method_id=method, rpc_code=status.code)
    else:
        error = RPCError(message, method_id=method, rpc_code=status.code)
    raise error


def raise_deadline_exceeded(method: str, timeout_seconds: float | None) -> NoReturn:
    """Raise the sanitized aggregate-deadline result for ``method``."""

    raise_grpc_status(
        GrpcStatus("DEADLINE_EXCEEDED", _STATUS_CODES["DEADLINE_EXCEEDED"]),
        method=method,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "GrpcStatus",
    "grpc_status",
    "is_grpc_status",
    "raise_deadline_exceeded",
    "raise_grpc_status",
    "sanitize_async_boundary",
    "sanitize_escaping_exception",
]
