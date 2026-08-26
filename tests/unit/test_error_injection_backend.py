"""Synthetic failure tests use the semantic fake-backend seam."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._backend import BackendError, BackendErrorReason
from notebooklm._notebooks import NotebooksAPI
from notebooklm._operations import Operation
from notebooklm._records import (
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NotebookListInput,
    NotebookListResult,
)
from notebooklm.exceptions import AuthError, RateLimitError, ServerError
from tests._fixtures.recording_backend import BackendInvocation, RecordingBackend


def _notebooks_api(backend: RecordingBackend) -> tuple[NotebooksAPI, AsyncMock]:
    rpc_call = AsyncMock()
    return (
        NotebooksAPI(
            sources_api=MagicMock(),
            _backend=backend,
        ),
        rpc_call,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "reason", "diagnostics", "expected_type"),
    [
        (
            "429",
            BackendErrorReason.RATE_LIMIT,
            {"status_code": 429, "retry_after": 1},
            RateLimitError,
        ),
        ("5xx", BackendErrorReason.SERVER, {"status_code": 500}, ServerError),
        (
            "expired_csrf",
            BackendErrorReason.AUTH,
            {"status_code": 400, "recoverable": True},
            AuthError,
        ),
    ],
)
async def test_synthetic_error_modes_reconstruct_through_fake_backend(
    mode: str,
    reason: BackendErrorReason,
    diagnostics: dict[str, object],
    expected_type: type[Exception],
) -> None:
    """The former chain-only modes remain testable without runtime internals."""
    backend = RecordingBackend()
    backend.set_error(
        NOTEBOOK_LIST_DEF,
        BackendError(
            f"synthetic {mode}",
            operation=Operation.NOTEBOOK_LIST,
            reason=reason,
            diagnostics=diagnostics,
        ),
    )
    api, rpc_call = _notebooks_api(backend)

    with pytest.raises(expected_type, match=f"synthetic {mode}"):
        await api.list()

    assert backend.invocations == [
        BackendInvocation(Operation.NOTEBOOK_LIST, NotebookListInput(), None)
    ]
    rpc_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_registering_result_replaces_injected_error() -> None:
    backend = RecordingBackend()
    backend.set_error(
        NOTEBOOK_LIST_DEF,
        BackendError(
            "synthetic server failure",
            operation=Operation.NOTEBOOK_LIST,
            reason=BackendErrorReason.SERVER,
            diagnostics={"status_code": 500},
        ),
    )
    backend.set_result(NOTEBOOK_LIST_DEF, NotebookListResult(()))

    api, _ = _notebooks_api(backend)
    assert await api.list() == []


def test_fake_backend_rejects_error_for_different_operation() -> None:
    backend = RecordingBackend()

    with pytest.raises(ValueError, match="notebook.list cannot raise an error for notebook.get"):
        backend.set_error(
            NOTEBOOK_LIST_DEF,
            BackendError("wrong operation", operation=NOTEBOOK_GET_DEF.key),
        )
