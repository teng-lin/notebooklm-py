"""Unit tests for the web ``PlayBooksService`` (#2292).

Drives the service against a fake ``RpcCaller`` — no network — to pin the list
decode, the export-eligibility refusal, the added-source id extraction, and the
unconfirmed-on-transport-loss marking that mirrors the sibling AddSourcesAsync
path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._web.sources.play_books import PlayBooksService
from notebooklm.exceptions import (
    NetworkError,
    PlayBookNotExportableError,
    RPCError,
    ServerError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.types import PlayBook

_ROW = [
    "QhsZEAAAQBAJ",
    1,
    "The Art of War",
    "<p>…</p>",
    "https://cover",
    False,
    None,
    ["Sun Tzu"],
    4.6,
    [1788284189],
]


def _book(content_id: str = "QhsZEAAAQBAJ", *, disabled: bool = False) -> PlayBook:
    return PlayBook(
        content_id=content_id,
        title="The Art of War",
        authors=("Sun Tzu",),
        description_html="<p>…</p>",
        cover_url="https://cover",
        export_disabled=disabled,
        reason=None,
        field_type=4.6,
        updated_at=None,
    )


def _rpc(return_value=None, *, side_effect=None) -> MagicMock:
    # A MagicMock container (not AsyncMock) carrying an async ``rpc_call``, so
    # the fake satisfies the ADR-0007 no-AsyncMock-attribute-assignment policy.
    return MagicMock(rpc_call=AsyncMock(return_value=return_value, side_effect=side_effect))


class TestList:
    @pytest.mark.asyncio
    async def test_list_decodes_rows(self) -> None:
        rpc = _rpc([[_ROW]])
        books = await PlayBooksService(rpc).list_play_books()
        assert [b.content_id for b in books] == ["QhsZEAAAQBAJ"]
        method, params = rpc.rpc_call.await_args.args[:2]
        assert method is RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT
        assert params == [None, 1]

    @pytest.mark.asyncio
    async def test_empty_library_returns_empty(self) -> None:
        assert await PlayBooksService(_rpc(None)).list_play_books() == []


class TestAdd:
    @pytest.mark.asyncio
    async def test_add_returns_new_source_id(self) -> None:
        response = [
            [[["src_new"], "New Source", [None, None, None, None, 20]]],
            None,
            [[[["src_new"], "New Source", [None, None, None, None, 20]], 0]],
        ]
        rpc = _rpc(response)
        source_id = await PlayBooksService(rpc).add_play_book_spec("nb_1", _book())
        assert source_id == "src_new"
        method, params = rpc.rpc_call.await_args.args[:2]
        assert method is RPCMethod.ADD_SOURCES_ASYNC
        # spec at index 15, f11 marker at 10.
        spec = params[0][0]
        assert spec[10] == 1
        assert spec[15][1] == "QhsZEAAAQBAJ"
        assert rpc.rpc_call.await_args.kwargs["operation_variant"] == "play_book"

    @pytest.mark.asyncio
    async def test_export_disabled_is_refused_without_dispatch(self) -> None:
        rpc = _rpc()
        with pytest.raises(PlayBookNotExportableError):
            await PlayBooksService(rpc).add_play_book_spec("nb_1", _book(disabled=True))
        rpc.rpc_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_id_in_response_is_unconfirmed(self) -> None:
        # An id-less stub means the add may still have committed — surface it as
        # UNRESOLVED, not a clean SourceNotFoundError a caller could retry.
        rpc = _rpc([[[[""], "New Source", [None]]], None, []])
        with pytest.raises(RPCError) as caught:
            await PlayBooksService(rpc).add_play_book_spec("nb_1", _book())
        assert getattr(caught.value, "unconfirmed", False) is True

    @pytest.mark.asyncio
    async def test_undecodable_response_is_unconfirmed(self) -> None:
        # A response-shape break after dispatch is also UNRESOLVED, not a failed
        # add: first_added_source_id's safe_index raises, which we re-wrap.
        rpc = _rpc([])  # empty envelope → safe_index drift
        with pytest.raises(RPCError) as caught:
            await PlayBooksService(rpc).add_play_book_spec("nb_1", _book())
        assert getattr(caught.value, "unconfirmed", False) is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc", [ServerError("500"), NetworkError("down")])
    async def test_transport_loss_marks_unconfirmed(self, exc: Exception) -> None:
        rpc = _rpc(side_effect=exc)
        with pytest.raises(RPCError) as caught:
            await PlayBooksService(rpc).add_play_book_spec("nb_1", _book())
        # The write may have committed before the response was lost — surfaced
        # as an unconfirmed RPCError so callers reconcile rather than retry.
        assert getattr(caught.value, "unconfirmed", False) is True
        assert "UNRESOLVED" in str(caught.value)
