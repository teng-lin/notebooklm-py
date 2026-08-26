"""P9 binding core: row-only construction audit and typed dispatch."""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path
from typing import Any

import pytest

from notebooklm._backend import BackendContractError, BackendDeadlineExceededError
from notebooklm._binding import (
    BindingAuditError,
    BindingTable,
    CodecBinding,
    CodecPayload,
    CustomBinding,
    DeadlineMode,
    NativeCallSpec,
    OperationDisposition,
    RpcNative,
    StreamNative,
    StreamRequestPayload,
    audit_bindings,
    invoke_binding,
)
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._records import (
    ARTIFACT_GENERATE_AUDIO_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NotebookListInput,
    NotebookListResult,
)
from notebooklm._web import registry
from notebooklm._web.backend import _build_binding_table
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SUPPORTED_OPERATIONS
from tests._fixtures.web_backend import build_web_backend

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Executor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[Any, list[Any], dict[str, Any]]] = []

    async def rpc_call(self, method: Any, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append((method, params, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _FakeTransport:
    """Minimal ``Transport``: assembles a tuple and records what it dispatched."""

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[Any, ...]] = []
        self.deadlines: list[RuntimeDeadline | None] = []

    def assemble(
        self, definition, native, payload, *, retry_flag, deadline, outcome_unknown_on_expiry=False
    ):
        return (definition.key, native, payload, retry_flag, deadline, outcome_unknown_on_expiry)

    async def call(self, request, *, deadline):
        self.requests.append(request)
        self.deadlines.append(deadline)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def assemble_stream(self, definition, native, payload, *, deadline):
        return (definition.key, native, payload, deadline)

    async def stream(self, request, *, deadline):
        return await self.call(request, deadline=deadline)


def _list_codec_row() -> CodecBinding[NotebookListInput, NotebookListResult, str]:
    return CodecBinding(
        definition=NOTEBOOK_LIST_DEF,
        encode=lambda value: CodecPayload(params=[]),
        decode=lambda value, raw: NotebookListResult(notebooks=()),
        native=NativeCallSpec.constant("LIST_NOTEBOOKS"),
    )


# --- table + audit -----------------------------------------------------------


def test_registry_dispositions_are_three_way_and_supported_set_is_direct() -> None:
    dispositions = {binding.disposition for binding in WEB_OPERATION_REGISTRY.values()}
    assert dispositions == {
        OperationDisposition.SUPPORTED_DIRECT,
        OperationDisposition.SERVICE_OWNED,
        OperationDisposition.UNSUPPORTED,
    }
    direct = frozenset(
        operation
        for operation, binding in WEB_OPERATION_REGISTRY.items()
        if binding.disposition is OperationDisposition.SUPPORTED_DIRECT
    )
    assert direct == WEB_SUPPORTED_OPERATIONS
    assert len(WEB_SUPPORTED_OPERATIONS) == registry._EXPECTED_SUPPORTED_COUNT


def test_audit_rejects_missing_and_extra_rows_in_both_directions() -> None:
    row = _list_codec_row()
    with pytest.raises(BindingAuditError, match="without a row: notebook.get"):
        audit_bindings(
            BindingTable({Operation.NOTEBOOK_LIST: row}),
            frozenset({Operation.NOTEBOOK_LIST, Operation.NOTEBOOK_GET}),
        )
    with pytest.raises(BindingAuditError, match="not supported: notebook.list"):
        audit_bindings(BindingTable({Operation.NOTEBOOK_LIST: row}), frozenset())
    with pytest.raises(BindingAuditError, match="binds definition notebook.list"):
        audit_bindings(
            BindingTable({Operation.NOTEBOOK_GET: row}),
            frozenset({Operation.NOTEBOOK_GET}),
        )
    audit_bindings(
        BindingTable({Operation.NOTEBOOK_LIST: row}), frozenset({Operation.NOTEBOOK_LIST})
    )


def test_table_counts_each_row_kind_and_custom_category() -> None:
    codec: CodecBinding[Any, Any, str] = CodecBinding(
        definition=NOTEBOOK_GET_DEF,
        encode=lambda value: CodecPayload(params=[]),
        decode=lambda value, raw: raw,
        native=NativeCallSpec.constant("GET"),
    )

    async def custom(value, deadline, invoke):
        return None

    custom_row: CustomBinding[Any, Any, str] = CustomBinding(
        definition=ARTIFACT_GENERATE_AUDIO_DEF,
        handler=custom,
        native=(NativeCallSpec.constant("CREATE", key="create"),),
        justification="Input-defaulting generate member kept adapter-owned in P9.",
        category="deferred-product",
    )
    table = BindingTable(
        {
            Operation.NOTEBOOK_GET: codec,
            Operation.ARTIFACT_GENERATE_AUDIO: custom_row,
        }
    )
    assert (table.codec_count, table.custom_count) == (1, 1)
    assert dict(table.custom_count_by_category()) == {
        "protocol": 0,
        "compatibility": 0,
        "deferred-product": 1,
    }
    assert repr(table) == "BindingTable(rows=2, codec=1, custom=1)"
    with pytest.raises(TypeError):
        table._rows[Operation.NOTE_LIST] = codec  # type: ignore[index]


def test_custom_binding_validates_category_justification_and_spec_keys() -> None:
    async def custom(value, deadline, invoke):
        return None

    with pytest.raises(ValueError, match="category"):
        CustomBinding(
            definition=NOTEBOOK_GET_DEF,
            handler=custom,
            native=(NativeCallSpec.constant("GET", key="get"),),
            justification="x",
            category="other",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="justification"):
        CustomBinding(
            definition=NOTEBOOK_GET_DEF,
            handler=custom,
            native=(NativeCallSpec.constant("GET", key="get"),),
            justification="  ",
            category="protocol",
        )
    with pytest.raises(ValueError, match="unique, non-empty keys"):
        CustomBinding(
            definition=NOTEBOOK_GET_DEF,
            handler=custom,
            native=(NativeCallSpec.constant("GET"),),
            justification="x",
            category="protocol",
        )


def test_native_call_spec_is_constant_or_finite_input_keyed() -> None:
    with pytest.raises(ValueError):
        NativeCallSpec(choices=())
    with pytest.raises(ValueError):
        NativeCallSpec(choices=(RpcNative("A"), RpcNative("B")))
    constant = NativeCallSpec.constant("A", "variant")
    assert constant.is_constant and constant.select(object()) == RpcNative("A", "variant")
    keyed = NativeCallSpec.keyed(
        lambda value: RpcNative("FAST") if value == "fast" else RpcNative("OTHER"),
        RpcNative("FAST"),
        RpcNative("DEEP"),
    )
    assert keyed.select("fast") == RpcNative("FAST")
    with pytest.raises(BackendContractError, match="undeclared native"):
        keyed.select("deep")


# --- invoke_binding ----------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_binding_runs_codec_rows_through_the_transport() -> None:
    transport = _FakeTransport({"raw": True})
    row: CodecBinding[Any, Any, str] = CodecBinding(
        definition=NOTEBOOK_GET_DEF,
        encode=lambda value: CodecPayload(params=[value], allow_null=True),
        decode=lambda value, raw: (value, raw),
        native=NativeCallSpec.constant("GET_NOTEBOOK", "with-sources"),
        forward_disable_internal_retries=True,
    )
    deadline = RuntimeDeadline.start(5.0)
    result = await invoke_binding(
        BindingTable({Operation.NOTEBOOK_GET: row}),
        transport,
        None,
        NOTEBOOK_GET_DEF,
        "nb",
        deadline=deadline,
    )
    assert result == ("nb", {"raw": True})
    assert transport.requests == [
        (
            Operation.NOTEBOOK_GET,
            RpcNative("GET_NOTEBOOK", "with-sources"),
            CodecPayload(params=["nb"], allow_null=True),
            True,
            deadline,
            False,
        )
    ]
    assert transport.deadlines == [deadline]


@pytest.mark.asyncio
async def test_codec_rows_can_ignore_the_deadline_and_map_errors_only_on_failure() -> None:
    mapped_calls: list[tuple[Any, Exception, RpcNative[str]]] = []

    def map_error(value, raw, native):
        mapped_calls.append((value, raw, native))
        return BackendContractError("mapped", operation=Operation.NOTEBOOK_GET)

    row: CodecBinding[Any, Any, str] = CodecBinding(
        definition=NOTEBOOK_GET_DEF,
        encode=lambda value: CodecPayload(params=[]),
        decode=lambda value, raw: raw,
        native=NativeCallSpec.constant("GET"),
        deadline=DeadlineMode.IGNORE,
        map_error=map_error,
    )
    table = BindingTable({Operation.NOTEBOOK_GET: row})
    transport = _FakeTransport("ok", RuntimeError("boom"))
    deadline = RuntimeDeadline.start(5.0)
    assert (
        await invoke_binding(table, transport, None, NOTEBOOK_GET_DEF, 1, deadline=deadline) == "ok"
    )
    assert transport.deadlines == [None]
    assert mapped_calls == []
    with pytest.raises(BackendContractError, match="mapped") as info:
        await invoke_binding(table, transport, None, NOTEBOOK_GET_DEF, 2, deadline=deadline)
    assert isinstance(info.value.__cause__, RuntimeError)
    assert getattr(info.value.__cause__, "binding_native", None) == RpcNative("GET")
    assert mapped_calls[0][0] == 2


@pytest.mark.asyncio
async def test_invoke_binding_owns_the_single_pre_dispatch_expiry_check() -> None:
    """One check, applied per row kind after ``DeadlineMode`` and native selection."""
    expired = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)
    keyed: CodecBinding[Any, Any, str] = CodecBinding(
        definition=NOTEBOOK_GET_DEF,
        encode=lambda value: CodecPayload(params=[]),
        decode=lambda value, raw: raw,
        native=NativeCallSpec.keyed(
            lambda value: RpcNative(value),
            RpcNative("GET_ONE"),
            RpcNative("GET_TWO"),
        ),
    )
    transport = _FakeTransport()

    # A keyed codec row resolves its native from the input first, so the failure
    # names the native it blocked.
    with pytest.raises(BackendDeadlineExceededError) as codec_failure:
        await invoke_binding(
            BindingTable({Operation.NOTEBOOK_GET: keyed}),
            transport,
            None,
            NOTEBOOK_GET_DEF,
            "GET_TWO",
            deadline=expired,
        )
    assert codec_failure.value.diagnostics == {
        "timeout": 5.0,
        "remaining": 0.0,
        "timeout_seconds": 5.0,
        "method_id": "GET_TWO",
    }

    # A row that ignores the deadline never reaches the check.
    ignoring: CodecBinding[Any, Any, str] = CodecBinding(
        definition=NOTEBOOK_GET_DEF,
        encode=lambda value: CodecPayload(params=[]),
        decode=lambda value, raw: raw,
        native=NativeCallSpec.constant("GET"),
        deadline=DeadlineMode.IGNORE,
    )
    assert (
        await invoke_binding(
            BindingTable({Operation.NOTEBOOK_GET: ignoring}),
            _FakeTransport("ok"),
            None,
            NOTEBOOK_GET_DEF,
            1,
            deadline=expired,
        )
        == "ok"
    )

    # A custom row resolves no native here, so its failure names none.
    async def handler(value, deadline, invoke):  # pragma: no cover - never reached
        raise AssertionError("an expired custom row never enters its handler")

    custom: CustomBinding[Any, Any, str] = CustomBinding(
        definition=NOTEBOOK_GET_DEF,
        handler=handler,
        native=(NativeCallSpec.constant("GET", key="get"),),
        justification="The wire forces a fetch after the streamed answer.",
        category="protocol",
    )
    with pytest.raises(BackendDeadlineExceededError) as custom_failure:
        await invoke_binding(
            BindingTable({Operation.NOTEBOOK_GET: custom}),
            transport,
            None,
            NOTEBOOK_GET_DEF,
            1,
            deadline=expired,
        )
    assert custom_failure.value.diagnostics == {
        "timeout": 5.0,
        "remaining": 0.0,
        "timeout_seconds": 5.0,
    }
    assert transport.requests == []


@pytest.mark.asyncio
async def test_codec_rows_require_a_transport() -> None:
    row: CodecBinding[Any, Any, str] = CodecBinding(
        definition=NOTEBOOK_GET_DEF,
        encode=lambda value: CodecPayload(params=[]),
        decode=lambda value, raw: raw,
        native=NativeCallSpec.constant("GET"),
    )
    with pytest.raises(BackendContractError, match="requires a transport"):
        await invoke_binding(
            BindingTable({Operation.NOTEBOOK_GET: row}),
            None,
            None,
            NOTEBOOK_GET_DEF,
            1,
            deadline=None,
        )


@pytest.mark.asyncio
async def test_custom_rows_only_reach_their_declared_specs_and_tag_failures() -> None:
    seen: list[str] = []

    async def handler(value, deadline, invoke):
        seen.append(value)
        first = await invoke.call("list", CodecPayload(params=["a"]), deadline=deadline)
        try:
            await invoke.call("undeclared", CodecPayload(params=[]), deadline=deadline)
        except BackendContractError as exc:
            seen.append(str(exc))
        try:
            await invoke.call("fails", CodecPayload(params=[]), deadline=deadline)
        except RuntimeError as exc:
            seen.append(repr(getattr(exc, "binding_native", None)))
        return first

    row: CustomBinding[Any, Any, str] = CustomBinding(
        definition=NOTEBOOK_GET_DEF,
        handler=handler,
        native=(
            NativeCallSpec.constant("LIST", key="list"),
            NativeCallSpec.constant("GET", key="fails"),
        ),
        justification="The wire forces a fetch after the streamed answer.",
        category="protocol",
    )
    transport = _FakeTransport("listed", RuntimeError("call failed"))
    result = await invoke_binding(
        BindingTable({Operation.NOTEBOOK_GET: row}),
        transport,
        None,
        NOTEBOOK_GET_DEF,
        "v",
        deadline=None,
    )
    assert result == "listed"
    assert seen == [
        "v",
        "notebook.get declares no native spec 'undeclared'",
        "RpcNative(method='GET', variant=None)",
    ]
    assert [request[1] for request in transport.requests] == [
        RpcNative("LIST"),
        RpcNative("GET"),
    ]


@pytest.mark.asyncio
async def test_a_custom_row_cannot_call_a_streamed_native() -> None:
    """``RowInvoker`` has no stream verb; a streamed spec fails closed on ``call``."""

    async def handler(value, deadline, invoke):
        return await invoke.call("streamed", CodecPayload(params=[]), deadline=deadline)

    row: CustomBinding[Any, Any, str] = CustomBinding(
        definition=NOTEBOOK_GET_DEF,
        handler=handler,
        native=(NativeCallSpec.streamed("fake.stream", key="streamed"),),
        justification="The wire forces a fetch after the streamed answer.",
        category="protocol",
    )

    with pytest.raises(BackendContractError, match="streamed native"):
        await invoke_binding(
            BindingTable({Operation.NOTEBOOK_GET: row}),
            _FakeTransport(),
            None,
            NOTEBOOK_GET_DEF,
            "v",
            deadline=None,
        )


@pytest.mark.asyncio
async def test_a_streamed_codec_row_dispatches_through_the_transport_stream_verb() -> None:
    """The spec picks the verb: a ``StreamNative`` assembles and streams."""
    row: CodecBinding[Any, Any, str] = CodecBinding(
        definition=NOTEBOOK_GET_DEF,
        encode=lambda value: StreamRequestPayload(data=f"encoded-{value}"),
        decode=lambda value, raw: raw,
        native=NativeCallSpec.streamed("fake.stream"),
    )
    transport = _FakeTransport("streamed")

    result = await invoke_binding(
        BindingTable({Operation.NOTEBOOK_GET: row}),
        transport,
        None,
        NOTEBOOK_GET_DEF,
        "v",
        deadline=None,
    )

    assert result == "streamed"
    assert transport.requests == [
        (
            Operation.NOTEBOOK_GET,
            StreamNative("fake.stream"),
            StreamRequestPayload("encoded-v"),
            None,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("native", "payload", "message"),
    [
        (NativeCallSpec.streamed("fake.stream"), CodecPayload(params=[]), "streams a native"),
        (NativeCallSpec.constant("LIST"), StreamRequestPayload(data="x"), "calls a native"),
    ],
)
async def test_a_codec_row_pairs_its_payload_kind_with_its_native_kind(
    native, payload, message
) -> None:
    row: CodecBinding[Any, Any, str] = CodecBinding(
        definition=NOTEBOOK_GET_DEF,
        encode=lambda value: payload,
        decode=lambda value, raw: raw,
        native=native,
    )

    with pytest.raises(BackendContractError, match=message):
        await invoke_binding(
            BindingTable({Operation.NOTEBOOK_GET: row}),
            _FakeTransport("unused"),
            None,
            NOTEBOOK_GET_DEF,
            "v",
            deadline=None,
        )


def test_a_streamed_native_carries_a_non_empty_label() -> None:
    with pytest.raises(ValueError, match="non-empty label"):
        StreamNative("")


# --- construction-time row audit ---------------------------------------------


def test_backend_audits_every_row_at_construction() -> None:
    backend = build_web_backend(_Executor())
    table = backend._bindings
    assert set(table) == WEB_SUPPORTED_OPERATIONS
    row_backed = {op for op, binding in WEB_OPERATION_REGISTRY.items() if binding.row is not None}
    assert table.codec_count + table.custom_count == len(row_backed) == len(WEB_BINDING_ROWS)
    assert row_backed == set(WEB_BINDING_ROWS)
    assert table.custom_count == sum(
        1 for row in WEB_BINDING_ROWS.values() if isinstance(row, CustomBinding)
    )
    assert row_backed == WEB_SUPPORTED_OPERATIONS
    assert all(isinstance(table[op], (CodecBinding, CustomBinding)) for op in row_backed)
    assert table[Operation.SETTINGS_GET] is WEB_OPERATION_REGISTRY[Operation.SETTINGS_GET].row
    assert Operation.NOTEBOOK_CREATE not in table
    assert isinstance(table[Operation.NOTEBOOK_ALLOCATE], CodecBinding)
    assert "_bindings" in vars(backend)


def test_table_missing_a_supported_row_is_rejected_by_the_construction_audit() -> None:
    narrowed = dict(WEB_OPERATION_REGISTRY)
    narrowed[Operation.NOTEBOOK_LIST] = registry.WebOperationBinding(
        definition=None, unsupported_reason="dropped"
    )
    with pytest.raises(BackendContractError, match="without a row: notebook.list"):
        _build_binding_table(registry=narrowed)
    with pytest.raises(BackendContractError, match="not supported: notebook.list"):
        _build_binding_table(supported=WEB_SUPPORTED_OPERATIONS - {Operation.NOTEBOOK_LIST})


@pytest.mark.asyncio
async def test_invoke_dispatches_through_the_table_without_getattr() -> None:
    executor = _Executor([[]])
    backend = build_web_backend(executor)
    calls: list[NotebookListInput] = []

    async def replacement(value, deadline, invoke):
        del deadline, invoke
        calls.append(value)
        return NotebookListResult(notebooks=())

    object.__setattr__(
        backend,
        "_bindings",
        BindingTable(
            {
                **backend._bindings,
                Operation.NOTEBOOK_LIST: CustomBinding(
                    definition=NOTEBOOK_LIST_DEF,
                    handler=replacement,
                    native=(),
                    justification="Synthetic row proves dispatch does not use attribute lookup.",
                    category="compatibility",
                ),
            }
        ),
    )
    result = await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)
    assert result == NotebookListResult(notebooks=())
    assert calls == [NotebookListInput()]
    assert executor.calls == []


# --- typed dispatch ----------------------------------------------------------

_MYPY_SNIPPET = """
from notebooklm._binding import CodecBinding, CodecPayload, NativeCallSpec
from notebooklm._records import (
    ARTIFACT_GENERATE_AUDIO_DEF,
    AudioGenerateInput,
    AudioGenerateResult,
    VideoGenerateInput,
)


def encode_audio(value: AudioGenerateInput) -> CodecPayload:
    return CodecPayload(params=[])


def encode_video(value: VideoGenerateInput) -> CodecPayload:
    return CodecPayload(params=[])


def decode_audio(value: AudioGenerateInput, raw: object) -> AudioGenerateResult:
    raise NotImplementedError


good: CodecBinding[AudioGenerateInput, AudioGenerateResult, str] = CodecBinding(
    definition=ARTIFACT_GENERATE_AUDIO_DEF,
    encode=encode_audio,
    decode=decode_audio,
    native=NativeCallSpec.constant("CREATE_ARTIFACT"),
)
{bad_line}
"""


@pytest.mark.xdist_group("mypy_api")
@pytest.mark.timeout(300)
def test_dispatch_is_type_checked_by_mypy(tmp_path: Path) -> None:
    if importlib.util.find_spec("mypy") is None:
        pytest.skip("mypy is not installed")
    from mypy import api as mypy_api

    def run(bad_line: str) -> tuple[str, int]:
        snippet = tmp_path / "snippet.py"
        snippet.write_text(textwrap.dedent(_MYPY_SNIPPET.format(bad_line=bad_line)))
        stdout, _stderr, status = mypy_api.run(
            [
                "--config-file",
                str(REPO_ROOT / "pyproject.toml"),
                "--no-incremental",
                "--no-error-summary",
                str(snippet),
            ]
        )
        return stdout, status

    clean_output, clean_status = run("")
    assert clean_status == 0, clean_output
    bad_output, bad_status = run(
        """bad: CodecBinding[AudioGenerateInput, AudioGenerateResult, str] = CodecBinding(
    definition=ARTIFACT_GENERATE_AUDIO_DEF,
    encode=encode_video,
    decode=decode_audio,
    native=NativeCallSpec.constant(\"CREATE_ARTIFACT\"),
)"""
    )
    assert bad_status != 0
    assert "snippet.py:" in bad_output and "error:" in bad_output, bad_output
