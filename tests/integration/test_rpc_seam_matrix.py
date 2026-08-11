"""Cross-seam coverage for the RPC wire envelope and a real client adapter."""

import json
from urllib.parse import parse_qs

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc import RPCMethod

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.asyncio
async def test_notebooks_list_crosses_rpc_encode_decode_seam(
    httpx_mock: HTTPXMock, auth_tokens, build_rpc_response
):
    """The notebooks adapter observes the real RPC envelope and decoded rows."""
    response = build_rpc_response(
        RPCMethod.LIST_NOTEBOOKS,
        [
            [
                [
                    "Boundary notebook",
                    [["source-1"]],
                    "notebook-1",
                    "📓",
                    None,
                    [None, False, None, None, None, [1704153600, 0]],
                ]
            ]
        ],
    )
    httpx_mock.add_response(content=response.encode())

    async with NotebookLMClient(auth_tokens) as client:
        notebooks = await client.notebooks.list()

    request = httpx_mock.get_request()
    assert request.url.params["rpcids"] == RPCMethod.LIST_NOTEBOOKS.value
    form = parse_qs(request.content.decode())
    assert json.loads(form["f.req"][0]) == [
        [[RPCMethod.LIST_NOTEBOOKS.value, "[null,1,null,[2]]", None, "generic"]]
    ]
    assert form["at"] == [auth_tokens.csrf_token]
    assert [(notebook.id, notebook.title) for notebook in notebooks] == [
        ("notebook-1", "Boundary notebook")
    ]


@pytest.mark.asyncio
async def test_notebooks_list_surfaces_response_rpc_id_drift(
    httpx_mock: HTTPXMock, auth_tokens, build_rpc_response
):
    """A response for another RPC cannot silently pass through the adapter."""
    httpx_mock.add_response(content=build_rpc_response("rotated-rpc-id", [[]]).encode())

    async with NotebookLMClient(auth_tokens) as client:
        with pytest.raises(UnknownRPCMethodError, match="may have changed"):
            await client.notebooks.list()
