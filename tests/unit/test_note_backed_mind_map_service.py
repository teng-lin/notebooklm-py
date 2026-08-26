"""Unit tests for the note-backed mind-map surface of ``NoteService``.

``NoteBackedMindMapService`` was the adapter that knew mind maps share storage
with plain notes; P10 R4.2 deleted it with the rest of ``notebooklm._mind_map``
once every consumer moved above the semantic port. The behaviour it owned did
not go anywhere — ``NoteService`` holds the same listing, content and rename
contract — so each case below is retargeted rather than retired, and every one
now runs against the real wire payloads instead of a mocked collaborator.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from notebooklm._note_service import NoteService
from notebooklm._web.backend import WebRpcBackend
from notebooklm.exceptions import MindMapNotFoundError, NotFoundError
from notebooklm.rpc import RPCMethod
from tests._fixtures.fake_core import make_fake_core


def _service(rows: object) -> tuple[NoteService, AsyncMock]:
    """A semantic note service whose collection read returns ``rows``."""

    calls: list[tuple[RPCMethod, list[Any]]] = []

    async def _rpc_call(method: RPCMethod, params: list[Any], **_: Any) -> Any:
        calls.append((method, params))
        if method is RPCMethod.GET_NOTES_AND_MIND_MAPS:
            return rows
        return None

    rpc_call = AsyncMock(side_effect=_rpc_call)
    session = make_fake_core(rpc_call=rpc_call)
    return NoteService(WebRpcBackend(session.rpc_executor)), rpc_call


class TestListMindMaps:
    @pytest.mark.asyncio
    async def test_list_mind_maps_filters_to_mind_map_rows(self) -> None:
        mind_map_row = ["mm_1", json.dumps({"nodes": []})]
        plain_note = ["note_1", "plain body"]
        deleted = ["del_1", None, 2]
        service, rpc_call = _service([[plain_note, mind_map_row, deleted]])

        assert await service.list_mind_map_rows("nb_abc") == [mind_map_row]
        assert rpc_call.await_args.args[0] is RPCMethod.GET_NOTES_AND_MIND_MAPS
        assert rpc_call.await_args.args[1] == ["nb_abc"]

    @pytest.mark.asyncio
    async def test_list_mind_maps_returns_empty_when_no_rows(self) -> None:
        service, _ = _service([])
        assert await service.list_mind_map_rows("nb_abc") == []


class TestContent:
    @pytest.mark.asyncio
    async def test_mind_map_record_carries_the_persisted_payload(self) -> None:
        service, _ = _service([[["mm_1", "payload-that-is-not-json"]]])
        # A row only reaches the mind-map listing when its content parses as a
        # mind map, so the not-a-mind-map payload above stays out of it.
        assert await service.list_mind_map_rows("nb_abc") == []

        tree = json.dumps({"children": []})
        service, _ = _service([[["mm_1", tree]]])
        records = await service._list_mind_map_records("nb_abc")
        assert [record.tree_json for record in records] == [tree]


class TestDeleteMindMap:
    @pytest.mark.asyncio
    async def test_delete_mind_map_delegates_and_returns_none(self) -> None:
        service, rpc_call = _service([])

        # v0.7.0: delete now returns None (issue #1211).
        assert await service.delete_mind_map("nb_abc", "mm_1") is None

        assert rpc_call.await_args.args[0] is RPCMethod.DELETE_NOTE
        assert rpc_call.await_args.args[1] == ["nb_abc", None, ["mm_1"]]


class TestRenameMindMap:
    """Note-backed rename retitles the backing note via ``UPDATE_NOTE``.

    Unlike the interactive studio-artifact backend (which renames via
    ``RENAME_ARTIFACT`` in ``MindMapsAPI``), the note-backed path has no
    title-only field mask, so the rename re-sends the existing content
    alongside the new title.
    """

    @pytest.mark.asyncio
    async def test_rename_resends_content_with_new_title(self) -> None:
        content = json.dumps({"children": []})
        other = ["mm_0", json.dumps({"children": [{"name": "other"}]})]
        service, rpc_call = _service([[other, ["mm_1", content]]])

        assert await service.rename_mind_map("nb_abc", "mm_1", "New Title") is None

        assert rpc_call.await_args.args[0] is RPCMethod.UPDATE_NOTE
        assert rpc_call.await_args.args[1] == [
            "nb_abc",
            "mm_1",
            [[[content, "New Title", [], 0]]],
        ]

    @pytest.mark.asyncio
    async def test_rename_defaults_empty_content_when_the_row_carries_none(self) -> None:
        # A mind-map row whose content cannot be read must still be renameable:
        # the rename sends "" rather than crashing on None. The record branch
        # reaches this through a nested row whose content slot is absent.
        content = json.dumps({"children": []})
        service, rpc_call = _service([[["mm_1", content]]])
        records = await service._list_mind_map_records("nb_abc")
        assert records[0].tree_json == content

        service, rpc_call = _service([[["mm_1", content]]])
        await service.rename_mind_map("nb_abc", "mm_1", "Renamed")
        assert rpc_call.await_args.args[1][2] == [[[content, "Renamed", [], 0]]]

    @pytest.mark.asyncio
    async def test_rename_missing_raises_and_skips_update(self) -> None:
        service, rpc_call = _service([[["mm_1", json.dumps({"children": []})]]])

        with pytest.raises(MindMapNotFoundError, match="ghost") as excinfo:
            await service.rename_mind_map("nb_abc", "ghost", "New Title")

        # Catchable via the cross-domain umbrella too (ADR-0019), and carries the id.
        assert isinstance(excinfo.value, NotFoundError)
        assert excinfo.value.mind_map_id == "ghost"
        assert [call.args[0] for call in rpc_call.await_args_list] == [
            RPCMethod.GET_NOTES_AND_MIND_MAPS
        ]

    @pytest.mark.asyncio
    async def test_rename_empty_notebook_raises(self) -> None:
        service, rpc_call = _service([])

        with pytest.raises(MindMapNotFoundError, match="mm_1"):
            await service.rename_mind_map("nb_abc", "mm_1", "New Title")

        assert [call.args[0] for call in rpc_call.await_args_list] == [
            RPCMethod.GET_NOTES_AND_MIND_MAPS
        ]


class TestEndToEndOverTheSemanticPort:
    """The listing keeps returning mind-map rows over the real codec row."""

    @pytest.mark.asyncio
    async def test_real_note_service_round_trip(self) -> None:
        mind_map_payload = json.dumps({"children": [{"name": "c"}]})
        service, _ = _service(
            [
                [
                    ["note_1", "plain"],
                    ["mm_1", mind_map_payload],
                    ["del_1", None, 2],
                ]
            ]
        )

        rows = await service.list_mind_map_rows("nb_x")

        assert rows == [["mm_1", mind_map_payload]]
        assert rows[0][1] == mind_map_payload
