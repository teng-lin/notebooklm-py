from __future__ import annotations

from enum import Enum

import pytest

from notebooklm._android.errors import grpc_status, raise_grpc_status
from notebooklm.exceptions import (
    AuthError,
    ClientError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
)


class _Code(Enum):
    NOT_FOUND = (5, "not found")
    UNAUTHENTICATED = (16, "unauthenticated")


class _RawError(Exception):
    def __init__(self, code: _Code) -> None:
        super().__init__("raw ya29.must-not-escape")
        self._code = code

    def code(self):
        return self._code

    def details(self):
        return "aas_et/must-not-escape"


def test_status_extraction_ignores_raw_details() -> None:
    status = grpc_status(_RawError(_Code.NOT_FOUND))
    assert (status.name, status.code) == ("NOT_FOUND", 5)
    assert "ya29." not in repr(status)


@pytest.mark.parametrize(
    ("name", "code", "error_type"),
    [
        ("NOT_FOUND", 5, RPCError),
        ("UNAUTHENTICATED", 16, AuthError),
        ("PERMISSION_DENIED", 7, ClientError),
        ("RESOURCE_EXHAUSTED", 8, RateLimitError),
        ("DEADLINE_EXCEEDED", 4, RPCTimeoutError),
        ("UNAVAILABLE", 14, ServerError),
        ("INTERNAL", 13, ServerError),
        ("INVALID_ARGUMENT", 3, ClientError),
        ("FAILED_PRECONDITION", 9, ClientError),
        ("CANCELLED", 1, RPCError),
        ("ABORTED", 10, RPCError),
        ("ALREADY_EXISTS", 6, RPCError),
        ("OUT_OF_RANGE", 11, RPCError),
        ("UNIMPLEMENTED", 12, RPCError),
        ("UNKNOWN", 2, RPCError),
        ("DATA_LOSS", 15, RPCError),
    ],
)
def test_status_mapping(name, code, error_type) -> None:
    from notebooklm._android.errors import GrpcStatus

    with pytest.raises(error_type) as captured:
        raise_grpc_status(
            GrpcStatus(name, code),
            method="/package.Service/Method",
            timeout_seconds=12.0,
        )
    error = captured.value
    assert str(error) == f"Android RPC /package.Service/Method failed with {name}."
    assert error.method_id == "/package.Service/Method"
    if isinstance(error, RPCError):
        assert error.rpc_code == code
    else:
        assert isinstance(error, RPCTimeoutError)
        assert error.original_error is None
    assert error.__cause__ is None
    assert error.__context__ is None
