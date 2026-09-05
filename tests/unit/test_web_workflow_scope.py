"""Real-supervisor regressions for complete Web workflow admission."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from notebooklm._android.chat import AndroidChatAPI
from notebooklm._android.notebooks import AndroidNotebooksAPI
from notebooklm._android.sources import AndroidSourcesAPI
from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import AdmissionState, CallSupervisor
from notebooklm._runtime.lifecycle import ClientLifecycle
from notebooklm._source.polling import SourcePoller
from notebooklm._types.research import MindMapResult
from notebooklm._types.sharing import ShareAccess, ShareStatus, ShareViewLevel
from notebooklm._web.artifacts import WebArtifactsAPI
from notebooklm._web.chat import WebChatAPI
from notebooklm._web.labels import WebLabelsAPI
from notebooklm._web.notebooks import WebNotebooksAPI
from notebooklm._web.sharing import WebSharingAPI
from notebooklm._web.sources import WebSourcesAPI
from notebooklm.rpc import RPCMethod
from notebooklm.types import Artifact, ChatSessionStatus, Notebook, Source, SourceStatus


def _lifecycle() -> tuple[ClientLifecycle, CallSupervisor]:
    supervisor = CallSupervisor(metrics=ClientMetrics(), max_concurrent_rpcs=1)
    return (
        ClientLifecycle(
            supervisor=supervisor,
            transports=(),
            loop_participants=(supervisor,),
        ),
        supervisor,
    )


async def _wait_for_drain(supervisor: CallSupervisor) -> None:
    for _ in range(100):
        generation = supervisor._current
        if generation is not None and generation.state is AdmissionState.DRAINING:
            return
        await asyncio.sleep(0)
    raise AssertionError("workflow did not observe graceful drain")


class _StagedRpc:
    def __init__(
        self,
        supervisor: CallSupervisor,
        responses: list[Any],
        *,
        gate_first: bool = True,
    ) -> None:
        self._supervisor = supervisor
        self._responses = iter(responses)
        self._gate_first = gate_first
        self.first_call_started = asyncio.Event()
        self.release_first_call = asyncio.Event()
        self.calls = 0

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        del params, kwargs
        self.calls += 1
        async with self._supervisor.call_scope(
            f"test.{method.value}",
            method.value,
            None,
        ):
            if self.calls == 1 and self._gate_first:
                self.first_call_started.set()
                await self.release_first_call.wait()
            return next(self._responses)


class _StagedCalls:
    def __init__(self, supervisor: CallSupervisor, first_result: Any, second_result: Any) -> None:
        self._supervisor = supervisor
        self._first_result = first_result
        self._second_result = second_result
        self.first_call_started = asyncio.Event()
        self.release_first_call = asyncio.Event()

    async def first(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        async with self._supervisor.call_scope("test.first-stage", "first-stage", None):
            self.first_call_started.set()
            await self.release_first_call.wait()
            return self._first_result

    async def second(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        async with self._supervisor.call_scope("test.second-stage", "second-stage", None):
            return self._second_result


class _StagedAndroidTransport:
    def __init__(self, supervisor: CallSupervisor, response: Any) -> None:
        self._supervisor = supervisor
        self._response = response

    def operation_scope(self, label: str, *, expected_epoch: int | None = None):
        return self._supervisor.operation_scope(label, expected_epoch=expected_epoch)

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        del request
        async with self._supervisor.call_scope(
            f"test.{method}",
            method,
            None,
            expected_epoch=kwargs.get("expected_epoch"),
        ):
            return self._response


async def _drain_after_first_stage(
    lifecycle: ClientLifecycle,
    supervisor: CallSupervisor,
    first_call_started: asyncio.Event,
    release_first_call: asyncio.Event,
    workflow: asyncio.Task[Any],
) -> Any:
    await first_call_started.wait()
    draining = asyncio.create_task(lifecycle.drain())
    await _wait_for_drain(supervisor)
    release_first_call.set()
    result, _ = await asyncio.gather(workflow, draining)
    await lifecycle.close(drain=False)
    return result


@pytest.mark.asyncio
async def test_set_view_level_keeps_readback_admitted_after_drain() -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    rpc = _StagedRpc(supervisor, [None])
    api = WebSharingAPI(rpc, supervisor=supervisor)

    async def get_status(notebook_id: str) -> ShareStatus:
        async with supervisor.call_scope("test.share-status", "share-status", None):
            return ShareStatus(
                notebook_id=notebook_id,
                is_public=False,
                access=ShareAccess.RESTRICTED,
                view_level=ShareViewLevel.FULL_NOTEBOOK,
            )

    api.get_status = get_status  # type: ignore[method-assign]
    workflow = asyncio.create_task(api.set_view_level("nb", ShareViewLevel.CHAT_ONLY))
    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        rpc.first_call_started,
        rpc.release_first_call,
        workflow,
    )

    assert result.view_level is ShareViewLevel.CHAT_ONLY


@pytest.mark.asyncio
async def test_label_create_keeps_send_admitted_after_baseline_drain() -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    rpc = _StagedRpc(
        supervisor,
        [
            [[]],
            [None, [["New", None, "label-1", ""]]],
        ],
    )
    api = WebLabelsAPI(
        rpc, list_sources=lambda _notebook_id: _empty_sources(), supervisor=supervisor
    )
    workflow = asyncio.create_task(api.create("nb", "New"))
    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        rpc.first_call_started,
        rpc.release_first_call,
        workflow,
    )

    assert result.id == "label-1"


async def _empty_sources() -> list[Any]:
    return []


class _StagedMindMapGeneration:
    def __init__(self, supervisor: CallSupervisor) -> None:
        self._supervisor = supervisor
        self.first_call_started = asyncio.Event()
        self.release_first_call = asyncio.Event()

    async def generate_mind_map(self, *args: Any, **kwargs: Any) -> MindMapResult:
        del args, kwargs
        async with self._supervisor.call_scope("test.resolve-sources", "resolve", None):
            self.first_call_started.set()
            await self.release_first_call.wait()
        async with self._supervisor.call_scope("test.persist-note", "persist", None):
            return MindMapResult(mind_map={"name": "Map"}, note_id="note-1")


@pytest.mark.asyncio
async def test_artifact_mind_map_keeps_persistence_admitted_after_drain() -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    generation = _StagedMindMapGeneration(supervisor)
    api = object.__new__(WebArtifactsAPI)
    api._supervisor = supervisor
    api._generation = generation

    workflow = asyncio.create_task(api.generate_mind_map("nb"))
    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        generation.first_call_started,
        generation.release_first_call,
        workflow,
    )

    assert result.note_id == "note-1"


@pytest.mark.asyncio
async def test_artifact_rename_keeps_readback_admitted_after_drain() -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    rpc = _StagedRpc(supervisor, [None])
    listing = _StagedCalls(
        supervisor,
        None,
        Artifact(id="artifact-1", title="Renamed", _artifact_type=1, status=2),
    )
    api = object.__new__(WebArtifactsAPI)
    api._supervisor = supervisor
    api._rpc = rpc
    api._listing = SimpleNamespace(get_studio_only=listing.second)
    api._list_raw = None

    workflow = asyncio.create_task(api.rename("nb", "artifact-1", "Renamed"))
    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        rpc.first_call_started,
        rpc.release_first_call,
        workflow,
    )

    assert result == Artifact(id="artifact-1", title="Renamed", _artifact_type=1, status=2)


@pytest.mark.asyncio
async def test_web_notebook_update_keeps_readback_admitted_after_drain() -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    rpc = _StagedRpc(supervisor, [None])
    stages = _StagedCalls(supervisor, None, Notebook(id="nb", title="Renamed"))
    api = object.__new__(WebNotebooksAPI)
    api._supervisor = supervisor
    api._rpc = rpc
    api.get = stages.second  # type: ignore[method-assign]

    workflow = asyncio.create_task(api.update("nb", title="Renamed"))
    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        rpc.first_call_started,
        rpc.release_first_call,
        workflow,
    )

    assert result.title == "Renamed"


@pytest.mark.parametrize("backend", ["web", "android"])
@pytest.mark.asyncio
async def test_suggest_prompts_keeps_send_admitted_after_source_resolution_drain(
    backend: str,
) -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    stages = _StagedCalls(supervisor, ["source-1"], None)
    if backend == "web":
        api = object.__new__(WebNotebooksAPI)
        api._supervisor = supervisor
        api._rpc = _StagedRpc(supervisor, [[]], gate_first=False)
    else:
        api = object.__new__(AndroidNotebooksAPI)
        api._transport = _StagedAndroidTransport(
            supervisor,
            SimpleNamespace(suggestions=[]),
        )
    api.get_source_ids = stages.first  # type: ignore[method-assign]

    workflow = asyncio.create_task(api.suggest_prompts("nb"))
    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        stages.first_call_started,
        stages.release_first_call,
        workflow,
    )

    assert result == []


@pytest.mark.parametrize("operation", ["session_status", "cancel"])
@pytest.mark.asyncio
async def test_shared_chat_control_keeps_second_stage_admitted_after_drain(
    operation: str,
) -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    expected = ChatSessionStatus(generating=True, token="generation-1")
    stages = _StagedCalls(supervisor, "conversation-1", expected)
    api = object.__new__(WebChatAPI)
    api._supervisor = supervisor
    api._loop_guard = supervisor
    api.get_conversation_id = stages.first  # type: ignore[method-assign]
    if operation == "session_status":
        api._get_session_status = stages.second  # type: ignore[method-assign]
        workflow = asyncio.create_task(api.session_status("nb"))
    else:
        api._cancel_generation = stages.second  # type: ignore[method-assign]
        workflow = asyncio.create_task(api.cancel("nb"))

    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        stages.first_call_started,
        stages.release_first_call,
        workflow,
    )

    assert result == (expected if operation == "session_status" else None)


@pytest.mark.parametrize("backend", ["web", "android"])
@pytest.mark.asyncio
async def test_chat_history_keeps_turn_read_admitted_after_id_resolution_drain(
    backend: str,
) -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    second_result = [] if backend == "web" else SimpleNamespace(chat_turns=[])
    stages = _StagedCalls(supervisor, "conversation-1", second_result)
    if backend == "web":
        api = object.__new__(WebChatAPI)
        api._supervisor = supervisor
    else:
        api = object.__new__(AndroidChatAPI)
        api._transport = _StagedAndroidTransport(supervisor, None)
    api.get_conversation_id = stages.first  # type: ignore[method-assign]
    api.get_conversation_turns = stages.second  # type: ignore[method-assign]

    workflow = asyncio.create_task(api.get_history("nb"))
    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        stages.first_call_started,
        stages.release_first_call,
        workflow,
    )

    assert result == []


class _ConcurrentMetadataReads:
    def __init__(self, supervisor: CallSupervisor) -> None:
        self._supervisor = supervisor
        self._started = 0
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    async def _wait(self, label: str) -> None:
        self._started += 1
        if self._started == 2:
            self.both_started.set()
        await self.release.wait()
        async with self._supervisor.call_scope(label, label, None):
            pass

    async def get_notebook(self, notebook_id: str) -> Notebook:
        await self._wait("test.metadata.notebook")
        return Notebook(id=notebook_id, title="Metadata")

    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Any]:
        del notebook_id, strict
        await self._wait("test.metadata.sources")
        return []


@pytest.mark.asyncio
async def test_metadata_registers_gather_children_and_drains_after_both_settle() -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    reads = _ConcurrentMetadataReads(supervisor)
    api = WebNotebooksAPI(
        _StagedRpc(supervisor, []),
        sources_api=reads,
        supervisor=supervisor,
    )
    api.get = reads.get_notebook  # type: ignore[method-assign]

    workflow = asyncio.create_task(api.get_metadata("nb"))
    await reads.both_started.wait()
    generation = supervisor._current
    assert generation is not None
    # The outer workflow and both gather children are independently registered.
    assert generation.in_flight == 3
    draining = asyncio.create_task(lifecycle.drain())
    await _wait_for_drain(supervisor)
    reads.release.set()

    metadata, _ = await asyncio.gather(workflow, draining)
    assert metadata.notebook.title == "Metadata"
    assert generation.in_flight == 0
    assert not generation.depths
    await lifecycle.close(drain=False)


@pytest.mark.asyncio
async def test_web_suggest_prompts_is_fenced_after_forced_close_and_reopen() -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    stages = _StagedCalls(supervisor, ["source-1"], None)
    api = object.__new__(WebNotebooksAPI)
    api._supervisor = supervisor
    api._rpc = _StagedRpc(supervisor, [[]], gate_first=False)
    api.get_source_ids = stages.first  # type: ignore[method-assign]

    workflow = asyncio.create_task(api.suggest_prompts("nb"))
    await stages.first_call_started.wait()
    await lifecycle.close(drain=False)
    assert supervisor._retired
    await lifecycle.open()
    reopened_epoch = lifecycle._epoch
    stages.release_first_call.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await workflow
    assert lifecycle._epoch == reopened_epoch
    assert supervisor._retired == {}
    await lifecycle.close(drain=False)


@pytest.mark.asyncio
async def test_android_chat_history_cancellation_settles_all_admission() -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    stages = _StagedCalls(
        supervisor,
        "conversation-1",
        SimpleNamespace(chat_turns=[]),
    )
    api = object.__new__(AndroidChatAPI)
    api._transport = _StagedAndroidTransport(supervisor, None)
    api.get_conversation_id = stages.first  # type: ignore[method-assign]
    api.get_conversation_turns = stages.second  # type: ignore[method-assign]

    workflow = asyncio.create_task(api.get_history("nb"))
    await stages.first_call_started.wait()
    workflow.cancel()
    with pytest.raises(asyncio.CancelledError):
        await workflow

    generation = supervisor._current
    assert generation is not None
    assert generation.in_flight == 0
    assert not generation.depths
    await lifecycle.close(drain=False)


@pytest.mark.asyncio
async def test_source_polling_keeps_next_tick_admitted_after_drain() -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    stages = _StagedCalls(
        supervisor,
        Source(id="source-1", status=SourceStatus.PROCESSING),
        Source(id="source-1", status=SourceStatus.READY),
    )
    api = object.__new__(WebSourcesAPI)
    api._supervisor = supervisor
    api._poller = SourcePoller()
    api._spawn_child = supervisor.spawn_child

    calls = 0

    async def get_or_none(*args: Any, **kwargs: Any) -> Source:
        nonlocal calls
        calls += 1
        return await (stages.first if calls == 1 else stages.second)(*args, **kwargs)

    api.get_or_none = get_or_none  # type: ignore[method-assign]
    workflow = asyncio.create_task(
        api.wait_until_ready(
            "nb",
            "source-1",
            initial_interval=0,
            max_interval=0,
        )
    )
    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        stages.first_call_started,
        stages.release_first_call,
        workflow,
    )

    assert result.is_ready


@pytest.mark.asyncio
async def test_source_wait_fanout_registers_children_before_graceful_drain() -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    both_started = asyncio.Event()
    release = asyncio.Event()
    started = 0
    api = object.__new__(WebSourcesAPI)
    api._supervisor = supervisor
    api._poller = SourcePoller()
    api._spawn_child = supervisor.spawn_child

    async def wait_until_ready(
        notebook_id: str,
        source_id: str,
        timeout: float = 120,
        **kwargs: Any,
    ) -> Source:
        nonlocal started
        del notebook_id, timeout, kwargs
        started += 1
        if started == 2:
            both_started.set()
        await release.wait()
        async with supervisor.call_scope(f"test.wait.{source_id}", "source-wait", None):
            return Source(id=source_id, status=SourceStatus.READY)

    api.wait_until_ready = wait_until_ready  # type: ignore[method-assign]
    workflow = asyncio.create_task(api.wait_for_sources("nb", ["one", "two"]))
    await both_started.wait()
    generation = supervisor._current
    assert generation is not None
    assert generation.in_flight == 3
    draining = asyncio.create_task(lifecycle.drain())
    await _wait_for_drain(supervisor)
    release.set()

    results, _ = await asyncio.gather(workflow, draining)
    assert [source.id for source in results] == ["one", "two"]
    assert generation.in_flight == 0
    assert not generation.depths
    await lifecycle.close(drain=False)


class _StagedArtifactTransfer:
    def __init__(self, supervisor: CallSupervisor) -> None:
        self._supervisor = supervisor
        self.first_call_started = asyncio.Event()
        self.release_first_call = asyncio.Event()

    async def run(self, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        async with self._supervisor.call_scope("test.transfer.resolve", "resolve", None):
            self.first_call_started.set()
            await self.release_first_call.wait()
        async with self._supervisor.call_scope("test.transfer.settle", "settle", None):
            return "settled-output"


def _artifact_download_api(
    supervisor: CallSupervisor,
    transfer: _StagedArtifactTransfer,
) -> WebArtifactsAPI:
    api = object.__new__(WebArtifactsAPI)
    api._supervisor = supervisor
    api._downloads = SimpleNamespace(
        download_audio=transfer.run,
        download_video=transfer.run,
        download_infographic=transfer.run,
        download_slide_deck=transfer.run,
        download_report=transfer.run,
        download_mind_map=transfer.run,
        download_data_table=transfer.run,
        download_interactive_artifact=transfer.run,
    )
    return api


@pytest.mark.parametrize(
    "method",
    [
        "download_audio",
        "download_video",
        "download_infographic",
        "download_slide_deck",
        "download_report",
        "download_mind_map",
        "download_data_table",
        "download_quiz",
        "download_flashcards",
    ],
)
@pytest.mark.asyncio
async def test_web_artifact_download_keeps_settlement_admitted_after_drain(
    method: str,
) -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    transfer = _StagedArtifactTransfer(supervisor)
    api = _artifact_download_api(supervisor, transfer)

    workflow = asyncio.create_task(getattr(api, method)("nb", "output"))
    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        transfer.first_call_started,
        transfer.release_first_call,
        workflow,
    )

    assert result == "settled-output"


@pytest.mark.asyncio
async def test_web_artifact_download_cannot_cross_forced_close_and_reopen() -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    transfer = _StagedArtifactTransfer(supervisor)
    api = _artifact_download_api(supervisor, transfer)

    workflow = asyncio.create_task(api.download_audio("nb", "output"))
    await transfer.first_call_started.wait()
    await lifecycle.close(drain=False)
    await lifecycle.open()
    reopened_epoch = lifecycle._epoch
    transfer.release_first_call.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await workflow
    assert lifecycle._epoch == reopened_epoch
    assert supervisor._retired == {}
    await lifecycle.close(drain=False)


class _PausedUploadPipeline:
    def __init__(self, supervisor: CallSupervisor) -> None:
        self._supervisor = supervisor
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def upload_file(self, *args: Any, **kwargs: Any) -> Source:
        del args, kwargs
        self.entered.set()
        await self.release.wait()
        async with self._supervisor.call_scope("test.upload.settle", "upload", None):
            return Source(id="source-1", status=SourceStatus.PROCESSING)


def _android_upload_api(
    supervisor: CallSupervisor,
    pipeline: _PausedUploadPipeline,
) -> AndroidSourcesAPI:
    api = object.__new__(AndroidSourcesAPI)
    api._transport = _StagedAndroidTransport(supervisor, None)
    api._upload_pipeline = pipeline
    api._add_file_compat = None
    api._drive_download = None
    return api


@pytest.mark.asyncio
async def test_android_add_file_keeps_pipeline_settlement_admitted_after_drain(
    tmp_path: Path,
) -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    pipeline = _PausedUploadPipeline(supervisor)
    api = _android_upload_api(supervisor, pipeline)
    path = tmp_path / "source.pdf"
    path.write_bytes(b"pdf")

    workflow = asyncio.create_task(api.add_file("nb", path))
    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        pipeline.entered,
        pipeline.release,
        workflow,
    )

    assert result.id == "source-1"


class _StagedDriveDownload:
    def __init__(self, supervisor: CallSupervisor, path: Path) -> None:
        self._supervisor = supervisor
        self._path = path
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @asynccontextmanager
    async def __call__(self, document_id: str) -> AsyncIterator[tuple[Path, str, str]]:
        del document_id
        async with self._supervisor.call_scope("test.drive.download", "download", None):
            self.started.set()
            await self.release.wait()
        yield self._path, self._path.name, "application/pdf"


@pytest.mark.asyncio
async def test_android_drive_download_keeps_import_admitted_after_drain(
    tmp_path: Path,
) -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    pipeline = _PausedUploadPipeline(supervisor)
    pipeline.release.set()
    api = _android_upload_api(supervisor, pipeline)
    path = tmp_path / "drive.pdf"
    path.write_bytes(b"pdf")
    drive_download = _StagedDriveDownload(supervisor, path)
    api._drive_download = drive_download

    workflow = asyncio.create_task(api.add_drive_file("nb", "drive-id"))
    result = await _drain_after_first_stage(
        lifecycle,
        supervisor,
        drive_download.started,
        drive_download.release,
        workflow,
    )

    assert result.id == "source-1"


@pytest.mark.asyncio
async def test_android_add_file_cannot_cross_forced_close_and_reopen(
    tmp_path: Path,
) -> None:
    lifecycle, supervisor = _lifecycle()
    await lifecycle.open()
    pipeline = _PausedUploadPipeline(supervisor)
    api = _android_upload_api(supervisor, pipeline)
    path = tmp_path / "source.pdf"
    path.write_bytes(b"pdf")

    workflow = asyncio.create_task(api.add_file("nb", path))
    await pipeline.entered.wait()
    await lifecycle.close(drain=False)
    await lifecycle.open()
    reopened_epoch = lifecycle._epoch
    pipeline.release.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await workflow
    assert lifecycle._epoch == reopened_epoch
    assert supervisor._retired == {}
    await lifecycle.close(drain=False)
