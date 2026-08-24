"""P9.3 chat: the five unary chat leaves dispatch as codec rows exactly as the handlers did.

``CHAT_GET_CONVERSATION``, ``CHAT_GET_HISTORY``, ``CHAT_DELETE_HISTORY``,
``CHAT_SAVE_NOTE`` and the input-keyed ``CHAT_CONFIGURE`` are
``encode → one native call → decode`` rows in ``_web/bindings/chat.py``.  These
tests pin the conversion oracles: the identical keyword set reaches the runtime
(including explicit ``False``/``None`` values), the input-keyed row selects
exactly the ledger's two natives, failure projection is byte-for-byte what
``invoke()`` produced for handler rows — including the chat family's request-URL
scrub — the ``dispatched`` marker reaches the neutral error, and the
conversation-id shape warnings survive the move into the codec.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CodecBinding, DeadlineMode, NativeChoice
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._records import (
    CHAT_CONFIGURE_DEF,
    CHAT_DELETE_HISTORY_DEF,
    CHAT_GET_CONVERSATION_DEF,
    CHAT_GET_HISTORY_DEF,
    CHAT_SAVE_NOTE_DEF,
    ChatConfigureAction,
    ChatConfigureInput,
    ChatDeleteHistoryInput,
    ChatGetConversationInput,
    ChatGetHistoryInput,
    ChatReferenceRecord,
    ChatSaveNoteInput,
)
from notebooklm._web import chat as chat_handlers
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import chat as chat_rows
from notebooklm._web.codec.chat import build_save_note_params
from notebooklm._web.failure_projection import _capture_public_failure
from notebooklm._web.policy import WEB_CALL_POLICY_BINDINGS
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import NetworkError, RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_NB = "nb-1"
_CONV = "conv-1"
_SETTINGS_RAW = [[None, None, None, None, None, None, None, [[3], [4]]]]


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


def _default_kwargs(notebook_id: str, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "source_path": f"/notebook/{notebook_id}",
        "allow_null": False,
        "_is_retry": False,
        "disable_internal_retries": False,
        "operation_variant": None,
        "read_timeout": None,
        "raise_on_null_status": False,
        "_retry_deadline": None,
    }
    kwargs.update(overrides)
    return kwargs


def _reference() -> ChatReferenceRecord:
    return ChatReferenceRecord(
        source_id="src-1",
        citation_number=1,
        cited_text="cited",
        start_char=0,
        end_char=5,
        chunk_id="chunk-1",
    )


def test_chat_rows_replace_their_handlers_in_the_registry_and_table() -> None:
    converted = {
        Operation.CHAT_GET_CONVERSATION: chat_rows.CHAT_GET_CONVERSATION,
        Operation.CHAT_GET_HISTORY: chat_rows.CHAT_GET_HISTORY,
        Operation.CHAT_DELETE_HISTORY: chat_rows.CHAT_DELETE_HISTORY,
        Operation.CHAT_CONFIGURE: chat_rows.CHAT_CONFIGURE,
        Operation.CHAT_SAVE_NOTE: chat_rows.CHAT_SAVE_NOTE,
    }
    assert {op: WEB_BINDING_ROWS[op] for op in converted} == converted
    for operation, row in converted.items():
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.handler_name is None
        assert binding.row is row
        assert isinstance(row, CodecBinding)
        assert row.definition is binding.definition
        assert row.deadline is DeadlineMode.INHERIT
        assert row.forward_disable_internal_retries is False
        assert row.map_error is None
        # The row's declared natives are exactly the reviewed ledger's natives.
        declared = {(choice.method, choice.variant) for choice in row.native.choices}
        ledger = {
            (native.method, native.variant)
            for native in WEB_CALL_POLICY_BINDINGS[operation].native_bindings
        }
        assert declared == ledger
    for name in (
        "_chat_get_conversation",
        "_chat_get_history",
        "_chat_delete_history",
        "_chat_configure",
        "_chat_save_note",
    ):
        assert not hasattr(WebRpcBackend, name)
        assert not hasattr(chat_handlers.ChatWebHandlers, name)
    # The two-phase streamed composite stays a handler (protocol custom row in P9.4).
    assert WEB_OPERATION_REGISTRY[Operation.CHAT_ASK].handler_name == "_chat_ask"
    assert hasattr(chat_handlers.ChatWebHandlers, "_chat_conversation_id")
    backend = build_web_backend(_RecordingExecutor())
    assert backend._bindings[Operation.CHAT_CONFIGURE] is chat_rows.CHAT_CONFIGURE


def test_configure_row_is_input_keyed_over_exactly_the_ledger_natives() -> None:
    spec = chat_rows.CHAT_CONFIGURE.native
    assert not spec.is_constant
    assert spec.choices == (
        NativeChoice(RPCMethod.GET_NOTEBOOK),
        NativeChoice(RPCMethod.RENAME_NOTEBOOK),
    )
    read = ChatConfigureInput(_NB, ChatConfigureAction.GET)
    write = ChatConfigureInput(
        _NB, ChatConfigureAction.SET, goal="default", response_length="longer"
    )
    assert spec.select(read) == NativeChoice(RPCMethod.GET_NOTEBOOK)
    assert spec.select(write) == NativeChoice(RPCMethod.RENAME_NOTEBOOK)


@pytest.mark.asyncio
async def test_chat_rows_forward_the_identical_keyword_set() -> None:
    executor = _RecordingExecutor(
        [[[_CONV]]],
        [[["q-turn", None, 1]]],
        None,
        _SETTINGS_RAW,
        None,
        [["note-1", ["Title", None, None, None, None, [1, 0]]]],
    )
    backend = build_web_backend(executor)

    conversation = await backend.invoke(
        CHAT_GET_CONVERSATION_DEF, ChatGetConversationInput(_NB), deadline=None
    )
    history = await backend.invoke(
        CHAT_GET_HISTORY_DEF, ChatGetHistoryInput(_NB, _CONV, limit=3), deadline=None
    )
    deleted = await backend.invoke(
        CHAT_DELETE_HISTORY_DEF, ChatDeleteHistoryInput(_NB, _CONV), deadline=None
    )
    settings = await backend.invoke(
        CHAT_CONFIGURE_DEF, ChatConfigureInput(_NB, ChatConfigureAction.GET), deadline=None
    )
    configured = await backend.invoke(
        CHAT_CONFIGURE_DEF,
        ChatConfigureInput(
            _NB,
            ChatConfigureAction.SET,
            goal="custom",
            response_length="shorter",
            custom_prompt="be brief",
        ),
        deadline=None,
    )
    saved = await backend.invoke(
        CHAT_SAVE_NOTE_DEF,
        ChatSaveNoteInput(_NB, "answer [1]", (_reference(),), "Title"),
        deadline=None,
    )

    assert conversation.conversation_id == _CONV
    assert len(history.turns) == 1
    assert deleted is not None
    assert settings.settings is not None
    assert (settings.settings.goal, settings.settings.response_length) == (
        "learning_guide",
        "longer",
    )
    assert configured.settings is None
    assert saved.note.id == "note-1"
    assert saved.note.notebook_id == _NB
    assert saved.note.content == "answer [1]"

    get_conversation, get_history, delete, get_settings, set_settings, save = executor.calls
    assert get_conversation.method is RPCMethod.GET_LAST_CONVERSATION_ID
    assert get_conversation.params == [[], None, _NB, 1]
    assert get_conversation.kwargs == _default_kwargs(_NB)
    assert get_history.method is RPCMethod.GET_CONVERSATION_TURNS
    assert get_history.params == [[], None, None, _CONV, 3]
    assert get_history.kwargs == _default_kwargs(_NB)
    assert delete.method is RPCMethod.DELETE_CONVERSATION
    assert delete.params == [[], _CONV, None, 1]
    assert delete.kwargs == _default_kwargs(_NB)
    assert get_settings.method is RPCMethod.GET_NOTEBOOK
    assert get_settings.kwargs == _default_kwargs(_NB)
    assert set_settings.method is RPCMethod.RENAME_NOTEBOOK
    assert set_settings.params == [
        _NB,
        [[None, None, None, None, None, None, None, [[2, "be brief"], [5]]]],
    ]
    assert set_settings.kwargs == _default_kwargs(_NB, allow_null=True)
    assert save.method is RPCMethod.CREATE_NOTE
    assert save.params == build_save_note_params(_NB, "answer [1]", (_reference(),), "Title")
    assert save.kwargs == _default_kwargs(_NB, operation_variant="saved_from_chat")


@pytest.mark.asyncio
async def test_get_conversation_row_keeps_the_shape_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = _RecordingExecutor([["unexpected"]], "not-a-list", [], None)
    backend = build_web_backend(executor)

    results = []
    with caplog.at_level(logging.WARNING, logger="notebooklm._chat.api"):
        for _ in range(4):
            results.append(
                await backend.invoke(
                    CHAT_GET_CONVERSATION_DEF, ChatGetConversationInput(_NB), deadline=None
                )
            )

    assert [result.conversation_id for result in results] == [None, None, None, None]
    messages = [record.getMessage() for record in caplog.records]
    # Same three warnings the handler emitted (an empty list still takes the
    # "non-list, non-empty" branch — a pre-existing quirk, preserved verbatim);
    # a ``None`` response is silent.
    assert len(messages) == 3
    assert "unexpected response shape" in messages[0]
    assert "non-list, non-empty response" in messages[1] and "type=str" in messages[1]
    assert "non-list, non-empty response" in messages[2] and "type=list" in messages[2]
    assert all(record.name == "notebooklm._chat.api" for record in caplog.records)


@pytest.mark.asyncio
async def test_codec_row_read_timeout_is_clamped_to_the_shared_deadline() -> None:
    executor = _RecordingExecutor([[[_CONV]]])
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    await backend.invoke(
        CHAT_GET_CONVERSATION_DEF, ChatGetConversationInput(_NB), deadline=deadline
    )

    (call,) = executor.calls
    assert call.kwargs["read_timeout"] == pytest.approx(4.0)
    assert call.kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_chat_row_server_error_translates_with_the_chat_url_scrub_and_is_dispatched() -> None:
    raw = ServerError("boom", method_id=RPCMethod.DELETE_CONVERSATION.value)
    executor = _RecordingExecutor(raw)
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            CHAT_DELETE_HISTORY_DEF, ChatDeleteHistoryInput(_NB, _CONV), deadline=None
        )

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.CHAT_DELETE_HISTORY
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.DELETE_CONVERSATION.value
    # Chat operations project their public failure with request URLs scrubbed —
    # the same projection ``invoke()`` applied when the handler owned the call.
    assert error.diagnostics["public_error_failure"] == _capture_public_failure(
        raw, operation=Operation.CHAT_DELETE_HISTORY, scrub_request_urls=True
    )
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.__cause__ is raw


@pytest.mark.asyncio
async def test_chat_row_network_error_scrubs_request_urls_from_the_public_projection() -> None:
    # The scrub acts on the httpx request carried by the wrapped transport
    # failure: the query string (rpcids, f.sid, …) is dropped from its URL.
    request = httpx.Request(
        "POST",
        "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute?rpcids=x&f.sid=1",
    )
    raw = NetworkError(
        "connect failed", original_error=httpx.ConnectError("refused", request=request)
    )
    executor = _RecordingExecutor(raw)
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(CHAT_GET_HISTORY_DEF, ChatGetHistoryInput(_NB, _CONV), deadline=None)

    error = caught.value
    assert error.reason is BackendErrorReason.NETWORK
    assert error.diagnostics is not None
    projected = error.diagnostics["public_error_failure"]
    assert projected == _capture_public_failure(
        raw, operation=Operation.CHAT_GET_HISTORY, scrub_request_urls=True
    )
    assert projected != _capture_public_failure(
        raw, operation=Operation.CHAT_GET_HISTORY, scrub_request_urls=False
    )
    assert error.dispatched is True


@pytest.mark.asyncio
async def test_codec_row_timeout_after_expiry_becomes_a_dispatched_deadline_error() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(
        RPCTimeoutError("slow", method_id=RPCMethod.RENAME_NOTEBOOK.value)
    )
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            CHAT_CONFIGURE_DEF,
            ChatConfigureInput(
                _NB, ChatConfigureAction.SET, goal="default", response_length="default"
            ),
            deadline=deadline,
        )

    error = caught.value
    assert error.operation is Operation.CHAT_CONFIGURE
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.outcome_unknown is True  # MUTATION policy
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.diagnostics is not None
    assert error.diagnostics["timeout"] == 5.0
    assert error.diagnostics["method_id"] == RPCMethod.RENAME_NOTEBOOK.value
    assert "public_error_failure" in error.diagnostics
    assert isinstance(error.__cause__, RPCTimeoutError)


@pytest.mark.asyncio
async def test_codec_row_pre_dispatch_expiry_is_not_dispatched() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            CHAT_SAVE_NOTE_DEF,
            ChatSaveNoteInput(_NB, "answer [1]", (_reference(),), "Title"),
            deadline=deadline,
        )

    assert executor.calls == []
    assert caught.value.dispatched is False
    assert may_have_committed(caught.value) is False
