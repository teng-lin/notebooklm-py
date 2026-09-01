"""Full-stack (facade → wire) integration tests for Play Books sources (#2292).

Mock-backed via ``pytest_httpx`` / ``build_rpc_response`` — the same harness the
other ``sources_*`` integration tests use — so the ``mVtEUb`` list and the
``X1snv`` add ride the real encode/decode path without live access.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.exceptions import PlayBookNotExportableError, SourceNotFoundError
from notebooklm.rpc import RPCMethod
from notebooklm.types import PlayBookExportReason, SourceStatus, SourceType

pytestmark = pytest.mark.allow_no_vcr

_EXPORTABLE_ROW = [
    "QhsZEAAAQBAJ",
    1,
    "The Art of War",
    "<p>Sun Tzu…</p>",
    "https://cover/QhsZEAAAQBAJ",
    False,
    None,
    ["Sun Tzu"],
    4.6458335,
    [1788284189],
]
_BLOCKED_ROW = [
    "kLrxEQAAQBAJ",
    1,
    "Bill Gates",
    "<p>…</p>",
    "https://cover/kLrxEQAAQBAJ",
    True,
    1,
    ["Author"],
    None,
    [1788284190],
]


class TestListPlayBooks:
    @pytest.mark.asyncio
    async def test_list(self, auth_tokens, httpx_mock: HTTPXMock, build_rpc_response) -> None:
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT,
                [[_EXPORTABLE_ROW, _BLOCKED_ROW]],
            ).encode()
        )
        async with NotebookLMClient(auth_tokens) as client:
            books = await client.sources.list_play_books()

        assert [b.content_id for b in books] == ["QhsZEAAAQBAJ", "kLrxEQAAQBAJ"]
        assert books[0].export_disabled is False
        assert books[0].authors == ("Sun Tzu",)
        assert books[1].export_disabled is True
        assert books[1].reason is PlayBookExportReason.OPTED_OUT
        urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert any(RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT in url for url in urls)

    @pytest.mark.asyncio
    async def test_empty_library(
        self, auth_tokens, httpx_mock: HTTPXMock, build_rpc_response
    ) -> None:
        httpx_mock.add_response(
            content=build_rpc_response(RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT, None).encode()
        )
        async with NotebookLMClient(auth_tokens) as client:
            assert await client.sources.list_play_books() == []


class TestAddPlayBook:
    @pytest.mark.asyncio
    async def test_add_exportable(
        self, auth_tokens, httpx_mock: HTTPXMock, build_rpc_response
    ) -> None:
        # 1) library lookup, 2) X1snv add. wait=False returns a PROCESSING stub
        # straight from the confirmed source id — no follow-up GET_NOTEBOOK read
        # (which could raise on a transient fault after the committed write).
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT, [[_EXPORTABLE_ROW]]
            ).encode()
        )
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.ADD_SOURCES_ASYNC,
                [
                    [[["src_book"], "New Source", [None, None, None, None, 20]]],
                    None,
                    [[[["src_book"], "New Source", [None, None, None, None, 20]], 0]],
                ],
            ).encode()
        )

        async with NotebookLMClient(auth_tokens) as client:
            source = await client.sources.add_play_book("nb_1", "QhsZEAAAQBAJ")

        assert source.id == "src_book"
        assert source.kind is SourceType.EXPERT_INTELLIGENCE
        assert source.status == SourceStatus.PROCESSING
        # The title comes from the resolved library row, not a follow-up read.
        assert source.title == "The Art of War"
        urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert any(RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT in url for url in urls)
        assert any(RPCMethod.ADD_SOURCES_ASYNC in url for url in urls)
        # No GET_NOTEBOOK read on the wait=False path.
        assert not any(RPCMethod.GET_NOTEBOOK in url for url in urls)

    @pytest.mark.asyncio
    async def test_refuses_blocked_title_before_adding(
        self, auth_tokens, httpx_mock: HTTPXMock, build_rpc_response
    ) -> None:
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT, [[_BLOCKED_ROW]]
            ).encode()
        )
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(PlayBookNotExportableError) as exc:
                await client.sources.add_play_book("nb_1", "kLrxEQAAQBAJ")

        assert exc.value.content_id == "kLrxEQAAQBAJ"
        assert exc.value.reason is PlayBookExportReason.OPTED_OUT
        # No add RPC fired — the refusal is client-side.
        urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert not any(RPCMethod.ADD_SOURCES_ASYNC in url for url in urls)

    @pytest.mark.asyncio
    async def test_not_in_library_raises(
        self, auth_tokens, httpx_mock: HTTPXMock, build_rpc_response
    ) -> None:
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT, [[_EXPORTABLE_ROW]]
            ).encode()
        )
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(SourceNotFoundError):
                await client.sources.add_play_book("nb_1", "NOT_IN_LIBRARY")
