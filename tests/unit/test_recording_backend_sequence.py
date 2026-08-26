"""``RecordingBackend.set_sequence`` scripts per-operation responses in order."""

from __future__ import annotations

import pytest

from notebooklm._semantic.backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
)
from notebooklm._semantic.operations import Operation
from notebooklm._semantic.records import (
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NotebookGetInput,
    NotebookListInput,
    NotebookListResult,
    NotebookRecord,
)
from tests._fixtures.recording_backend import RecordingBackend, scripted_error


@pytest.mark.asyncio
async def test_sequence_is_consumed_in_order_and_records_every_invocation() -> None:
    backend = RecordingBackend()
    first = NotebookListResult(notebooks=())
    second = NotebookListResult(notebooks=(NotebookRecord(id="nb-1", title="t"),))
    failure = scripted_error(
        BackendErrorReason.SERVER,
        operation=Operation.NOTEBOOK_LIST,
        dispatched=True,
    )
    backend.set_sequence(NOTEBOOK_LIST_DEF, [first, failure, second])

    assert await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None) is first
    with pytest.raises(BackendError) as caught:
        await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)
    assert caught.value is failure
    assert caught.value.dispatched is True
    assert await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None) is second

    assert [call.operation for call in backend.invocations] == [Operation.NOTEBOOK_LIST] * 3

    with pytest.raises(BackendContractError, match="exhausted"):
        await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)


@pytest.mark.asyncio
async def test_sequence_gates_on_supports_like_every_other_registration() -> None:
    backend = RecordingBackend()
    backend.set_sequence(NOTEBOOK_LIST_DEF, [NotebookListResult(notebooks=())])

    assert backend.capabilities.supports(Operation.NOTEBOOK_LIST)
    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(NOTEBOOK_GET_DEF, NotebookGetInput("nb-1"), deadline=None)


def test_sequence_validates_result_types_and_error_attribution() -> None:
    backend = RecordingBackend()
    with pytest.raises(TypeError):
        backend.set_sequence(NOTEBOOK_LIST_DEF, ["not a result"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        backend.set_sequence(
            NOTEBOOK_LIST_DEF,
            [scripted_error(BackendErrorReason.SERVER, operation=Operation.NOTEBOOK_GET)],
        )


def test_scripted_error_scripts_dispatched_and_outcome_unknown() -> None:
    error = scripted_error(
        BackendErrorReason.NETWORK,
        operation=Operation.NOTEBOOK_CREATE,
        dispatched=True,
        outcome_unknown=True,
        diagnostics={"method_id": "m1"},
    )
    assert error.reason is BackendErrorReason.NETWORK
    assert error.dispatched is True
    assert error.outcome_unknown is True
    assert error.diagnostics is not None and error.diagnostics["method_id"] == "m1"

    timeout = scripted_error(
        BackendErrorReason.TIMEOUT, operation=Operation.NOTEBOOK_CREATE, dispatched=True
    )
    assert isinstance(timeout, BackendDeadlineExceededError)
    assert timeout.dispatched is True
    assert timeout.message == "notebook.create exceeded its deadline"


@pytest.mark.asyncio
async def test_set_result_and_set_error_replace_a_sequence() -> None:
    backend = RecordingBackend()
    backend.set_sequence(NOTEBOOK_LIST_DEF, [])
    result = NotebookListResult(notebooks=())
    backend.set_result(NOTEBOOK_LIST_DEF, result)

    assert await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None) is result
    assert await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None) is result

    backend.set_sequence(NOTEBOOK_LIST_DEF, [result])
    backend.set_error(NOTEBOOK_LIST_DEF, BackendError("boom", reason=BackendErrorReason.RPC))
    with pytest.raises(BackendError, match="boom"):
        await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)
