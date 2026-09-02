"""Status extraction from gRPC-shaped errors.

``grpc_status`` is the only thing standing between a dependency-controlled
exception object and the public error text: it must read a *known* status name
or number and nothing else. Every rejection below collapses to ``UNKNOWN``
rather than letting an arbitrary value through.
"""

from __future__ import annotations

from typing import Any

import pytest

from notebooklm._android.errors import (
    GrpcStatus,
    grpc_status,
    is_grpc_status,
)
from notebooklm.exceptions import AuthError, RPCError


class _Code:
    """Stands in for ``grpc.StatusCode``, whose members carry name and value."""

    def __init__(self, name: Any = None, value: Any = None) -> None:
        if name is not None:
            self.name = name
        if value is not None:
            self.value = value


class _Error(Exception):
    def __init__(self, code: Any, *, raising: bool = False) -> None:
        super().__init__("wire failure with secret ya29.token")
        self._code = code
        self._raising = raising

    def code(self) -> Any:
        if self._raising:
            raise RuntimeError("code() blew up")
        return self._code


def test_a_known_status_name_is_admitted_with_its_canonical_code() -> None:
    assert grpc_status(_Error(_Code(name="NOT_FOUND"))) == GrpcStatus("NOT_FOUND", 5)


def test_the_name_wins_over_a_disagreeing_value() -> None:
    """The canonical table decides the code, not the object's own number."""
    status = grpc_status(_Error(_Code(name="UNAVAILABLE", value=999)))

    assert status == GrpcStatus("UNAVAILABLE", 14)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param(_Code(value=(5, "not found")), GrpcStatus("NOT_FOUND", 5), id="tuple-value"),
        pytest.param(_Code(value=16), GrpcStatus("UNAUTHENTICATED", 16), id="integer-value"),
        pytest.param(_Code(value=0), GrpcStatus("OK", 0), id="zero-is-a-real-code"),
    ],
)
def test_a_status_without_a_usable_name_falls_back_to_its_value(
    code: Any, expected: GrpcStatus
) -> None:
    assert grpc_status(_Error(code)) == expected


def test_a_bare_integer_code_is_mapped() -> None:
    """Some transports return the raw number rather than an enum member."""
    assert grpc_status(_Error(8)) == GrpcStatus("RESOURCE_EXHAUSTED", 8)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(Exception("no code attribute at all"), id="not-grpc-shaped"),
        pytest.param(_Error(_Code(name="FUTURE_STATUS")), id="unknown-name"),
        pytest.param(_Error(_Code(name=7)), id="non-string-name"),
        pytest.param(_Error(_Code(value=999)), id="out-of-range-value"),
        pytest.param(_Error(_Code(value=())), id="empty-tuple-value"),
        pytest.param(_Error(_Code(value="5")), id="stringly-typed-value"),
        pytest.param(_Error(_Code(value=True)), id="bool-is-not-an-int-code"),
        pytest.param(_Error(_Code()), id="neither-name-nor-value"),
        pytest.param(_Error(None), id="code-returns-none"),
        pytest.param(_Error("5"), id="code-returns-a-string"),
        pytest.param(_Error(None, raising=True), id="code-raises"),
    ],
)
def test_anything_unrecognised_collapses_to_unknown(error: Exception) -> None:
    status = grpc_status(error)

    assert status == GrpcStatus("UNKNOWN", 2)


def test_a_non_callable_code_attribute_is_not_invoked() -> None:
    error = Exception("plain")
    error.code = 5  # type: ignore[attr-defined]

    assert grpc_status(error) == GrpcStatus("UNKNOWN", 2)


def test_the_projection_never_carries_the_original_message() -> None:
    """The wire error may embed a credential; only name and number survive."""
    status = grpc_status(_Error(_Code(name="INTERNAL")))

    assert "ya29" not in repr(status)


# ---------------------------------------------------------------------------
# is_grpc_status
# ---------------------------------------------------------------------------


def test_a_mapped_rpc_error_reports_its_code() -> None:
    error = AuthError("denied", method_id="m", rpc_code=16)

    assert is_grpc_status(error, 16) is True
    assert is_grpc_status(error, 5) is False


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ValueError("not an RPC error"), id="unrelated-exception"),
        pytest.param(RPCError("no code", method_id="m"), id="rpc-error-without-a-code"),
    ],
)
def test_a_non_matching_error_is_not_a_grpc_status(error: BaseException) -> None:
    assert is_grpc_status(error, 16) is False
