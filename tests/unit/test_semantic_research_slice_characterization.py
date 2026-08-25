"""Migration sentinels for the P6.2 semantic research slice.

The whole domain -- start, poll, wait, cancel, and both import methods -- runs on
the semantic backend.  These tests pin the three things the migration could
silently break: the published facade signatures, the wire grammar now owned by
``_web/codec/research.py``, and the record/projector round trip that has to
rebuild every public ``ResearchTask`` field the parser recovered.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm import _research_service as research_service_module
from notebooklm._backend import BackendContractError, BackendError, BackendErrorReason
from notebooklm._backend_compat import project_backend_error
from notebooklm._operations import CallPolicy, Operation
from notebooklm._projectors import project_research_source, project_research_task
from notebooklm._records import (
    RESEARCH_CANCEL_DEF,
    RESEARCH_IMPORT_DEF,
    RESEARCH_POLL_DEF,
    RESEARCH_START_DEF,
    ResearchImportEntry,
    ResearchImportEntryKind,
    ResearchMode,
    ResearchSearchSource,
    ResearchSourceRecord,
    ResearchTaskRecord,
)
from notebooklm._research import ResearchAPI
from notebooklm._research_service import ResearchService
from notebooklm._web.codec.research import (
    build_report_import_entry,
    build_web_import_entry,
    decode_imported_sources,
    decode_research_start,
    decode_research_tasks,
    encode_research_cancel_params,
    encode_research_import_params,
    encode_research_poll_params,
    encode_research_start_params,
)
from notebooklm._web.errors import translate_web_error
from notebooklm.exceptions import (
    AmbiguousResearchTaskError,
    DecodingError,
    ResearchStartUnavailableError,
    RPCError,
    ValidationError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import DiscoveryMode
from notebooklm.types import ResearchStatus, ResearchTask
from tests._fixtures.web_backend import build_web_backend


class _RecordingRpc:
    """Replay queued decoded payloads and record every dispatched call."""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[RPCMethod, list[Any], dict[str, Any]]] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append((method, params, kwargs))
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    @property
    def methods(self) -> list[RPCMethod]:
        return [method for method, _params, _kwargs in self.calls]


def _service(rpc: _RecordingRpc, source_lister: object | None = None) -> ResearchService:
    return ResearchService(
        build_web_backend(rpc),
        source_lister=source_lister or MagicMock(),  # type: ignore[arg-type]
    )


def _task_row(
    task_id: str,
    *,
    query: str = "q",
    status_code: int = 2,
    source_type: int = 1,
) -> list[Any]:
    sources = [["https://example.com/a", "A", "desc", 1]]
    task_info = [None, [query, source_type], 1, [sources, f"{query} summary"], status_code]
    return [task_id, task_info]


# --- Published surface -------------------------------------------------------


def test_research_public_signatures_are_frozen() -> None:
    assert list(inspect.signature(ResearchAPI.start).parameters) == [
        "self",
        "notebook_id",
        "query",
        "source",
        "mode",
    ]
    assert list(inspect.signature(ResearchAPI.poll).parameters) == [
        "self",
        "notebook_id",
        "task_id",
    ]
    wait = inspect.signature(ResearchAPI.wait_for_completion).parameters
    assert list(wait) == ["self", "notebook_id", "task_id", "timeout", "initial_interval"]
    assert wait["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
    assert wait["timeout"].default == 1800
    # The unset sentinel must stay an opaque object, not the literal cadence:
    # the public-API compat gate compares default reprs.
    assert not isinstance(wait["initial_interval"].default, (int, float))
    assert list(inspect.signature(ResearchAPI.cancel).parameters) == [
        "self",
        "notebook_id",
        "run_id",
    ]
    assert list(inspect.signature(ResearchAPI.import_sources).parameters) == [
        "self",
        "notebook_id",
        "task_id",
        "sources",
        "_remaining_budget",
    ]
    verify = inspect.signature(ResearchAPI.import_sources_with_verification).parameters
    assert list(verify) == [
        "self",
        "notebook_id",
        "task_id",
        "sources",
        "max_elapsed",
        "initial_delay",
        "backoff_factor",
        "max_delay",
        "allow_duplicate",
    ]
    assert verify["max_elapsed"].default == 1800
    assert verify["allow_duplicate"].default is False


def test_facade_carries_no_wire_vocabulary() -> None:
    """The migrated facade must not name RPC methods or build wire rows."""
    source = inspect.getsource(inspect.getmodule(ResearchAPI))  # type: ignore[arg-type]
    assert "RPCMethod" not in source
    assert "rpc_call" not in source
    assert not hasattr(ResearchAPI, "_rpc_call")
    assert not hasattr(ResearchAPI, "_build_report_import_entry")
    assert not hasattr(ResearchAPI, "_build_web_import_entry")


def test_research_operation_policies_match_the_catalog() -> None:
    assert RESEARCH_START_DEF.key is Operation.RESEARCH_START
    assert RESEARCH_START_DEF.policy is CallPolicy.STATEFUL_START
    assert RESEARCH_POLL_DEF.policy is CallPolicy.READ
    assert RESEARCH_CANCEL_DEF.policy is CallPolicy.MUTATION
    assert RESEARCH_IMPORT_DEF.policy is CallPolicy.MUTATION


# --- Codec grammar -----------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "search_source", "expected"),
    [
        (ResearchMode.FAST, ResearchSearchSource.WEB, [["q", 1], None, 1, "nb"]),
        (ResearchMode.FAST, ResearchSearchSource.DRIVE, [["q", 2], None, 1, "nb"]),
        (ResearchMode.DEEP, ResearchSearchSource.WEB, [None, [1], ["q", 1], 5, "nb"]),
    ],
)
def test_start_params_are_pinned_per_mode_and_corpus(
    mode: ResearchMode,
    search_source: ResearchSearchSource,
    expected: list[Any],
) -> None:
    assert encode_research_start_params("nb", "q", search_source, mode) == expected


def test_poll_and_cancel_params_share_the_omitted_client_context_slot() -> None:
    assert encode_research_poll_params("nb") == [None, None, "nb"]
    assert encode_research_cancel_params("run") == [None, None, "run"]


def test_import_entry_builders_keep_their_positional_contracts() -> None:
    report = build_report_import_entry("Title", "# Markdown")
    assert report[0] is None
    assert report[1] == ["Title", "# Markdown"]
    assert report[3] == 3
    assert report[10] == 3

    web = build_web_import_entry("https://example.com", "Example")
    assert web[0] is None
    assert web[1] is None
    assert web[2] == ["https://example.com", "Example"]
    assert web[10] == 2


def test_import_params_send_report_entries_before_web_entries() -> None:
    params = encode_research_import_params(
        "nb",
        "task",
        (
            ResearchImportEntry(
                kind=ResearchImportEntryKind.REPORT,
                title="Report",
                report_markdown="# body",
            ),
            ResearchImportEntry(
                kind=ResearchImportEntryKind.WEB,
                title="Web",
                url="https://example.com",
            ),
        ),
    )
    assert params[:4] == [None, [1], "task", "nb"]
    report_entry, web_entry = params[4]
    assert report_entry[10] == 3
    assert web_entry[10] == 2


@pytest.mark.parametrize("payload", [None, [], "not-a-list", {}])
def test_start_decode_rejects_a_couldnt_start_payload(payload: Any) -> None:
    with pytest.raises(DecodingError, match="empty / non-list payload") as caught:
        decode_research_start(payload, method_id="Ljjv0c")
    assert caught.value.method_id == "Ljjv0c"


def test_start_decode_rejects_a_falsey_task_id() -> None:
    with pytest.raises(DecodingError, match="returned no task id"):
        decode_research_start(["", "report"], method_id="Ljjv0c")


def test_start_decode_keeps_the_optional_report_id_absent_for_fast_runs() -> None:
    assert decode_research_start(["task"], method_id="Ljjv0c").report_id is None
    assert decode_research_start(["task", "report"], method_id="Ljjv0c").report_id == "report"


def test_imported_source_decode_unwraps_either_response_envelope() -> None:
    wrapped = decode_imported_sources([[[["src_1"], "One"]]])
    flat = decode_imported_sources([[["src_1"], "One"]])
    assert wrapped == flat
    assert [(record.id, record.title) for record in flat] == [("src_1", "One")]


def test_imported_source_decode_skips_rows_without_a_usable_id() -> None:
    """The response is documented as incomplete: a bad row is skipped, not raised."""
    assert [
        (record.id, record.title)
        for record in decode_imported_sources(
            [[["src_1"], "One"], [None, "Skipped"], [[], "Empty"], ["short"]]
        )
    ] == [("src_1", "One")]
    assert decode_imported_sources(None) == ()


# --- Record / projector round trip -------------------------------------------


def test_poll_decode_and_projection_preserve_every_public_task_field() -> None:
    records = decode_research_tasks([[_task_row("task_1", query="quantum")]])
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, ResearchTaskRecord)
    # The backend reports what it observed; sibling selection is service policy.
    assert not hasattr(record, "tasks")

    task = project_research_task(record)
    assert task.task_id == "task_1"
    assert task.status is ResearchStatus.COMPLETED
    assert task.query == "quantum"
    assert task.summary == "quantum summary"
    assert task.status_code == 2
    assert task.source_type == 1
    assert task.discovery_mode is DiscoveryMode.DEFAULT_LLM_SEARCH
    assert task.tasks == ()
    assert [(source.url, source.title, source.hint) for source in task.sources] == [
        ("https://example.com/a", "A", "desc")
    ]


def test_task_projection_round_trips_the_full_public_field_set() -> None:
    record = ResearchTaskRecord(
        task_id="t",
        status=ResearchStatus.FAILED.value,
        query="q",
        sources=(
            ResearchSourceRecord(
                url="https://example.com",
                title="T",
                result_type=5,
                research_task_id="t",
                report_markdown="# body",
                source_ordinal=3,
                hint="why",
            ),
        ),
        summary="s",
        report="# body",
        status_code=7,
        source_type=2,
        discovery_mode="deep_research",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        account_id="acct",
    )
    task = project_research_task(record)
    assert task == ResearchTask(
        task_id="t",
        status=ResearchStatus.FAILED,
        query="q",
        sources=(project_research_source(record.sources[0]),),
        summary="s",
        report="# body",
        status_code=7,
        source_type=2,
        discovery_mode=DiscoveryMode.DEEP_RESEARCH,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        account_id="acct",
    )
    source = task.sources[0]
    assert source.is_report
    assert source.source_ordinal == 3
    assert source.hint == "why"


def test_unmapped_discovery_mode_label_projects_the_reserved_unknown_member() -> None:
    record = ResearchTaskRecord(task_id="t", status="completed", discovery_mode="from_the_future")
    assert project_research_task(record).discovery_mode is DiscoveryMode.UNKNOWN


# --- Backend bindings --------------------------------------------------------


@pytest.mark.asyncio
async def test_start_dispatches_the_mode_specific_rpc_and_routes_by_notebook() -> None:
    rpc = _RecordingRpc(["task_1", "report_1"])
    result = await _service(rpc).start("nb", "q", "web", "deep")

    method, params, kwargs = rpc.calls[0]
    assert method is RPCMethod.START_DEEP_RESEARCH
    assert params == [None, [1], ["q", 1], 5, "nb"]
    assert kwargs["source_path"] == "/notebook/nb"
    # notebook_id / query / mode are caller-supplied, not backend evidence.
    assert (result.task_id, result.report_id) == ("task_1", "report_1")
    assert (result.notebook_id, result.query, result.mode) == ("nb", "q", "deep")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "mode", "message"),
    [
        ("invalid", "fast", "Invalid source"),
        ("web", "invalid", "Invalid mode"),
        ("drive", "deep", "Deep Research only supports Web sources"),
    ],
)
async def test_start_validates_before_any_dispatch(
    source: str,
    mode: str,
    message: str,
) -> None:
    rpc = _RecordingRpc()
    with pytest.raises(ValidationError, match=message):
        await _service(rpc).start("nb", "q", source, mode)
    assert rpc.calls == []


@pytest.mark.asyncio
async def test_deep_start_null_result_keeps_its_domain_error_and_rejecting_cause() -> None:
    rejected = RPCError(
        "NotebookLM rejected this request",
        method_id=RPCMethod.START_DEEP_RESEARCH.value,
        rpc_code=13,
        found_ids=[RPCMethod.START_DEEP_RESEARCH.value],
    )
    rpc = _RecordingRpc(rejected)

    with pytest.raises(ResearchStartUnavailableError) as caught:
        await _service(rpc).start("nb", "q", "web", "deep")

    error = caught.value
    assert error.notebook_id == "nb"
    assert error.mode == "deep"
    assert error.method_id == RPCMethod.START_DEEP_RESEARCH.value
    assert error.rpc_code == 13
    assert error.found_ids == [RPCMethod.START_DEEP_RESEARCH.value]
    # The obfuscated ids stay out of the human-readable message (#1921).
    assert RPCMethod.START_DEEP_RESEARCH.value not in str(error)
    assert isinstance(error.__cause__, RPCError)
    assert error.__suppress_context__


@pytest.mark.asyncio
async def test_fast_start_null_result_is_not_reclassified() -> None:
    rejected = RPCError(
        "NotebookLM rejected this request",
        method_id=RPCMethod.START_FAST_RESEARCH.value,
        found_ids=[RPCMethod.START_FAST_RESEARCH.value],
    )
    with pytest.raises(RPCError) as caught:
        await _service(_RecordingRpc(rejected)).start("nb", "q", "web", "fast")
    assert not isinstance(caught.value, ResearchStartUnavailableError)


def test_start_unavailable_projection_requires_its_closed_evidence() -> None:
    incomplete = BackendError(
        message="research start returned no run",
        operation=Operation.RESEARCH_START,
        diagnostics={"notebook_id": "nb"},
        reason=BackendErrorReason.RESEARCH_START_UNAVAILABLE,
    )
    with pytest.raises(BackendContractError, match="lacks notebook_id/mode"):
        project_backend_error(incomplete)

    without_cause = BackendError(
        message="research start returned no run",
        operation=Operation.RESEARCH_START,
        diagnostics={"notebook_id": "nb", "mode": "deep"},
        reason=BackendErrorReason.RESEARCH_START_UNAVAILABLE,
    )
    with pytest.raises(BackendContractError, match="lacks original RPC evidence"):
        project_backend_error(without_cause)


def test_transport_classification_refuses_to_mint_the_domain_reason() -> None:
    """Only the handler that attaches the rejecting RPC may produce this reason.

    ``ResearchStartUnavailableError`` is an ``RPCError`` subclass, so leaving it
    in the transport table would let a raw raise reach the projector without the
    original-call evidence it reconstructs ``__cause__`` from.
    """
    with pytest.raises(BackendContractError, match="unclassified web error type"):
        translate_web_error(
            Operation.RESEARCH_START,
            ResearchStartUnavailableError("nb", "deep"),
        )


@pytest.mark.asyncio
async def test_cancel_is_fire_and_forget_and_routes_on_the_notebook() -> None:
    rpc = _RecordingRpc([])
    assert await _service(rpc).cancel("nb", "run_1") is None

    method, params, kwargs = rpc.calls[0]
    assert method is RPCMethod.CANCEL_RESEARCH
    assert params == [None, None, "run_1"]
    assert kwargs["source_path"] == "/notebook/nb"


@pytest.mark.asyncio
async def test_import_hands_the_resolved_attempt_window_to_the_transport() -> None:
    rpc = _RecordingRpc([[[["src_1"], "One"]]])
    imported = await _service(rpc).import_sources(
        "nb",
        "task",
        [{"url": "https://example.com", "title": "One"}],
    )

    method, params, kwargs = rpc.calls[0]
    assert method is RPCMethod.IMPORT_RESEARCH
    assert params[2:4] == ["task", "nb"]
    # One source: the batch-scaled 60 + 3 * 1 window, floored at the default.
    assert kwargs["read_timeout"] == 63.0
    assert imported == [{"id": "src_1", "title": "One"}]


@pytest.mark.asyncio
async def test_import_skips_the_rpc_when_nothing_survives_partitioning() -> None:
    rpc = _RecordingRpc()
    assert await _service(rpc).import_sources("nb", "task", []) == []
    assert await _service(rpc).import_sources("nb", "task", [{"title": "no url"}]) == []
    assert rpc.calls == []


# --- Service composition -----------------------------------------------------


@pytest.mark.asyncio
async def test_poll_raises_on_an_ambiguous_unfiltered_selection() -> None:
    rpc = _RecordingRpc([[_task_row("task_a"), _task_row("task_b")]])
    with pytest.raises(AmbiguousResearchTaskError) as caught:
        await _service(rpc).poll("nb")
    assert caught.value.task_ids == ["task_a", "task_b"]


@pytest.mark.asyncio
async def test_poll_carries_siblings_and_pins_a_requested_task() -> None:
    rpc = _RecordingRpc([[_task_row("task_a"), _task_row("task_b")]])
    result = await _service(rpc).poll("nb", "task_b")
    assert result.task_id == "task_b"
    assert [task.task_id for task in result.tasks] == ["task_b"]
    assert all(task.tasks == () for task in result.tasks)


@pytest.mark.asyncio
async def test_poll_distinguishes_an_empty_poll_from_a_missing_pinned_task() -> None:
    assert (await _service(_RecordingRpc([])).poll("nb")).status is ResearchStatus.NO_RESEARCH
    missing = await _service(_RecordingRpc([])).poll("nb", "task_x")
    assert missing.status is ResearchStatus.NOT_FOUND
    assert missing.task_id == "task_x"
    assert missing.tasks == ()


@pytest.mark.asyncio
async def test_wait_pins_the_first_task_and_polls_once_per_tick(monkeypatch) -> None:
    slept: list[float] = []

    async def _sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(research_service_module.asyncio, "sleep", _sleep)
    rpc = _RecordingRpc(
        [[_task_row("task_a", status_code=1)]],
        [[_task_row("task_a", status_code=1)], [_task_row("task_b", status_code=2)]],
        [[_task_row("task_a", status_code=2)]],
    )

    result = await _service(rpc).wait_for_completion("nb", timeout=100, initial_interval=1)

    # A sibling appearing mid-wait must not substitute its result: the loop
    # pinned task_a on the first poll and stayed on it.
    assert result.task_id == "task_a"
    assert rpc.methods == [RPCMethod.POLL_RESEARCH] * 3
    assert slept == [1, 1]


@pytest.mark.asyncio
async def test_wait_rejects_a_non_numeric_explicit_interval() -> None:
    with pytest.raises(TypeError, match="poll interval must be a number"):
        await _service(_RecordingRpc()).wait_for_completion("nb", initial_interval=None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timeout must be non-negative"):
        await _service(_RecordingRpc()).wait_for_completion("nb", timeout=-1)
    with pytest.raises(ValueError, match="poll interval must be positive"):
        await _service(_RecordingRpc()).wait_for_completion("nb", initial_interval=0)


# --- GET_NOTEBOOK recency inventory -----------------------------------------


@pytest.mark.asyncio
async def test_research_operations_bump_no_recency() -> None:
    """GET_NOTEBOOK writes lastViewedTime; no research binding may issue one."""
    rpc = _RecordingRpc(
        ["task_1"],
        [[_task_row("task_1")]],
        [],
        [[[["src_1"], "One"]]],
    )
    service = _service(rpc)
    await service.start("nb", "q")
    await service.poll("nb")
    await service.cancel("nb", "task_1")
    await service.import_sources("nb", "task_1", [{"url": "https://e.com", "title": "One"}])

    assert RPCMethod.GET_NOTEBOOK not in rpc.methods
    assert rpc.methods == [
        RPCMethod.START_FAST_RESEARCH,
        RPCMethod.POLL_RESEARCH,
        RPCMethod.CANCEL_RESEARCH,
        RPCMethod.IMPORT_RESEARCH,
    ]


@pytest.mark.asyncio
async def test_verified_import_reads_one_baseline_snapshot_on_the_happy_path() -> None:
    """One snapshot, no probe: the probe only runs after a failed attempt."""
    lister = MagicMock()
    lister.list = AsyncMock(return_value=[])
    rpc = _RecordingRpc([[[["src_1"], "One"]]])

    imported = await _service(rpc, lister).import_sources_with_verification(
        "nb",
        "task",
        [{"url": "https://example.com", "title": "One"}],
    )

    assert imported == [{"id": "src_1", "title": "One"}]
    assert lister.list.await_count == 1
    assert lister.list.await_args.kwargs == {"strict": False}
    assert rpc.methods == [RPCMethod.IMPORT_RESEARCH]
