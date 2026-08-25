"""P9.0: neutral binding core, construction-time handler resolution, typed dispatch."""

from __future__ import annotations

import importlib.util
import textwrap
from pathlib import Path
from typing import Any

import pytest

from notebooklm._backend import BackendContractError
from notebooklm._binding import (
    BindingAuditError,
    BindingTable,
    CodecBinding,
    CodecPayload,
    CustomBinding,
    DeadlineMode,
    NativeCallSpec,
    NativeChoice,
    OperationDisposition,
    ResolvedHandlerBinding,
    StreamPayload,
    StreamSpec,
    audit_bindings,
    bind,
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
from notebooklm._web.backend import WebRpcBackend, _resolve_handler_bindings
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

    def assemble_stream(self, definition, spec, payload, *, deadline):
        return (definition.key, spec, payload, deadline)

    async def stream(self, request, *, deadline):
        return await self.call(request, deadline=deadline)


async def _list_handler(value: NotebookListInput, *, deadline: RuntimeDeadline | None):
    del value, deadline
    return NotebookListResult(notebooks=())


# --- table + audit -----------------------------------------------------------


def test_registry_dispositions_are_three_way_and_supported_set_is_direct() -> None:
    dispositions = {binding.disposition for binding in WEB_OPERATION_REGISTRY.values()}
    assert dispositions == {
        OperationDisposition.SUPPORTED_DIRECT,
        OperationDisposition.UNSUPPORTED,
    }
    direct = frozenset(
        operation
        for operation, binding in WEB_OPERATION_REGISTRY.items()
        if binding.disposition is OperationDisposition.SUPPORTED_DIRECT
    )
    assert direct == WEB_SUPPORTED_OPERATIONS
    assert len(WEB_SUPPORTED_OPERATIONS) == 82


def test_audit_rejects_missing_and_extra_rows_in_both_directions() -> None:
    row = bind(NOTEBOOK_LIST_DEF, _list_handler)
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
            Operation.NOTEBOOK_LIST: bind(NOTEBOOK_LIST_DEF, _list_handler),
            Operation.NOTEBOOK_GET: codec,
            Operation.ARTIFACT_GENERATE_AUDIO: custom_row,
        }
    )
    assert (table.resolved_handler_count, table.codec_count, table.custom_count) == (1, 1, 1)
    assert dict(table.custom_count_by_category()) == {
        "protocol": 0,
        "compatibility": 0,
        "deferred-product": 1,
    }
    assert repr(table) == "BindingTable(rows=3, resolved_handlers=1, codec=1, custom=1)"
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
        NativeCallSpec(choices=(NativeChoice("A"), NativeChoice("B")))
    constant = NativeCallSpec.constant("A", "variant")
    assert constant.is_constant and constant.select(object()) == NativeChoice("A", "variant")
    keyed = NativeCallSpec.keyed(
        lambda value: NativeChoice("FAST") if value == "fast" else NativeChoice("OTHER"),
        NativeChoice("FAST"),
        NativeChoice("DEEP"),
    )
    assert keyed.select("fast") == NativeChoice("FAST")
    with pytest.raises(BackendContractError, match="undeclared native"):
        keyed.select("deep")


# --- invoke_binding ----------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_binding_dispatches_resolved_handler_rows_without_transport() -> None:
    table = BindingTable({Operation.NOTEBOOK_LIST: bind(NOTEBOOK_LIST_DEF, _list_handler)})
    result = await invoke_binding(
        table, None, None, NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None
    )
    assert result == NotebookListResult(notebooks=())
    with pytest.raises(BackendContractError, match="has no binding row"):
        await invoke_binding(table, None, None, NOTEBOOK_GET_DEF, object(), deadline=None)


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
            NativeChoice("GET_NOTEBOOK", "with-sources"),
            CodecPayload(params=["nb"], allow_null=True),
            True,
            deadline,
            False,
        )
    ]
    assert transport.deadlines == [deadline]


@pytest.mark.asyncio
async def test_codec_rows_can_ignore_the_deadline_and_map_errors_only_on_failure() -> None:
    mapped_calls: list[tuple[Any, Exception, NativeChoice[str]]] = []

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
    assert getattr(info.value.__cause__, "binding_native", None) == NativeChoice("GET")
    assert mapped_calls[0][0] == 2


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
            await invoke.stream(
                "undeclared-stream", StreamPayload(build_request=lambda s: s), deadline=deadline
            )
        except BackendContractError as exc:
            seen.append(str(exc))
        try:
            await invoke.stream(
                "stream", StreamPayload(build_request=lambda s: s), deadline=deadline
            )
        except RuntimeError as exc:
            seen.append(repr(getattr(exc, "binding_native", None)))
        return first

    row: CustomBinding[Any, Any, str] = CustomBinding(
        definition=NOTEBOOK_GET_DEF,
        handler=handler,
        native=(NativeCallSpec.constant("LIST", key="list"),),
        streams=(StreamSpec(key="stream", label="fake.stream"),),
        justification="The wire forces a fetch after the streamed answer.",
        category="protocol",
    )
    transport = _FakeTransport("listed", RuntimeError("stream failed"))
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
        "notebook.get declares no stream spec 'undeclared-stream'",
        "StreamSpec(key='stream', label='fake.stream')",
    ]
    assert [request[1] for request in transport.requests] == [
        NativeChoice("LIST"),
        StreamSpec(key="stream", label="fake.stream"),
    ]


# --- construction-time resolution --------------------------------------------


