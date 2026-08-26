"""P9.4b: the mind-map/catalog composites dispatch as ``CustomBinding`` rows.

``MIND_MAP_GENERATE_NOTE``, ``MIND_MAP_GENERATE_INTERACTIVE`` and
``ARTIFACT_GENERATE_MIND_MAP`` declare their natives as keyed specs and
sequence them through the row-scoped invoker.  These tests pin the conversion
oracles: the identical keyword set reaches the runtime for every phase
(including explicit ``False``/``None`` values, ``disable_internal_retries`` on
the guarded create, ``operation_variant="plain"`` on the legacy note
allocation), the closed error identities (quota limit, not-found, feature
unavailable), failure tagging with the selected spec and the deadline
projection.

``artifact.list``/``artifact.get`` left this module's rows in P10 R4.2; the same
oracles now run against ``StudioCatalog``, which sequences ``artifact.catalog``
and the supplemental ``mind_map.list`` merge, so the wire keywords and the
partial-availability net stay pinned at their new authority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from notebooklm._backend import (
    BackendError,
    BackendErrorReason,
)
from notebooklm._binding import CodecBinding, CustomBinding, NativeChoice
from notebooklm._operations import Operation
from notebooklm._records import (
    MIND_MAP_GENERATE_INTERACTIVE_DEF,
    MIND_MAP_GENERATE_NOTE_DEF,
    MindMapGenerateInput,
    MindMapGenerateInteractiveInput,
    MindMapGenerateNoteInput,
)
from notebooklm._studio import NoteBackedMindMapFamilyService, StudioCatalog
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import mind_maps as mind_map_rows
from notebooklm._web.bindings import notes as notes_rows
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_BASE_KWARGS = {
    "allow_null": False,
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
}


@dataclass
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params, kwargs=kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _kwargs(source_path: str, **overrides: Any) -> dict[str, Any]:
    return {**_BASE_KWARGS, "source_path": source_path, **overrides}


# --- registry partition ----------------------------------------------------------


def test_composites_are_custom_rows_with_their_categories_and_specs() -> None:
    expected = {
        Operation.MIND_MAP_GENERATE_NOTE: (
            mind_map_rows.MIND_MAP_GENERATE_NOTE,
            "deferred-product",
        ),
    }
    for operation, (row, category) in expected.items():
        assert isinstance(row, CustomBinding)
        assert WEB_BINDING_ROWS[operation] is row
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.row is row
        assert row.category == category
        assert row.justification.strip()
        assert row.collaborators == ()
    # P10 R5.1b took the interactive family's default-scope read above the port.
    interactive = mind_map_rows.MIND_MAP_GENERATE_INTERACTIVE
    assert isinstance(interactive, CodecBinding)
    assert WEB_BINDING_ROWS[Operation.MIND_MAP_GENERATE_INTERACTIVE] is interactive
    assert interactive.native.select(None) == NativeChoice(RPCMethod.CREATE_ARTIFACT)
    # P10 R4.2 made ``artifact.generate_mind_map`` service-owned; its
    # ``CREATE_NOTE`` phase is the ``note.create`` leaf's declared variant now.
    assert notes_rows.NOTE_CREATE.native.select(None) == NativeChoice(
        RPCMethod.CREATE_NOTE, "plain"
    )
    for name in (
        "_notebook_create",
        "_notebook_update",
        "_notebook_limit_error",
        "_list_notebooks",
        "_mind_map_generate_note",
        "_mind_map_generate",
        "_persist_generated_mind_map",
        "_artifact_list",
        "_artifact_get",
    ):
        assert not hasattr(WebRpcBackend, name)
    with pytest.raises(ModuleNotFoundError):
        __import__("notebooklm._web.deadline_rpc")


# --- mind-map generate rows ---------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_note_defaults_sources_through_get_notebook() -> None:
    executor = _RecordingExecutor([[None, []]], [['{"name": "Tree"}']])
    backend = build_web_backend(executor)

    result = await backend.invoke(
        MIND_MAP_GENERATE_NOTE_DEF, MindMapGenerateNoteInput("nb-1"), deadline=None
    )

    assert result.tree_json == '{"name": "Tree"}'
    sources, generate = executor.calls
    assert sources.method is RPCMethod.GET_NOTEBOOK
    assert sources.kwargs == _kwargs("/notebook/nb-1")
    assert generate.method is RPCMethod.GENERATE_MIND_MAP
    assert generate.kwargs == _kwargs("/notebook/nb-1", allow_null=True)


@pytest.mark.asyncio
async def test_generate_note_with_sources_issues_one_call() -> None:
    executor = _RecordingExecutor([["tree"]])
    backend = build_web_backend(executor)

    await backend.invoke(
        MIND_MAP_GENERATE_NOTE_DEF,
        MindMapGenerateNoteInput("nb-1", source_ids=("s1",)),
        deadline=None,
    )

    assert [call.method for call in executor.calls] == [RPCMethod.GENERATE_MIND_MAP]


@pytest.mark.asyncio
async def test_generate_interactive_creates_an_artifact_or_reports_unavailable() -> None:
    executor = _RecordingExecutor([["mm-1"]])
    backend = build_web_backend(executor)

    result = await backend.invoke(
        MIND_MAP_GENERATE_INTERACTIVE_DEF,
        MindMapGenerateInteractiveInput("nb-1", source_ids=("s1",)),
        deadline=None,
    )
    assert result.mind_map_id == "mm-1"
    (create,) = executor.calls
    assert create.method is RPCMethod.CREATE_ARTIFACT
    assert create.kwargs == _kwargs("/notebook/nb-1", allow_null=True)

    executor = _RecordingExecutor(None)
    backend = build_web_backend(executor)
    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            MIND_MAP_GENERATE_INTERACTIVE_DEF,
            MindMapGenerateInteractiveInput("nb-1", source_ids=("s1",)),
            deadline=None,
        )
    assert caught.value.reason is BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["method_id"] == RPCMethod.CREATE_ARTIFACT.value


@pytest.mark.asyncio
async def test_artifact_mind_map_generation_persists_through_the_note_leaves() -> None:
    executor = _RecordingExecutor([['{"name": "Tree", "children": []}']], [["note-1"]], None)
    backend = build_web_backend(executor)
    service = NoteBackedMindMapFamilyService(backend, StudioCatalog(backend))

    result = await service.generate(MindMapGenerateInput("nb-1", source_ids=("s1",)))

    assert result.note_id == "note-1"
    assert result.mind_map == {"name": "Tree", "children": []}
    generate, create, update = executor.calls
    assert generate.method is RPCMethod.GENERATE_MIND_MAP
    assert generate.kwargs == _kwargs("/notebook/nb-1", allow_null=True)
    assert create.method is RPCMethod.CREATE_NOTE
    assert create.params == ["nb-1", "", [1], None, "Tree"]
    assert create.kwargs == _kwargs("/notebook/nb-1", operation_variant="plain")
    assert update.method is RPCMethod.UPDATE_NOTE
    assert update.params == [
        "nb-1",
        "note-1",
        [[['{"name": "Tree", "children": []}', "Tree", [], 0]]],
    ]
    assert update.kwargs == _kwargs("/notebook/nb-1", allow_null=True)


@pytest.mark.asyncio
async def test_artifact_mind_map_generation_with_an_absent_leaf_persists_nothing() -> None:
    executor = _RecordingExecutor([])
    backend = build_web_backend(executor)
    service = NoteBackedMindMapFamilyService(backend, StudioCatalog(backend))

    result = await service.generate(MindMapGenerateInput("nb-1", source_ids=("s1",)))

    assert (result.mind_map, result.note_id, result.created_at) == (None, None, None)
    assert [call.method for call in executor.calls] == [RPCMethod.GENERATE_MIND_MAP]


# --- catalog rows -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_list_merges_note_backed_mind_maps_with_identical_kwargs() -> None:
    executor = _RecordingExecutor([], [])
    catalog_service = StudioCatalog(build_web_backend(executor))

    result = await catalog_service.list_records("nb-1")

    assert result == ()
    catalog, notes = executor.calls
    assert catalog.method is RPCMethod.LIST_ARTIFACTS
    assert catalog.kwargs == _kwargs("/notebook/nb-1", allow_null=True)
    assert notes.method is RPCMethod.GET_NOTES_AND_MIND_MAPS
    assert notes.params == ["nb-1"]
    assert notes.kwargs == _kwargs("/notebook/nb-1", allow_null=True)


@pytest.mark.asyncio
async def test_artifact_list_for_another_family_skips_the_merge() -> None:
    executor = _RecordingExecutor([])
    catalog_service = StudioCatalog(build_web_backend(executor))

    await catalog_service.list_records("nb-1", family="audio")

    assert [call.method for call in executor.calls] == [RPCMethod.LIST_ARTIFACTS]


@pytest.mark.asyncio
async def test_artifact_get_swallows_a_raw_merge_failure_into_partial_availability(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = httpx.Request("POST", "https://notebooklm.google.com/_/rpc")
    failure = httpx.HTTPStatusError(
        "authentication expired", request=request, response=httpx.Response(401, request=request)
    )
    executor = _RecordingExecutor([], failure)
    catalog_service = StudioCatalog(build_web_backend(executor))

    with caplog.at_level(logging.WARNING, logger="notebooklm._artifact.listing"):
        result = await catalog_service.get_record("nb-1", "art-1")

    assert result is None
    assert "Failed to fetch mind maps" in caplog.text
    assert len(executor.calls) == 2


@pytest.mark.asyncio
async def test_a_failed_catalog_read_is_translated_dispatched_and_tagged() -> None:
    executor = _RecordingExecutor(ServerError("boom", method_id=RPCMethod.LIST_ARTIFACTS.value))
    catalog_service = StudioCatalog(build_web_backend(executor))

    with pytest.raises(BackendError) as caught:
        await catalog_service.list_records("nb-1")

    error = caught.value
    # The primary read is not covered by the partial-availability net, and the
    # workflow re-attributes the leaf failure so the public identity is unchanged.
    assert error.operation is Operation.ARTIFACT_LIST
    assert error.diagnostics["leaf_operation"] is Operation.ARTIFACT_CATALOG
    assert error.reason is BackendErrorReason.SERVER
    assert error.dispatched is True
    assert error.__cause__.binding_native == NativeChoice(RPCMethod.LIST_ARTIFACTS)  # type: ignore[union-attr]
    assert len(executor.calls) == 1
