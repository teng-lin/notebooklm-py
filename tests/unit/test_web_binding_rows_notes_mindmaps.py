"""P9.3 notes/mind maps: nine leaf handlers became codec rows that dispatch as before.

The rows live in ``_web/bindings/notes.py`` and ``_web/bindings/mind_maps.py``.
These tests pin the conversion oracles: the identical keyword set reaches the
runtime for every converted operation (including explicit ``False``/``None``
values and the notebook route), the payloads are byte-for-byte the handlers'
params, decoders still need the input where they did before, failure
projection is what ``invoke()`` produced for handler rows, and the
``dispatched`` marker reaches the neutral error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CodecBinding, CodecPayload, DeadlineMode
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._records import (
    MIND_MAP_DELETE_DEF,
    MIND_MAP_GET_DEF,
    MIND_MAP_LIST_DEF,
    MIND_MAP_UPDATE_DEF,
    NOTE_CREATE_DEF,
    NOTE_DELETE_DEF,
    NOTE_GET_DEF,
    NOTE_LIST_DEF,
    NOTE_UPDATE_DEF,
    MindMapDeleteInput,
    MindMapDeleteResult,
    MindMapGetInput,
    MindMapListInput,
    MindMapUpdateInput,
    MindMapUpdateResult,
    NoteCreateInput,
    NoteDeleteInput,
    NoteDeleteResult,
    NoteGetInput,
    NoteListInput,
    NoteUpdateInput,
    NoteUpdateResult,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import mind_maps as mind_map_rows
from notebooklm._web.bindings import notes as note_rows
from notebooklm._web.codec import mind_maps as mind_maps_codec
from notebooklm._web.codec import notes as notes_codec
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import RPCError, RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_NOTE_ROW = ["note-1", ["note-1", "Plain body", None, None, [1_700_000_000, 0]], "Title"]
_MIND_MAP_ROW = [
    "mm-1",
    ["mm-1", '{"nodes": [{"name": "root", "children": []}]}', None, None, [1_700_000_001, 0]],
    "Map",
]
_NOTES_RESPONSE = [[_NOTE_ROW, _MIND_MAP_ROW]]
_INTERACTIVE_RESPONSE = [
    [None, None, None, None, None, None, None, None, None, [0, 0, 0, '{"tree": 1}']]
]


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


_CONVERTED: dict[Operation, CodecBinding[Any, Any, RPCMethod]] = {
    Operation.NOTE_LIST: note_rows.NOTE_LIST,
    Operation.NOTE_GET: note_rows.NOTE_GET,
    Operation.NOTE_CREATE: note_rows.NOTE_CREATE,
    Operation.NOTE_UPDATE: note_rows.NOTE_UPDATE,
    Operation.NOTE_DELETE: note_rows.NOTE_DELETE,
    Operation.MIND_MAP_LIST: mind_map_rows.MIND_MAP_LIST,
    Operation.MIND_MAP_GET: mind_map_rows.MIND_MAP_GET,
    Operation.MIND_MAP_UPDATE: mind_map_rows.MIND_MAP_UPDATE,
    Operation.MIND_MAP_DELETE: mind_map_rows.MIND_MAP_DELETE,
}

_EXPECTED_NATIVES = {
    Operation.NOTE_LIST: (RPCMethod.GET_NOTES_AND_MIND_MAPS, None),
    Operation.NOTE_GET: (RPCMethod.GET_NOTES_AND_MIND_MAPS, None),
    Operation.NOTE_CREATE: (RPCMethod.CREATE_NOTE, "plain"),
    Operation.NOTE_UPDATE: (RPCMethod.UPDATE_NOTE, None),
    Operation.NOTE_DELETE: (RPCMethod.DELETE_NOTE, None),
    Operation.MIND_MAP_LIST: (RPCMethod.GET_NOTES_AND_MIND_MAPS, None),
    Operation.MIND_MAP_GET: (RPCMethod.GET_INTERACTIVE_HTML, None),
    Operation.MIND_MAP_UPDATE: (RPCMethod.RENAME_ARTIFACT, None),
    Operation.MIND_MAP_DELETE: (RPCMethod.DELETE_ARTIFACT, None),
}


def test_note_and_mind_map_rows_replace_their_handlers_in_the_registry_and_table() -> None:
    assert {op: WEB_BINDING_ROWS[op] for op in _CONVERTED} == _CONVERTED
    assert dict(note_rows.NOTE_ROWS) == {
        op: row for op, row in _CONVERTED.items() if op.value.startswith("note.")
    }
    # P9.4b adds the generate and catalog custom rows to the same table.
    assert {op: row for op, row in mind_map_rows.MIND_MAP_ROWS.items() if op in _CONVERTED} == {
        op: row for op, row in _CONVERTED.items() if op.value.startswith("mind_map.")
    }
    for operation, row in _CONVERTED.items():
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.row is row
        assert isinstance(row, CodecBinding)
        assert row.definition is binding.definition
        assert row.deadline is DeadlineMode.INHERIT
        assert row.native.is_constant
        choice = row.native.select(None)
        assert (choice.method, choice.variant) == _EXPECTED_NATIVES[operation]
        assert row.forward_disable_internal_retries is False
        # ``mind_map.list`` carries the one scoped mapper in this family: the
        # Studio catalog's supplemental read needs its raw transport leaf as a
        # neutral reason (P10 R4.2). Every other leaf here maps nothing.
        assert (row.map_error is None) is (operation is not Operation.MIND_MAP_LIST)
    for name in (
        "_note_list",
        "_note_get",
        "_note_create",
        "_note_update",
        "_note_delete",
        "_mind_map_list",
        "_mind_map_get",
        "_mind_map_update",
        "_mind_map_delete",
    ):
        assert not hasattr(WebRpcBackend, name)
    # P9.4b: the input-defaulting generate composites are custom rows.
    for operation in (Operation.MIND_MAP_GENERATE_NOTE, Operation.MIND_MAP_GENERATE_INTERACTIVE):
        assert WEB_OPERATION_REGISTRY[operation].row is not None
    backend = build_web_backend(_RecordingExecutor())
    assert backend._bindings[Operation.NOTE_CREATE] is note_rows.NOTE_CREATE
    assert backend._bindings[Operation.MIND_MAP_GET] is mind_map_rows.MIND_MAP_GET


# --- codec goldens: the payload builders reproduce the handlers' params exactly --------


@pytest.mark.parametrize(
    ("encode", "value", "expected"),
    [
        (
            notes_codec.encode_note_list,
            NoteListInput("nb"),
            CodecPayload(params=["nb"], source_path="/notebook/nb", allow_null=True),
        ),
        (
            notes_codec.encode_note_get,
            NoteGetInput("nb", "note-1"),
            CodecPayload(params=["nb"], source_path="/notebook/nb", allow_null=True),
        ),
        (
            notes_codec.encode_note_create,
            NoteCreateInput("nb", "Title", "Body"),
            CodecPayload(params=["nb", "", [1], None, "Title"], source_path="/notebook/nb"),
        ),
        (
            notes_codec.encode_note_update,
            NoteUpdateInput("nb", "note-1", title="New title", content="New body"),
            CodecPayload(
                params=["nb", "note-1", [[["New body", "New title", [], 0]]]],
                source_path="/notebook/nb",
                allow_null=True,
            ),
        ),
        (
            notes_codec.encode_note_delete,
            NoteDeleteInput("nb", "note-1"),
            CodecPayload(
                params=["nb", None, ["note-1"]], source_path="/notebook/nb", allow_null=True
            ),
        ),
        (
            mind_maps_codec.encode_mind_map_list,
            MindMapListInput("nb"),
            CodecPayload(params=["nb"], source_path="/notebook/nb", allow_null=True),
        ),
        (
            mind_maps_codec.encode_mind_map_get,
            MindMapGetInput("nb", "mm-1"),
            CodecPayload(params=["mm-1"], source_path="/notebook/nb", allow_null=True),
        ),
        (
            mind_maps_codec.encode_mind_map_update,
            MindMapUpdateInput("nb", "mm-1", "Renamed"),
            CodecPayload(
                params=[["mm-1", "Renamed"], [["title"]]],
                source_path="/notebook/nb",
                allow_null=True,
            ),
        ),
        (
            mind_maps_codec.encode_mind_map_delete,
            MindMapDeleteInput("nb", "mm-1"),
            CodecPayload(params=[[2], "mm-1"], source_path="/notebook/nb", allow_null=True),
        ),
    ],
    ids=lambda item: getattr(item, "__name__", None) or type(item).__name__,
)
def test_row_payloads_match_the_handler_params(
    encode: Any, value: Any, expected: CodecPayload
) -> None:
    payload = encode(value)
    assert payload == expected
    assert payload.raise_on_null_status is False
    assert payload.attempt_timeout is None


def test_row_decoders_carry_the_input_where_the_legacy_decoder_needed_it() -> None:
    notes = notes_codec.decode_note_list(NoteListInput("nb"), _NOTES_RESPONSE)
    assert [note.id for note in notes.notes] == ["note-1"]
    assert notes.notes[0].notebook_id == "nb"
    assert notes_codec.decode_note_get(NoteGetInput("nb", "missing"), _NOTES_RESPONSE).note is None
    assert notes_codec.decode_note_get(NoteGetInput("nb", "note-1"), _NOTES_RESPONSE).note.id == (
        "note-1"
    )
    created = notes_codec.decode_note_create(
        NoteCreateInput("nb", "Title", "Body"), [["created-1", None, None, None, [5, 0]]]
    )
    assert (created.note.id, created.note.title, created.note.content) == (
        "created-1",
        "Title",
        "Body",
    )
    with pytest.raises(RPCError):
        notes_codec.decode_note_create(NoteCreateInput("nb", "Title", "Body"), [[None]])
    assert (
        notes_codec.decode_note_update(NoteUpdateInput("nb", "n", "t", "c"), None)
        == NoteUpdateResult()
    )
    assert notes_codec.decode_note_delete(NoteDeleteInput("nb", "n"), None) == NoteDeleteResult()

    mind_maps = mind_maps_codec.decode_mind_map_list(MindMapListInput("nb"), _NOTES_RESPONSE)
    assert [item.id for item in mind_maps.mind_maps] == ["mm-1"]
    assert mind_maps.mind_maps[0].kind == "note_backed"
    tree = mind_maps_codec.decode_mind_map_get(MindMapGetInput("nb", "mm-1"), _INTERACTIVE_RESPONSE)
    assert tree.tree_json == '{"tree": 1}'
    assert (
        mind_maps_codec.decode_mind_map_get(MindMapGetInput("nb", "mm-1"), None).tree_json is None
    )
    assert (
        mind_maps_codec.decode_mind_map_update(MindMapUpdateInput("nb", "mm-1", "x"), None)
        == MindMapUpdateResult()
    )
    assert (
        mind_maps_codec.decode_mind_map_delete(MindMapDeleteInput("nb", "mm-1"), None)
        == MindMapDeleteResult()
    )


# --- dispatch oracles ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rows_forward_the_identical_keyword_set() -> None:
    executor = _RecordingExecutor(
        _NOTES_RESPONSE,  # note.list
        _NOTES_RESPONSE,  # note.get
        [["created-1", None, None, None, [5, 0]]],  # note.create
        None,  # note.update
        None,  # note.delete
        _NOTES_RESPONSE,  # mind_map.list
        _INTERACTIVE_RESPONSE,  # mind_map.get
        None,  # mind_map.update
        None,  # mind_map.delete
    )
    backend = build_web_backend(executor)

    listed = await backend.invoke(NOTE_LIST_DEF, NoteListInput("nb"), deadline=None)
    got = await backend.invoke(NOTE_GET_DEF, NoteGetInput("nb", "note-1"), deadline=None)
    created = await backend.invoke(
        NOTE_CREATE_DEF, NoteCreateInput("nb", "Title", "Body"), deadline=None
    )
    updated = await backend.invoke(
        NOTE_UPDATE_DEF, NoteUpdateInput("nb", "note-1", title="T", content="C"), deadline=None
    )
    deleted = await backend.invoke(NOTE_DELETE_DEF, NoteDeleteInput("nb", "note-1"), deadline=None)
    maps = await backend.invoke(MIND_MAP_LIST_DEF, MindMapListInput("nb"), deadline=None)
    tree = await backend.invoke(MIND_MAP_GET_DEF, MindMapGetInput("nb", "mm-1"), deadline=None)
    renamed = await backend.invoke(
        MIND_MAP_UPDATE_DEF, MindMapUpdateInput("nb", "mm-1", "Renamed"), deadline=None
    )
    removed = await backend.invoke(
        MIND_MAP_DELETE_DEF, MindMapDeleteInput("nb", "mm-1"), deadline=None
    )

    assert [note.id for note in listed.notes] == ["note-1"]
    assert got.note is not None and got.note.id == "note-1"
    assert created.note.id == "created-1"
    assert updated == NoteUpdateResult()
    assert deleted == NoteDeleteResult()
    assert [item.id for item in maps.mind_maps] == ["mm-1"]
    assert tree.tree_json == '{"tree": 1}'
    assert renamed == MindMapUpdateResult()
    assert removed == MindMapDeleteResult()

    base_kwargs = {
        "source_path": "/notebook/nb",
        "allow_null": True,
        "_is_retry": False,
        "disable_internal_retries": False,
        "operation_variant": None,
        "read_timeout": None,
        "raise_on_null_status": False,
        "_retry_deadline": None,
    }
    expected = [
        (RPCMethod.GET_NOTES_AND_MIND_MAPS, ["nb"], base_kwargs),
        (RPCMethod.GET_NOTES_AND_MIND_MAPS, ["nb"], base_kwargs),
        (
            RPCMethod.CREATE_NOTE,
            ["nb", "", [1], None, "Title"],
            {**base_kwargs, "allow_null": False, "operation_variant": "plain"},
        ),
        (RPCMethod.UPDATE_NOTE, ["nb", "note-1", [[["C", "T", [], 0]]]], base_kwargs),
        (RPCMethod.DELETE_NOTE, ["nb", None, ["note-1"]], base_kwargs),
        (RPCMethod.GET_NOTES_AND_MIND_MAPS, ["nb"], base_kwargs),
        (RPCMethod.GET_INTERACTIVE_HTML, ["mm-1"], base_kwargs),
        (RPCMethod.RENAME_ARTIFACT, [["mm-1", "Renamed"], [["title"]]], base_kwargs),
        (RPCMethod.DELETE_ARTIFACT, [[2], "mm-1"], base_kwargs),
    ]
    assert [(call.method, call.params, call.kwargs) for call in executor.calls] == expected


@pytest.mark.asyncio
async def test_row_read_timeout_is_clamped_to_the_shared_deadline() -> None:
    executor = _RecordingExecutor(_NOTES_RESPONSE)
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    await backend.invoke(MIND_MAP_LIST_DEF, MindMapListInput("nb"), deadline=deadline)

    (call,) = executor.calls
    assert call.kwargs["read_timeout"] == pytest.approx(4.0)
    assert call.kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_row_server_error_translates_like_a_handler_and_is_dispatched() -> None:
    executor = _RecordingExecutor(ServerError("boom", method_id=RPCMethod.UPDATE_NOTE.value))
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            NOTE_UPDATE_DEF, NoteUpdateInput("nb", "note-1", title="T", content="C"), deadline=None
        )

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.NOTE_UPDATE
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.UPDATE_NOTE.value
    assert "public_error_failure" in error.diagnostics
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert isinstance(error.__cause__, ServerError)


@pytest.mark.asyncio
async def test_row_timeout_after_expiry_becomes_a_dispatched_deadline_error() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(
        RPCTimeoutError("slow", method_id=RPCMethod.DELETE_ARTIFACT.value)
    )
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            MIND_MAP_DELETE_DEF, MindMapDeleteInput("nb", "mm-1"), deadline=deadline
        )

    error = caught.value
    assert error.operation is Operation.MIND_MAP_DELETE
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.outcome_unknown is True  # MUTATION policy
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.diagnostics is not None
    assert error.diagnostics["timeout"] == 5.0
    assert error.diagnostics["method_id"] == RPCMethod.DELETE_ARTIFACT.value
    assert "public_error_failure" in error.diagnostics
    assert isinstance(error.__cause__, RPCTimeoutError)


@pytest.mark.asyncio
async def test_row_pre_dispatch_expiry_is_not_dispatched() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            NOTE_CREATE_DEF, NoteCreateInput("nb", "Title", "Body"), deadline=deadline
        )

    assert executor.calls == []
    assert caught.value.dispatched is False
    assert caught.value.outcome_unknown is False
    assert may_have_committed(caught.value) is False
