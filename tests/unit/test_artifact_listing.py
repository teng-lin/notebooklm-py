"""Artifact representation catalog shape handling after P5.8 retirement."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._backend import (
    BackendContractError,
    BackendError,
    BackendErrorReason,
    BackendKind,
    UnsupportedOperationError,
)
from notebooklm._operations import Operation
from notebooklm._semantic.records import (
    ARTIFACT_DOWNLOAD_DEF,
    ArtifactDownloadInput,
    ArtifactRecord,
    ArtifactRepresentationRecord,
    MindMapRepresentationRecord,
)
from notebooklm.exceptions import RPCError
from tests._fixtures.web_backend import build_web_backend


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], False])
async def test_falsy_catalog_payload_is_empty(payload: object) -> None:
    core = MagicMock(rpc_call=AsyncMock(return_value=payload))

    result = await build_web_backend(core).invoke(
        ARTIFACT_DOWNLOAD_DEF,
        ArtifactDownloadInput("nb", "catalog"),
        deadline=None,
    )

    assert result.representations == ()


@pytest.mark.asyncio
async def test_truthy_non_list_catalog_payload_fails_loud() -> None:
    core = MagicMock(rpc_call=AsyncMock(return_value={"moved": True}))

    with pytest.raises(BackendError, match="Unrecognized LIST_ARTIFACTS") as caught:
        await build_web_backend(core).invoke(
            ARTIFACT_DOWNLOAD_DEF,
            ArtifactDownloadInput("nb", "catalog"),
            deadline=None,
        )

    assert caught.value.reason is BackendErrorReason.DECODING


def test_prefetched_neutral_representation_records_are_not_decoded_twice() -> None:
    """The shared CLI/MCP/REST prefetch handoff is typed and idempotent."""

    representation = ArtifactRepresentationRecord(
        ArtifactRecord("audio-id", "Audio", "audio", "completed")
    )
    mind_map = MindMapRepresentationRecord("map-id", "Map", "{}")

    assert ArtifactsAPI._representation_records([representation]) == (representation,)
    assert ArtifactsAPI._mind_map_records([mind_map]) == (mind_map,)


def test_representation_records_decodes_raw_wire_rows() -> None:
    """Public ``download_<x>(..., artifacts_data=...)`` callers may still pass
    raw ``LIST_ARTIFACTS`` rows, not just pre-decoded records (P10 PR1 review
    finding 1): ``_representation_records`` must decode them rather than
    handing the studio/download layer an undecoded row it can't use.
    """
    raw_row: list[object] = ["slide_001", "Slide Deck Title", 8, None, 3]
    raw_row.extend([None] * 11)
    raw_row.append(
        [
            ["config"],
            "Slide Deck Title",
            [["slide1"], ["slide2"]],
            "https://contribution.usercontent.google.com/download?filename=test.pdf",
        ]
    )

    (record,) = ArtifactsAPI._representation_records([raw_row])

    assert isinstance(record, ArtifactRepresentationRecord)
    assert record.artifact.id == "slide_001"
    assert record.artifact.family == "slide_deck"


def test_mind_map_records_decodes_raw_note_rows_and_drops_none() -> None:
    """Same raw-row tolerance for ``download_mind_map(..., mind_maps=...)``,
    plus the ``None`` filter (a deleted/non-mind-map note row decodes to
    ``None`` and must not reach ``_select`` as a bare ``None``)."""
    mind_map_row = ["mm_1", json.dumps({"children": []})]
    non_mind_map_row = ["note_1", "just a plain note, not a mind map"]

    records = ArtifactsAPI._mind_map_records([mind_map_row, non_mind_map_row])

    assert records is not None
    assert len(records) == 1
    assert isinstance(records[0], MindMapRepresentationRecord)
    assert records[0].id == "mm_1"


@pytest.mark.asyncio
async def test_download_prefetch_partial_availability_is_closed_and_fail_closed() -> None:
    representation = ArtifactRepresentationRecord(
        ArtifactRecord("audio-id", "Audio", "audio", "completed")
    )
    service = MagicMock()
    service._list_representations = AsyncMock(return_value=(representation,))
    api = object.__new__(ArtifactsAPI)
    api._representations = service

    service._list_mind_maps = AsyncMock(
        side_effect=BackendError(
            "temporary RPC failure",
            operation=Operation.ARTIFACT_DOWNLOAD,
            reason=BackendErrorReason.RPC,
            diagnostics={
                "method_id": "rpc-id",
                "raw_response": None,
                "rpc_code": None,
                "found_ids": [],
            },
        )
    )
    artifacts, studio, mind_maps = await api._list_for_download("nb")
    assert [item.id for item in artifacts] == ["audio-id"]
    assert studio == [representation]
    assert mind_maps is None

    contract_error = BackendContractError(
        "invalid representation result",
        operation=Operation.ARTIFACT_DOWNLOAD,
    )
    service._list_mind_maps = AsyncMock(side_effect=contract_error)
    with pytest.raises(BackendContractError) as caught:
        await api._list_for_download("nb")
    assert caught.value is contract_error

    unsupported = UnsupportedOperationError(Operation.ARTIFACT_DOWNLOAD, BackendKind.WEB)
    service._list_mind_maps = AsyncMock(side_effect=unsupported)
    with pytest.raises(UnsupportedOperationError) as caught_unsupported:
        await api._list_for_download("nb")
    assert caught_unsupported.value is unsupported

    service._list_mind_maps = AsyncMock(
        side_effect=BackendError(
            "schema drift",
            operation=Operation.ARTIFACT_DOWNLOAD,
            reason=BackendErrorReason.DECODING,
            diagnostics={
                "method_id": "rpc-id",
                "raw_response": None,
                "rpc_code": None,
                "found_ids": [],
            },
        )
    )
    with pytest.raises(RPCError, match="schema drift"):
        await api._list_for_download("nb")
