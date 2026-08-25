"""Migration sentinels for the semantic notebook mutation slice.

P2 may replace the execution authority, but create reconciliation, mutation
payloads, read-back behavior, and public signatures remain facade contracts.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from notebooklm._notebook_payloads import (
    build_create_notebook_params,
    build_get_notebook_params,
    build_update_notebook_params,
)
from notebooklm._notebooks import NotebooksAPI
from notebooklm._web.backend import WebRpcBackend
from notebooklm.exceptions import (
    ClientError,
    DecodingError,
    NetworkError,
    NotebookNotFoundError,
    RPCError,
    ServerError,
    ValidationError,
)
from notebooklm.rpc import RPCMethod


def _api(rpc_call: AsyncMock) -> NotebooksAPI:
    executor = MagicMock(rpc_call=rpc_call)
    backend = WebRpcBackend(executor)
    return NotebooksAPI(sources_api=MagicMock(), _backend=backend)


def test_notebook_mutation_public_signatures_are_frozen() -> None:
    assert list(inspect.signature(NotebooksAPI.create).parameters) == ["self", "title"]
    assert list(inspect.signature(NotebooksAPI.rename).parameters) == [
        "self",
        "notebook_id",
        "new_title",
    ]
    update = inspect.signature(NotebooksAPI.update).parameters
    assert list(update) == ["self", "notebook_id", "title", "emoji"]
    assert update["notebook_id"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert update["title"].kind is inspect.Parameter.KEYWORD_ONLY
    assert update["title"].default is None
    assert update["emoji"].kind is inspect.Parameter.KEYWORD_ONLY
    assert update["emoji"].default is None
    assert list(inspect.signature(NotebooksAPI.delete).parameters) == ["self", "notebook_id"]


@pytest.mark.asyncio
async def test_create_pins_baseline_payload_projection_and_retry_ownership() -> None:
    rpc_call = AsyncMock(
        side_effect=[
            [],
            [
                "Daily News",
                None,
                "nb-new",
                None,
                None,
                [None, False, None, None, None, [1704067200, 0]],
            ],
        ]
    )
    api = _api(rpc_call)

    notebook = await api.create("Daily News")

    assert (notebook.id, notebook.title) == ("nb-new", "Daily News")
    assert [item.args[0] for item in rpc_call.await_args_list] == [
        RPCMethod.LIST_NOTEBOOKS,
        RPCMethod.CREATE_NOTEBOOK,
    ]
    assert rpc_call.await_args_list[1].args[1] == build_create_notebook_params("Daily News")
    assert rpc_call.await_args_list[1].kwargs["disable_internal_retries"] is True


@pytest.mark.asyncio
async def test_create_transport_failure_adopts_one_new_baseline_diff_without_repost() -> None:
    old_row = ["Daily News", [], "nb-old"]
    landed_row = ["Daily News", [], "nb-landed"]
    rpc_call = AsyncMock(
        side_effect=[
            [[old_row]],
            ServerError("bad gateway", status_code=502),
            [[old_row, landed_row]],
        ]
    )
    api = _api(rpc_call)

    recovered = await api.create("Daily News")

    assert (recovered.id, recovered.title) == ("nb-landed", "Daily News")
    assert [item.args[0] for item in rpc_call.await_args_list] == [
        RPCMethod.LIST_NOTEBOOKS,
        RPCMethod.CREATE_NOTEBOOK,
        RPCMethod.LIST_NOTEBOOKS,
    ]
    assert sum(item.args[0] is RPCMethod.CREATE_NOTEBOOK for item in rpc_call.await_args_list) == 1


@pytest.mark.asyncio
async def test_create_probe_failure_preserves_bounded_public_error_graph() -> None:
    create_error = ServerError(
        "create response lost",
        status_code=502,
        method_id=RPCMethod.CREATE_NOTEBOOK.value,
    )
    probe_error = NetworkError(
        "probe unavailable",
        method_id=RPCMethod.LIST_NOTEBOOKS.value,
    )
    rpc_call = AsyncMock(side_effect=[[], create_error, probe_error])
    api = _api(rpc_call)

    with pytest.raises(NetworkError) as caught:
        await api.create("Daily News")

    assert caught.value is not probe_error
    assert caught.value.method_id == RPCMethod.LIST_NOTEBOOKS.value
    assert getattr(caught.value, "unconfirmed", False) is True
    assert type(caught.value.__context__) is ServerError
    assert caught.value.__context__.args == create_error.args
    assert caught.value.__context__.status_code == 502
    assert caught.value.__context__.method_id == RPCMethod.CREATE_NOTEBOOK.value
    assert sum(item.args[0] is RPCMethod.CREATE_NOTEBOOK for item in rpc_call.await_args_list) == 1


@pytest.mark.asyncio
async def test_create_wrapped_probe_failure_preserves_cause_and_create_context() -> None:
    create_error = ServerError("create response lost", status_code=502)
    probe_error = DecodingError("probe payload drift", method_id=RPCMethod.LIST_NOTEBOOKS.value)
    api = _api(AsyncMock(side_effect=[[], create_error, probe_error]))

    with pytest.raises(RPCError) as caught:
        await api.create("Daily News")

    assert getattr(caught.value, "unconfirmed", False) is True
    assert type(caught.value.__cause__) is DecodingError
    assert caught.value.__context__ is caught.value.__cause__
    assert type(caught.value.__cause__.__context__) is ServerError
    assert caught.value.__cause__.__context__.args == create_error.args


@pytest.mark.asyncio
async def test_title_update_pins_mutation_then_get_readback() -> None:
    rpc_call = AsyncMock(
        side_effect=[
            None,
            [["Renamed", [], "nb-1"]],
        ]
    )
    api = _api(rpc_call)

    notebook = await api.rename("nb-1", "Renamed")

    assert (notebook.id, notebook.title) == ("nb-1", "Renamed")
    assert rpc_call.await_args_list == [
        call(
            RPCMethod.RENAME_NOTEBOOK,
            build_update_notebook_params("nb-1", title="Renamed"),
            source_path="/",
            allow_null=True,
            _is_retry=False,
            disable_internal_retries=False,
            operation_variant=None,
            read_timeout=None,
            raise_on_null_status=False,
            _retry_deadline=None,
        ),
        call(
            RPCMethod.GET_NOTEBOOK,
            build_get_notebook_params("nb-1"),
            source_path="/notebook/nb-1",
            allow_null=False,
            _is_retry=False,
            disable_internal_retries=False,
            operation_variant=None,
            read_timeout=None,
            raise_on_null_status=False,
            _retry_deadline=None,
        ),
    ]


@pytest.mark.asyncio
async def test_update_not_found_preserves_reconstructed_public_cause_graph() -> None:
    original = ClientError(
        "not found",
        status_code=404,
        method_id=RPCMethod.GET_NOTEBOOK.value,
        raw_response="scrubbed response",
        rpc_code=5,
    )
    api = _api(AsyncMock(side_effect=[None, original]))

    with pytest.raises(NotebookNotFoundError) as caught:
        await api.update("nb-missing", title="Renamed")

    assert isinstance(caught.value.__cause__, ClientError)
    assert caught.value.__context__ is caught.value.__cause__
    assert caught.value.__cause__.status_code == 404
    assert caught.value.__cause__.rpc_code == 5
    assert caught.value.__cause__.raw_response == "scrubbed response"
    assert caught.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_update_rejects_empty_change_and_delete_stays_single_id_set_operation() -> None:
    rpc_call = AsyncMock(return_value=None)
    api = _api(rpc_call)

    with pytest.raises(ValidationError, match="At least one"):
        await api.update("nb-1")
    rpc_call.assert_not_awaited()

    assert await api.delete("nb-1") is None
    assert rpc_call.await_args_list[-1].args == (
        RPCMethod.DELETE_NOTEBOOK,
        [["nb-1"], [2]],
    )
