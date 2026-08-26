"""Migration sentinels for the first semantic notebook/source read slice.

These tests intentionally characterize the legacy facade contract before P1's
backend types are available.  P2 may replace the execution authority, but it
must preserve these request, projection, filtering, and miss semantics.
"""

from __future__ import annotations

import inspect
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._notebook_payloads import build_get_notebook_params
from notebooklm._notebooks import NotebooksAPI
from notebooklm._semantic.backend import BackendError, BackendErrorReason
from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import (
    AuthError,
    ClientError,
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCResponseTooLargeError,
    RPCTimeoutError,
    ServerError,
    SourceNotFoundError,
    UnknownRPCMethodError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import SourceStatus
from notebooklm.types import Source, SourceType
from tests._fixtures.web_backend import build_web_backend
from tests._helpers.signature_inspection import signature_parameters


def _source_entry(
    source_id: str,
    *,
    title: str,
    type_code: int = 3,
    status: SourceStatus = SourceStatus.READY,
) -> list[object]:
    return [
        [source_id],
        title,
        [None, 10, [1704067200, 0], None, type_code],
        [None, status],
    ]


def _sources_api(result: object) -> tuple[SourcesAPI, AsyncMock]:
    rpc_call = AsyncMock(return_value=result)
    rpc = MagicMock(rpc_call=rpc_call)
    return SourcesAPI(
        rpc,
        uploader=MagicMock(),
        _backend=build_web_backend(rpc),
    ), rpc_call


def test_read_slice_public_signatures_are_frozen() -> None:
    """P2 keeps positional IDs and the source-list keyword-only controls."""
    assert list(signature_parameters(NotebooksAPI.list)) == ["self"]
    assert list(signature_parameters(NotebooksAPI.get)) == ["self", "notebook_id"]
    assert list(signature_parameters(NotebooksAPI.get_or_none)) == [
        "self",
        "notebook_id",
    ]

    source_list = signature_parameters(SourcesAPI.list)
    assert list(source_list) == ["self", "notebook_id", "strict", "statuses", "types"]
    assert source_list["notebook_id"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert source_list["strict"].kind is inspect.Parameter.KEYWORD_ONLY
    assert source_list["strict"].default is False
    assert source_list["statuses"].default is None
    assert source_list["types"].default is None
    assert list(signature_parameters(SourcesAPI.get)) == [
        "self",
        "notebook_id",
        "source_id",
    ]
    assert list(signature_parameters(SourcesAPI.get_or_none)) == [
        "self",
        "notebook_id",
        "source_id",
    ]


@pytest.mark.asyncio
async def test_notebook_reads_pin_requests_projection_and_backend_order() -> None:
    rpc_call = AsyncMock(
        side_effect=[
            [
                [
                    [
                        "Second",
                        [],
                        "nb-2",
                    ],
                    ["First", [], "nb-1"],
                ]
            ],
            [["Details", [], "nb-1"]],
        ]
    )
    rpc = MagicMock(rpc_call=rpc_call)
    api = NotebooksAPI(
        sources_api=MagicMock(),
        _backend=build_web_backend(rpc),
    )

    listed = await api.list()
    fetched = await api.get("nb-1")

    assert [(notebook.id, notebook.title) for notebook in listed] == [
        ("nb-2", "Second"),
        ("nb-1", "First"),
    ]
    assert (fetched.id, fetched.title) == ("nb-1", "Details")
    assert rpc_call.await_args_list[0].args == (
        RPCMethod.LIST_NOTEBOOKS,
        [None, 1, None, [2]],
    )
    common_backend_kwargs = {
        "allow_null": False,
        "_is_retry": False,
        "disable_internal_retries": False,
        "operation_variant": None,
        "read_timeout": None,
        "raise_on_null_status": False,
        "_retry_deadline": None,
    }
    assert rpc_call.await_args_list[0].kwargs == {
        "source_path": "/",
        **common_backend_kwargs,
    }
    assert rpc_call.await_args_list[1].args == (
        RPCMethod.GET_NOTEBOOK,
        build_get_notebook_params("nb-1"),
    )
    assert rpc_call.await_args_list[1].kwargs == {
        "source_path": "/notebook/nb-1",
        **common_backend_kwargs,
    }


@pytest.mark.asyncio
async def test_source_list_pins_request_normalization_filters_and_strict_count() -> None:
    duplicate = _source_entry("src-pdf", title="PDF", status=SourceStatus.READY)
    payload = [
        [
            "Notebook",
            [
                duplicate,
                duplicate,
                _source_entry(
                    "src-web",
                    title="Web",
                    type_code=5,
                    status=SourceStatus.ERROR,
                ),
            ],
            "nb-1",
        ]
    ]
    api, rpc_call = _sources_api(payload)

    sources = await api.list(
        "nb-1",
        strict=True,
        statuses={SourceStatus.READY, SourceStatus.ERROR},
        types={SourceType.PDF, SourceType.WEB_PAGE},
    )

    assert [(source.id, source.title, source.kind, source.status) for source in sources] == [
        ("src-pdf", "PDF", SourceType.PDF, SourceStatus.READY),
        ("src-web", "Web", SourceType.WEB_PAGE, SourceStatus.ERROR),
    ]
    assert rpc_call.await_args.args == (
        RPCMethod.GET_NOTEBOOK,
        build_get_notebook_params("nb-1"),
    )
    assert rpc_call.await_args.kwargs["source_path"] == "/notebook/nb-1"


@pytest.mark.asyncio
async def test_source_get_preserves_late_bound_list_and_miss_contracts() -> None:
    api, rpc_call = _sources_api(None)
    expected = Source(id="src-2", title="Two")
    api.list = AsyncMock(return_value=[Source(id="src-1"), expected])  # type: ignore[method-assign]

    assert await api.get_or_none("nb-1", "src-2") is expected
    assert await api.get_or_none("nb-1", "missing") is None
    with pytest.raises(SourceNotFoundError) as exc_info:
        await api.get("nb-1", "missing")

    assert exc_info.value.source_id == "missing"
    assert api.list.await_args_list == [
        (("nb-1",), {}),
        (("nb-1",), {}),
        (("nb-1",), {}),
    ]
    rpc_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_get_preserves_class_level_late_bound_list_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, rpc_call = _sources_api(None)
    expected = Source(id="src-2", title="Two")
    calls: list[str] = []

    async def replacement_list(
        self: SourcesAPI, notebook_id: str, **_kwargs: object
    ) -> list[Source]:
        assert self is api
        calls.append(notebook_id)
        return [Source(id="src-1"), expected]

    monkeypatch.setattr(SourcesAPI, "list", replacement_list)

    assert await api.get_or_none("nb-1", "src-2") is expected
    assert await api.get_or_none("nb-1", "missing") is None
    with pytest.raises(SourceNotFoundError) as exc_info:
        await api.get("nb-1", "missing")

    assert exc_info.value.source_id == "missing"
    assert calls == ["nb-1", "nb-1", "nb-1"]
    rpc_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_get_default_path_uses_backend_and_preserves_genuine_miss() -> None:
    payload = [["Notebook", [_source_entry("src-1", title="One")], "nb-1"]]
    rpc_call = AsyncMock(side_effect=[payload, payload, payload])
    rpc = MagicMock(rpc_call=rpc_call)
    api = SourcesAPI(
        rpc,
        uploader=MagicMock(),
        _backend=build_web_backend(rpc),
    )

    found = await api.get_or_none("nb-1", "src-1")
    assert found is not None and found.id == "src-1"
    assert await api.get_or_none("nb-1", "missing") is None
    with pytest.raises(SourceNotFoundError) as exc_info:
        await api.get("nb-1", "missing")

    assert exc_info.value.source_id == "missing"
    assert [call.args[:2] for call in rpc_call.await_args_list] == [
        (RPCMethod.GET_NOTEBOOK, build_get_notebook_params("nb-1")),
        (RPCMethod.GET_NOTEBOOK, build_get_notebook_params("nb-1")),
        (RPCMethod.GET_NOTEBOOK, build_get_notebook_params("nb-1")),
    ]


@pytest.mark.parametrize(
    ("reason", "expected_type", "expected_attributes"),
    [
        (BackendErrorReason.AUTH, AuthError, {"recoverable": True}),
        (BackendErrorReason.CLIENT, ClientError, {"status_code": 503}),
        (BackendErrorReason.DECODING, DecodingError, {}),
        (BackendErrorReason.NETWORK, NetworkError, {}),
        (BackendErrorReason.RATE_LIMIT, RateLimitError, {"retry_after": 7}),
        (
            BackendErrorReason.RESPONSE_TOO_LARGE,
            RPCResponseTooLargeError,
            {"limit_bytes": 100, "bytes_read": 101},
        ),
        (BackendErrorReason.RPC, RPCError, {}),
        (BackendErrorReason.SERVER, ServerError, {"status_code": 503}),
        (BackendErrorReason.TIMEOUT, RPCTimeoutError, {"timeout_seconds": 3.5}),
        (
            BackendErrorReason.UNKNOWN_RPC_METHOD,
            UnknownRPCMethodError,
            {"path": (0, 1), "source": "source-list", "data_at_failure": "scrubbed-row"},
        ),
    ],
)
def test_source_facade_projects_every_closed_backend_error_reason(
    reason: BackendErrorReason,
    expected_type: type[Exception],
    expected_attributes: dict[str, object],
) -> None:
    diagnostics = MappingProxyType(
        {
            "method_id": RPCMethod.GET_NOTEBOOK.value,
            "rpc_code": 13,
            "found_ids": ["src-1"],
            "raw_response": "scrubbed",
            "recoverable": True,
            "status_code": 503,
            "retry_after": 7,
            "limit_bytes": 100,
            "bytes_read": 101,
            "timeout_seconds": 3.5,
            "path": (0, 1),
            "source": "source-list",
            "data_at_failure": "scrubbed-row",
        }
    )
    projected = SourcesAPI._compat_read_error(
        BackendError(
            "backend message",
            operation=None,
            diagnostics=diagnostics,
            reason=reason,
        )
    )

    assert type(projected) is expected_type
    assert str(projected).startswith("backend message")
    assert getattr(projected, "method_id", None) == RPCMethod.GET_NOTEBOOK.value
    assert {name: getattr(projected, name) for name in expected_attributes} == expected_attributes
    if reason is BackendErrorReason.UNKNOWN_RPC_METHOD:
        assert str(projected).count("path=(0, 1)") == 1


@pytest.mark.asyncio
async def test_source_facade_reconstructs_rpc_error_without_replaying_backend_cause() -> None:
    original = RPCError(
        "source decode drift",
        method_id=RPCMethod.GET_NOTEBOOK.value,
        rpc_code=13,
        found_ids=["src-1"],
        raw_response="already-scrubbed",
    )
    rpc = MagicMock(rpc_call=AsyncMock(side_effect=original))
    api = SourcesAPI(
        rpc,
        uploader=MagicMock(),
        _backend=build_web_backend(rpc),
    )

    with pytest.raises(RPCError) as caught:
        await api.list("nb-1")

    assert caught.value is not original
    assert caught.value.__cause__ is None
    assert caught.value.method_id == original.method_id
    assert caught.value.rpc_code == original.rpc_code
    assert caught.value.found_ids == original.found_ids
    assert isinstance(caught.value.found_ids, list)
    assert caught.value.raw_response == original.raw_response