def _handler_backed() -> tuple[Operation, str]:
    """One operation the registry still resolves by handler name (P9.4b shrinks the set)."""
    return next(
        (operation, binding.handler_name)
        for operation, binding in WEB_OPERATION_REGISTRY.items()
        if binding.handler_name is not None
    )


def test_backend_resolves_every_handler_at_construction() -> None:
    backend = build_web_backend(_Executor())
    table = backend._bindings
    assert set(table) == WEB_SUPPORTED_OPERATIONS
    row_backed = {op for op, binding in WEB_OPERATION_REGISTRY.items() if binding.row is not None}
    assert table.resolved_handler_count == 82 - len(row_backed)
    # Derived, not a literal: every P9.3/P9.4 domain PR grows the row set.
    assert table.codec_count + table.custom_count == len(row_backed) == len(WEB_BINDING_ROWS)
    assert row_backed == set(WEB_BINDING_ROWS)
    assert table.custom_count == sum(
        1 for row in WEB_BINDING_ROWS.values() if isinstance(row, CustomBinding)
    )
    assert all(isinstance(table[op], (CodecBinding, CustomBinding)) for op in row_backed)
    assert table[Operation.SETTINGS_GET] is WEB_OPERATION_REGISTRY[Operation.SETTINGS_GET].row
    handler_operation, handler_name = _handler_backed()
    row = table[handler_operation]
    assert isinstance(row, ResolvedHandlerBinding)
    assert row.handler == getattr(backend, handler_name)
    assert "_bindings" in vars(backend)


def test_misnamed_handler_fails_at_resolution_not_first_invocation() -> None:
    backend = build_web_backend(_Executor())
    handler_operation, handler_name = _handler_backed()
    broken = dict(WEB_OPERATION_REGISTRY)
    broken[handler_operation] = registry.WebOperationBinding(
        definition=WEB_OPERATION_REGISTRY[handler_operation].definition,
        handler_name=f"{handler_name}_renamed",
        unsupported_reason=None,
    )
    with pytest.raises(
        BackendContractError, match=f"names missing web handler '{handler_name}_renamed'"
    ):
        _resolve_handler_bindings(backend, registry=broken)


def test_missing_handler_fails_at_construction() -> None:
    handler_operation, handler_name = _handler_backed()
    Incomplete = type("Incomplete", (WebRpcBackend,), {handler_name: None})

    with pytest.raises(
        BackendContractError, match=f"{handler_operation.value} names missing web handler"
    ):
        Incomplete(_Executor(), transport_factory=lambda **kwargs: object())  # type: ignore[arg-type]


def test_table_missing_a_supported_row_is_rejected_by_the_construction_audit() -> None:
    backend = build_web_backend(_Executor())
    narrowed = dict(WEB_OPERATION_REGISTRY)
    handler_operation, _handler_name = _handler_backed()
    narrowed[handler_operation] = registry.WebOperationBinding(
        definition=None, handler_name=None, unsupported_reason="dropped"
    )
    with pytest.raises(BackendContractError, match=f"without a row: {handler_operation.value}"):
        _resolve_handler_bindings(backend, registry=narrowed)
    with pytest.raises(BackendContractError, match="not supported: notebook.list"):
        _resolve_handler_bindings(
            backend, supported=WEB_SUPPORTED_OPERATIONS - {Operation.NOTEBOOK_LIST}
        )


@pytest.mark.asyncio
async def test_invoke_dispatches_through_the_table_without_getattr() -> None:
    executor = _Executor([[]])
    backend = build_web_backend(executor)
    calls: list[NotebookListInput] = []

    async def replacement(value, *, deadline):
        calls.append(value)
        return NotebookListResult(notebooks=())

    object.__setattr__(
        backend,
        "_bindings",
        BindingTable(
            {**backend._bindings, Operation.NOTEBOOK_LIST: bind(NOTEBOOK_LIST_DEF, replacement)}
        ),
    )
    result = await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)
    assert result == NotebookListResult(notebooks=())
    assert calls == [NotebookListInput()]
    assert executor.calls == []


# --- typed dispatch ----------------------------------------------------------

_MYPY_SNIPPET = """
from notebooklm._binding import bind
from notebooklm._deadline import RuntimeDeadline
from notebooklm._records import (
    ARTIFACT_GENERATE_AUDIO_DEF,
    AudioGenerateInput,
    AudioGenerateResult,
    VideoGenerateInput,
)


async def audio(value: AudioGenerateInput, *, deadline: RuntimeDeadline | None) -> AudioGenerateResult:
    raise NotImplementedError


async def video(value: VideoGenerateInput, *, deadline: RuntimeDeadline | None) -> AudioGenerateResult:
    raise NotImplementedError


class Handlers:
    async def audio(self, value: AudioGenerateInput, *, deadline: RuntimeDeadline | None) -> AudioGenerateResult:
        raise NotImplementedError


good = bind(ARTIFACT_GENERATE_AUDIO_DEF, audio)
bound = bind(ARTIFACT_GENERATE_AUDIO_DEF, Handlers().audio)
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
    bad_output, bad_status = run("bad = bind(ARTIFACT_GENERATE_AUDIO_DEF, video)")
    assert bad_status != 0
    # mypy reports the mismatch either as an arg-type error or, when the
    # protocol's input parameter cannot unify, as an inference failure on
    # ``bind``; both mean the pairing was rejected at type-check time.
    assert "snippet.py:27: error:" in bad_output, bad_output
