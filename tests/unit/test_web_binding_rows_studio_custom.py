"""P9.4b: the Studio generate families and prompt suggestions as custom rows.

The eight ``CREATE_ARTIFACT`` generate members and ``NOTEBOOK_SUGGEST_PROMPTS``
dispatch as ``CustomBinding`` rows exactly as the P5/P6
handlers did.  These tests pin the conversion oracles: the identical keyword set
reaches the runtime for every phase (the conditional default-source read and
the guarded kickoff), option validation rejects an
unreviewed input before any native call, the null-kickoff projection is the
closed unavailable error, failures stay tagged with the selected spec and carry
the ``dispatched`` marker, the deadline projection is the handler's, and the
collapsed source-id decoder preserves each family's warning surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CustomBinding, RpcNative
from notebooklm._deadline import RuntimeDeadline
from notebooklm._notebook_payloads import build_get_notebook_params
from notebooklm._operations import Operation
from notebooklm._records import (
    ARTIFACT_GENERATE_AUDIO_DEF,
    ARTIFACT_GENERATE_DATA_TABLE_DEF,
    ARTIFACT_GENERATE_FLASHCARDS_DEF,
    ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
    ARTIFACT_GENERATE_QUIZ_DEF,
    ARTIFACT_GENERATE_REPORT_DEF,
    ARTIFACT_GENERATE_SLIDE_DECK_DEF,
    ARTIFACT_GENERATE_VIDEO_DEF,
    NOTEBOOK_SUGGEST_PROMPTS_DEF,
    AudioGenerateInput,
    DataTableGenerateInput,
    InfographicGenerateInput,
    InteractiveGenerateInput,
    NotebookSuggestPromptsInput,
    ReportGenerateInput,
    SlideDeckGenerateInput,
    VideoGenerateInput,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import settings as settings_rows
from notebooklm._web.bindings import studio as studio_rows
from notebooklm._web.codec.source_ids import SourceIdDiagnostics, decode_notebook_source_ids
from notebooklm._web.codec.suggestions import encode_prompt_suggestions
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_NOTEBOOK_WITH_SOURCES: list[Any] = [
    ["Notebook", [[["src-a"], "A"], [["src-b"], "B"]], "nb"],
]
_KICKOFF: list[Any] = [["task-id", "Title", 1, None, 1]]

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


_GENERATE_CASES = [
    (ARTIFACT_GENERATE_AUDIO_DEF, lambda ids: AudioGenerateInput("nb", ids), "audio"),
    (
        ARTIFACT_GENERATE_QUIZ_DEF,
        lambda ids: InteractiveGenerateInput("nb", ids, None, None, None),
        "quiz",
    ),
    (
        ARTIFACT_GENERATE_FLASHCARDS_DEF,
        lambda ids: InteractiveGenerateInput("nb", ids, None, None, None),
        "flashcards",
    ),
    (ARTIFACT_GENERATE_REPORT_DEF, lambda ids: ReportGenerateInput("nb", source_ids=ids), "report"),
    (ARTIFACT_GENERATE_VIDEO_DEF, lambda ids: VideoGenerateInput("nb", ids), "video"),
    (
        ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
        lambda ids: InfographicGenerateInput("nb", ids),
        "infographic",
    ),
    (
        ARTIFACT_GENERATE_SLIDE_DECK_DEF,
        lambda ids: SlideDeckGenerateInput("nb", ids),
        "slide deck",
    ),
    (
        ARTIFACT_GENERATE_DATA_TABLE_DEF,
        lambda ids: DataTableGenerateInput("nb", ids),
        "data table",
    ),
]
_GENERATE_IDS = [definition.key.value for definition, _factory, _kind in _GENERATE_CASES]


# --- registry partition ----------------------------------------------------------


def test_generate_families_and_prompts_are_deferred_product_custom_rows() -> None:
    rows = {
        Operation.ARTIFACT_GENERATE_AUDIO: studio_rows.ARTIFACT_GENERATE_AUDIO,
        Operation.ARTIFACT_GENERATE_QUIZ: studio_rows.ARTIFACT_GENERATE_QUIZ,
        Operation.ARTIFACT_GENERATE_FLASHCARDS: studio_rows.ARTIFACT_GENERATE_FLASHCARDS,
        Operation.ARTIFACT_GENERATE_REPORT: studio_rows.ARTIFACT_GENERATE_REPORT,
        Operation.ARTIFACT_GENERATE_VIDEO: studio_rows.ARTIFACT_GENERATE_VIDEO,
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC: studio_rows.ARTIFACT_GENERATE_INFOGRAPHIC,
        Operation.ARTIFACT_GENERATE_SLIDE_DECK: studio_rows.ARTIFACT_GENERATE_SLIDE_DECK,
        Operation.ARTIFACT_GENERATE_DATA_TABLE: studio_rows.ARTIFACT_GENERATE_DATA_TABLE,
        Operation.NOTEBOOK_SUGGEST_PROMPTS: settings_rows.NOTEBOOK_SUGGEST_PROMPTS,
    }
    for operation, row in rows.items():
        assert WEB_BINDING_ROWS[operation] is row
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.row is row
        assert isinstance(row, CustomBinding)
        assert row.definition is binding.definition
        assert row.category == "deferred-product"
        assert row.justification.strip()
        assert row.collaborators == ()
    for operation in list(rows)[:8]:
        row = rows[operation]
        assert [spec.key for spec in row.native] == ["sources", "create"]
        assert row.spec("sources").select(None) == RpcNative(RPCMethod.GET_NOTEBOOK)
        assert row.spec("create").select(None) == RpcNative(RPCMethod.CREATE_ARTIFACT)
    prompts = settings_rows.NOTEBOOK_SUGGEST_PROMPTS
    assert [spec.key for spec in prompts.native] == ["sources", "suggest"]
    assert prompts.spec("suggest").select(None) == RpcNative(RPCMethod.SUGGEST_PROMPTS)


def test_emptied_chain_classes_are_gone_and_the_chain_re_links() -> None:
    chain = [klass.__name__ for klass in WebRpcBackend.__mro__]
    assert chain == [
        "WebRpcBackend",
        "object",
    ]
    for name in (
        "_audio_generate",
        "_quiz_generate",
        "_flashcards_generate",
        "_interactive_generate",
        "_infographic_generate",
        "_slide_deck_generate",
        "_visual_generate",
        "_visual_source_selection",
        "_report_generate",
        "_video_generate",
        "_document_generate",
        "_document_source_ids",
        "_generation_source_ids",
        "_data_table_generate",
        "_notebook_suggest_prompts",
        "_artifact_rename",
    ):
        assert not hasattr(WebRpcBackend, name), name
    assert not hasattr(WebRpcBackend, "_audio_source_ids")
    assert not hasattr(WebRpcBackend, "_artifact_feature_unavailable")


# --- generate families: sequence and kwargs ----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("definition", "factory", "_kind"), _GENERATE_CASES, ids=_GENERATE_IDS)
async def test_generate_rows_resolve_default_sources_then_kick_off(
    definition: Any, factory: Any, _kind: str
) -> None:
    executor = _RecordingExecutor(_NOTEBOOK_WITH_SOURCES, _KICKOFF)
    backend = build_web_backend(executor)

    result = await backend.invoke(definition, factory(None), deadline=None)

    assert (result.status.task_id, result.status.status) == ("task-id", "pending")
    read, create = executor.calls
    assert read.method is RPCMethod.GET_NOTEBOOK
    assert read.params == build_get_notebook_params("nb")
    assert read.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb"}
    assert create.method is RPCMethod.CREATE_ARTIFACT
    assert create.kwargs == {
        **_BASE_KWARGS,
        "source_path": "/notebook/nb",
        "allow_null": True,
        "raise_on_null_status": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("definition", "factory", "_kind"), _GENERATE_CASES, ids=_GENERATE_IDS)
async def test_generate_rows_skip_the_read_when_sources_are_explicit(
    definition: Any, factory: Any, _kind: str
) -> None:
    executor = _RecordingExecutor(_KICKOFF)
    backend = build_web_backend(executor)

    await backend.invoke(definition, factory(("src-a",)), deadline=None)

    assert [call.method for call in executor.calls] == [RPCMethod.CREATE_ARTIFACT]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "factory", "artifact_type"), _GENERATE_CASES, ids=_GENERATE_IDS
)
async def test_null_kickoff_is_the_closed_unavailable_error(
    definition: Any, factory: Any, artifact_type: str
) -> None:
    backend = build_web_backend(_RecordingExecutor(None))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(definition, factory(("src-a",)), deadline=None)

    error = caught.value
    assert error.reason is BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE
    assert error.operation is definition.key
    assert error.message == f"{artifact_type.capitalize()} generation is unavailable"
    assert error.diagnostics == {
        "artifact_type": artifact_type,
        "method_id": RPCMethod.CREATE_ARTIFACT.value,
        "raw_response": None,
    }


@pytest.mark.asyncio
async def test_null_task_id_projects_the_generic_artifact_unavailable_error() -> None:
    backend = build_web_backend(_RecordingExecutor([[None, "Audio", 1, None, 1]]))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            ARTIFACT_GENERATE_AUDIO_DEF, AudioGenerateInput("nb", ("src-a",)), deadline=None
        )

    assert caught.value.reason is BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["artifact_type"] == "artifact"


@pytest.mark.asyncio
async def test_cinematic_video_names_its_own_artifact_type() -> None:
    backend = build_web_backend(_RecordingExecutor(None))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            ARTIFACT_GENERATE_VIDEO_DEF,
            VideoGenerateInput("nb", ("src-a",), cinematic_route=True),
            deadline=None,
        )

    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["artifact_type"] == "cinematic video"


# --- option validation before any native call ----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "value", "match"),
    [
        (
            ARTIFACT_GENERATE_AUDIO_DEF,
            AudioGenerateInput("nb", None, audio_format="future"),
            "unrecognized audio format",
        ),
        (
            ARTIFACT_GENERATE_QUIZ_DEF,
            InteractiveGenerateInput("nb", None, None, "dozens", None),
            "unrecognized interactive quantity",
        ),
        (
            ARTIFACT_GENERATE_FLASHCARDS_DEF,
            InteractiveGenerateInput("nb", None, None, None, "impossible"),
            "unrecognized interactive difficulty",
        ),
        (
            ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
            InfographicGenerateInput("nb", None, orientation="diagonal"),
            "unrecognized visual orientation",
        ),
        (
            ARTIFACT_GENERATE_SLIDE_DECK_DEF,
            SlideDeckGenerateInput("nb", None, slide_length="epic"),
            "unrecognized visual length",
        ),
    ],
)
async def test_media_families_reject_unreviewed_options_before_the_source_read(
    definition: Any, value: Any, match: str
) -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)

    with pytest.raises(BackendContractError, match=match):
        await backend.invoke(definition, value, deadline=None)

    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "value", "match"),
    [
        (
            ARTIFACT_GENERATE_VIDEO_DEF,
            VideoGenerateInput("nb", None, video_format="imax"),
            "unrecognized video option",
        ),
        (
            ARTIFACT_GENERATE_REPORT_DEF,
            ReportGenerateInput("nb", "novel", source_ids=None),
            "unrecognized report format",
        ),
    ],
)
async def test_document_families_validate_after_the_source_read_as_before(
    definition: Any, value: Any, match: str
) -> None:
    executor = _RecordingExecutor(_NOTEBOOK_WITH_SOURCES)
    backend = build_web_backend(executor)

    with pytest.raises(BackendContractError, match=match):
        await backend.invoke(definition, value, deadline=None)

    assert [call.method for call in executor.calls] == [RPCMethod.GET_NOTEBOOK]


# --- failure projection and deadline -----------------------------------------------------


@pytest.mark.asyncio
async def test_kickoff_server_error_is_translated_dispatched_and_tagged() -> None:
    executor = _RecordingExecutor(
        _NOTEBOOK_WITH_SOURCES,
        ServerError("boom", method_id=RPCMethod.CREATE_ARTIFACT.value),
    )
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            ARTIFACT_GENERATE_QUIZ_DEF,
            InteractiveGenerateInput("nb", None, None, None, None),
            deadline=None,
        )

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.ARTIFACT_GENERATE_QUIZ
    assert error.reason is BackendErrorReason.SERVER
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert isinstance(error.__cause__, ServerError)
    assert error.__cause__.binding_native == RpcNative(RPCMethod.CREATE_ARTIFACT)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_pre_dispatch_expiry_on_the_kickoff_is_not_commit_uncertain() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(_NOTEBOOK_WITH_SOURCES)
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0  # the read consumes the budget before the kickoff is assembled
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            ARTIFACT_GENERATE_DATA_TABLE_DEF, DataTableGenerateInput("nb", None), deadline=deadline
        )

    assert [call.method for call in executor.calls] == [RPCMethod.GET_NOTEBOOK]
    assert caught.value.operation is Operation.ARTIFACT_GENERATE_DATA_TABLE
    assert caught.value.outcome_unknown is False  # the handler never marked the kickoff phase
    assert caught.value.dispatched is False
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["method_id"] == RPCMethod.CREATE_ARTIFACT.value


@pytest.mark.asyncio
async def test_post_dispatch_timeout_becomes_a_dispatched_deadline_error() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(
        RPCTimeoutError("slow", method_id=RPCMethod.CREATE_ARTIFACT.value)
    )
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            ARTIFACT_GENERATE_AUDIO_DEF, AudioGenerateInput("nb", ("src-a",)), deadline=deadline
        )

    assert caught.value.dispatched is True
    assert caught.value.outcome_unknown is True  # STATEFUL_START is not a READ policy
    assert may_have_committed(caught.value) is True


# --- prompt suggestions ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_prompts_resolves_sources_then_reads_with_allow_null() -> None:
    executor = _RecordingExecutor(_NOTEBOOK_WITH_SOURCES, [[["Title", "Prompt"]]])
    backend = build_web_backend(executor)

    result = await backend.invoke(
        NOTEBOOK_SUGGEST_PROMPTS_DEF,
        NotebookSuggestPromptsInput("nb", None, mode=3, query="why"),
        deadline=None,
    )

    assert [(row.title, row.prompt) for row in result.suggestions] == [("Title", "Prompt")]
    read, suggest = executor.calls
    assert read.method is RPCMethod.GET_NOTEBOOK
    assert read.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb"}
    assert suggest.method is RPCMethod.SUGGEST_PROMPTS
    assert suggest.params == encode_prompt_suggestions(
        "nb", ["src-a", "src-b"], mode=3, query="why"
    )
    assert suggest.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb", "allow_null": True}


@pytest.mark.asyncio
async def test_suggest_prompts_with_explicit_sources_issues_one_call() -> None:
    executor = _RecordingExecutor(None)
    backend = build_web_backend(executor)

    result = await backend.invoke(
        NOTEBOOK_SUGGEST_PROMPTS_DEF,
        NotebookSuggestPromptsInput("nb", ("src-z",)),
        deadline=None,
    )

    assert result.suggestions == ()
    assert [call.method for call in executor.calls] == [RPCMethod.SUGGEST_PROMPTS]


# --- the collapsed source-id decoder: one helper, three diagnostics modes ----------------

_NO_SOURCES_SLOT: list[Any] = [["Notebook without a sources slot"]]
_SOURCES_NOT_A_LIST: list[Any] = [["Notebook", "not-a-list"]]
_TOP_NOT_A_LIST: list[Any] = ["scalar"]


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        (_NO_SOURCES_SLOT, "notebook_info has no sources slot for nb-x"),
        (_SOURCES_NOT_A_LIST, "notebook_info[1] not list for nb-x"),
        (_TOP_NOT_A_LIST, "notebook_data[0] shape unexpected for nb-x"),
    ],
)
def test_warn_and_guarded_modes_log_the_schema_drift_warnings(
    payload: list[Any], fragment: str, caplog: pytest.LogCaptureFixture
) -> None:
    for mode in (SourceIdDiagnostics.WARN, SourceIdDiagnostics.GUARDED):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
            assert decode_notebook_source_ids(payload, notebook_id="nb-x", diagnostics=mode) == ()
        assert [record.getMessage() for record in caplog.records] == [
            f"get_source_ids: {fragment} (schema drift?). "
            + ("top-type=str" if payload is _TOP_NOT_A_LIST else "len=" + str(len(payload[0])))
        ]


@pytest.mark.parametrize("payload", [_NO_SOURCES_SLOT, _SOURCES_NOT_A_LIST, _TOP_NOT_A_LIST])
def test_silent_mode_yields_no_ids_and_says_nothing(
    payload: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        assert (
            decode_notebook_source_ids(
                payload, notebook_id="nb-x", diagnostics=SourceIdDiagnostics.SILENT
            )
            == ()
        )
    assert caplog.records == []


def test_guarded_mode_keeps_partial_ids_and_reports_the_guard_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from notebooklm._web.codec import source_ids as module

    real_from_entry = module.SourceRow.from_entry
    seen: list[Any] = []

    def flaky(source: Any, *, method_id: str) -> Any:
        seen.append(source)
        if len(seen) % 2 == 0:  # the second source of every decode raises past the guards
            raise TypeError("guard bypassed")
        return real_from_entry(source, method_id=method_id)

    monkeypatch.setattr(module.SourceRow, "from_entry", staticmethod(flaky))
    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        ids = decode_notebook_source_ids(
            _NOTEBOOK_WITH_SOURCES, notebook_id="nb-x", diagnostics=SourceIdDiagnostics.GUARDED
        )
    assert ids == ("src-a",)
    assert any(
        "unexpected exception despite guards for nb-x" in r.getMessage() for r in caplog.records
    )
    with pytest.raises(TypeError):
        decode_notebook_source_ids(
            _NOTEBOOK_WITH_SOURCES, notebook_id="nb-x", diagnostics=SourceIdDiagnostics.WARN
        )


@pytest.mark.asyncio
async def test_audio_default_read_is_silent_and_flashcards_read_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = build_web_backend(_RecordingExecutor(_NO_SOURCES_SLOT, _KICKOFF))
    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        await backend.invoke(
            ARTIFACT_GENERATE_AUDIO_DEF, AudioGenerateInput("nb", None), deadline=None
        )
    assert caplog.records == []

    backend = build_web_backend(_RecordingExecutor(_NO_SOURCES_SLOT, _KICKOFF))
    with caplog.at_level(logging.WARNING, logger="notebooklm._notebooks"):
        await backend.invoke(
            ARTIFACT_GENERATE_FLASHCARDS_DEF,
            InteractiveGenerateInput("nb", None, None, None, None),
            deadline=None,
        )
    assert ["no sources slot for nb" in r.getMessage() for r in caplog.records] == [True]
