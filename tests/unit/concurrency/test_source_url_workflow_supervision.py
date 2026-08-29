"""Generation-bound supervision for multi-call URL source workflows."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm._web.sources import WebSourcesAPI
from notebooklm.auth import AuthTokens
from notebooklm.exceptions import NonIdempotentRetryError, ValidationError
from notebooklm.types import Source
from tests._helpers.client_factory import build_client_shell_for_tests


def _supervisor() -> CallSupervisor:
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        drain_tracker=TransportDrainTracker(),
        max_concurrent_rpcs=None,
    )
    supervisor.set_bound_loop(asyncio.get_running_loop())
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    return supervisor


def _sources(supervisor: CallSupervisor) -> WebSourcesAPI:
    return WebSourcesAPI(
        MagicMock(),
        supervisor=supervisor,
        uploader=MagicMock(),
    )


@pytest.mark.asyncio
async def test_drain_after_url_admission_allows_full_workflow_to_finish() -> None:
    supervisor = _supervisor()
    api = _sources(supervisor)
    workflow_admitted = asyncio.Event()
    continue_workflow = asyncio.Event()
    stages: list[str] = []

    async def _list_sources(*_args: object, **_kwargs: object) -> list[Source]:
        if not stages:
            workflow_admitted.set()
            await continue_workflow.wait()
        async with supervisor.call_scope("baseline", None, None):
            stages.append("baseline")
        return []

    async def _create(*_args: object, **_kwargs: object) -> list[object]:
        async with supervisor.call_scope("create", None, None):
            stages.append("create")
        return [[[["src_1"], "Upstream title", [None, 0], [None, 2]]]]

    async def _wait(*_args: object, **_kwargs: object) -> Source:
        async with supervisor.call_scope("readiness", None, None):
            stages.append("readiness")
        return Source(id="src_1", title="Upstream title")

    async def _rename(*_args: object, **_kwargs: object) -> Source:
        async with supervisor.call_scope("rename", None, None):
            stages.append("rename")
        return Source(id="src_1", title="Requested title")

    api.list = _list_sources  # type: ignore[method-assign]
    api._add_url_source = _create  # type: ignore[method-assign]
    api.wait_until_ready = _wait  # type: ignore[method-assign]
    api.rename = _rename  # type: ignore[method-assign]

    task = asyncio.create_task(
        api.add_url(
            "nb_1",
            "https://example.test",
            wait=True,
            title="Requested title",
        )
    )
    await asyncio.wait_for(workflow_admitted.wait(), timeout=1.0)
    await supervisor.stop_accepting(1)
    continue_workflow.set()

    result = await asyncio.wait_for(task, timeout=1.0)
    await supervisor.wait_for_idle(1, timeout=1.0)

    assert result.title == "Requested title"
    assert stages == ["baseline", "create", "readiness", "rename"]


@pytest.mark.asyncio
async def test_drain_after_text_write_allows_readiness_leg_to_finish() -> None:
    supervisor = _supervisor()
    api = _sources(supervisor)
    write_finished = asyncio.Event()
    continue_workflow = asyncio.Event()
    stages: list[str] = []

    class _Adder:
        async def add_text(self, *_args: object, **_kwargs: object) -> Source:
            async with supervisor.call_scope("text write", None, None):
                stages.append("write")
            write_finished.set()
            await continue_workflow.wait()
            async with supervisor.call_scope("text readiness", None, None):
                stages.append("readiness")
            return Source(id="src_text", title="Notes")

    api._adder = _Adder()  # type: ignore[assignment]
    task = asyncio.create_task(api.add_text("nb_1", "Notes", "body", wait=True))
    await asyncio.wait_for(write_finished.wait(), timeout=1.0)
    await supervisor.stop_accepting(1)
    continue_workflow.set()

    result = await asyncio.wait_for(task, timeout=1.0)
    await supervisor.wait_for_idle(1, timeout=1.0)

    assert result.id == "src_text"
    assert stages == ["write", "readiness"]


@pytest.mark.asyncio
async def test_drain_after_drive_write_allows_readiness_and_title_legs_to_finish() -> None:
    supervisor = _supervisor()
    api = _sources(supervisor)
    write_finished = asyncio.Event()
    continue_workflow = asyncio.Event()
    stages: list[str] = []

    class _Adder:
        async def add_drive(self, *_args: object, **_kwargs: object) -> Source:
            async with supervisor.call_scope("drive baseline", None, None):
                stages.append("baseline")
            async with supervisor.call_scope("drive write", None, None):
                stages.append("write")
            write_finished.set()
            await continue_workflow.wait()
            async with supervisor.call_scope("drive readiness", None, None):
                stages.append("readiness")
            return Source(id="src_drive", title="Upstream title")

    async def _rename(*_args: object, **_kwargs: object) -> Source:
        async with supervisor.call_scope("drive rename", None, None):
            stages.append("rename")
        return Source(id="src_drive", title="Requested title")

    api._adder = _Adder()  # type: ignore[assignment]
    api.rename = _rename  # type: ignore[method-assign]
    task = asyncio.create_task(api.add_drive("nb_1", "drive_1", "Requested title", wait=True))
    await asyncio.wait_for(write_finished.wait(), timeout=1.0)
    await supervisor.stop_accepting(1)
    continue_workflow.set()

    result = await asyncio.wait_for(task, timeout=1.0)
    await supervisor.wait_for_idle(1, timeout=1.0)

    assert result.title == "Requested title"
    assert stages == ["baseline", "write", "readiness", "rename"]


@pytest.mark.asyncio
async def test_source_validation_runs_before_workflow_admission() -> None:
    supervisor = _supervisor()
    api = _sources(supervisor)
    adder = MagicMock()
    api._adder = adder  # type: ignore[assignment]
    await supervisor.stop_accepting(1)

    with pytest.raises(NonIdempotentRetryError, match="cannot be marked idempotent"):
        await api.add_text("nb_1", "Notes", "body", idempotent=True)
    with pytest.raises(ValidationError, match="file_id cannot be empty"):
        await api.add_drive("nb_1", "  ", "Drive title")

    adder.add_text.assert_not_called()
    adder.add_drive.assert_not_called()


@pytest.mark.asyncio
async def test_forced_close_and_reopen_fences_old_url_workflow_from_new_epoch() -> None:
    supervisor = _supervisor()
    api = _sources(supervisor)
    workflow_admitted = asyncio.Event()
    continue_workflow = asyncio.Event()
    mutations: list[str] = []

    class _Adder:
        async def add_url(self, *_args: object, **_kwargs: object) -> Source:
            workflow_admitted.set()
            await continue_workflow.wait()
            async with supervisor.call_scope("url mutation", None, None):
                mutations.append("old workflow mutated active resources")
            return Source(id="src_old")

    api._adder = _Adder()  # type: ignore[assignment]
    task = asyncio.create_task(api.add_url("nb_1", "https://example.test"))
    await asyncio.wait_for(workflow_admitted.wait(), timeout=1.0)

    await supervisor.begin_closing(1)
    supervisor.mark_closed(1)
    supervisor.prepare_generation(2)
    supervisor.start_accepting(2)
    continue_workflow.set()

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await asyncio.wait_for(task, timeout=1.0)

    assert mutations == []
    async with supervisor.operation_scope("new epoch") as lease:
        assert lease.epoch == 2


@pytest.mark.asyncio
async def test_timeout_before_title_never_emits_rename() -> None:
    supervisor = _supervisor()
    api = _sources(supervisor)
    rename = AsyncMock()

    api.list = AsyncMock(return_value=[])  # type: ignore[method-assign]
    api._add_url_source = AsyncMock(  # type: ignore[method-assign]
        return_value=[[[["src_1"], "Upstream title", [None, 0], [None, 2]]]]
    )
    api.wait_until_ready = AsyncMock(  # type: ignore[method-assign]
        side_effect=TimeoutError("readiness timed out")
    )
    api.rename = rename  # type: ignore[method-assign]

    with pytest.raises(TimeoutError, match="readiness timed out"):
        await api.add_url(
            "nb_1",
            "https://example.test",
            wait=True,
            title="Requested title",
        )

    rename.assert_not_awaited()
    await supervisor.wait_for_idle(1, timeout=1.0)


@pytest.mark.asyncio
async def test_cancellation_before_title_never_emits_rename() -> None:
    supervisor = _supervisor()
    api = _sources(supervisor)
    readiness_started = asyncio.Event()
    release_readiness = asyncio.Event()
    rename = AsyncMock()

    async def _wait(*_args: object, **_kwargs: object) -> Source:
        readiness_started.set()
        await release_readiness.wait()
        return Source(id="src_1", title="Upstream title")

    api.list = AsyncMock(return_value=[])  # type: ignore[method-assign]
    api._add_url_source = AsyncMock(  # type: ignore[method-assign]
        return_value=[[[["src_1"], "Upstream title", [None, 0], [None, 2]]]]
    )
    api.wait_until_ready = _wait  # type: ignore[method-assign]
    api.rename = rename  # type: ignore[method-assign]

    task = asyncio.create_task(
        api.add_url(
            "nb_1",
            "https://example.test",
            wait=True,
            title="Requested title",
        )
    )
    await readiness_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rename.assert_not_awaited()
    await supervisor.wait_for_idle(1, timeout=1.0)


@pytest.mark.asyncio
async def test_old_url_workflow_is_rejected_before_epoch_two_kernel_or_auth_access() -> None:
    auth = AuthTokens(csrf_token="csrf", session_id="sid", cookies={"SID": "cookie"})
    client = build_client_shell_for_tests(auth)
    await client.__aenter__()
    baseline_started = asyncio.Event()
    continue_baseline = asyncio.Event()
    original_list = client.sources.list

    async def _gated_list(*args: Any, **kwargs: Any) -> list[Source]:
        baseline_started.set()
        await continue_baseline.wait()
        return await original_list(*args, **kwargs)

    client.sources.list = _gated_list  # type: ignore[method-assign]
    old_workflow = asyncio.create_task(
        client.sources.add_url("nb_1", "https://example.test", title="Never emitted")
    )
    await baseline_started.wait()

    await client.close(drain=False)
    await client.__aenter__()
    kernel_access = MagicMock(wraps=client._collaborators.kernel.assert_epoch)
    auth_access = AsyncMock(wraps=client._collaborators.auth_coord.snapshot)
    client._collaborators.kernel.assert_epoch = kernel_access  # type: ignore[method-assign]
    client._collaborators.auth_coord.snapshot = auth_access  # type: ignore[method-assign]
    continue_baseline.set()

    try:
        with pytest.raises(RuntimeError, match="retired resource generation"):
            await old_workflow
        kernel_access.assert_not_called()
        auth_access.assert_not_awaited()
    finally:
        await client.close(drain=False)


@pytest.mark.asyncio
async def test_batch_supervision_remains_admitted_through_reconciliation() -> None:
    supervisor = _supervisor()
    api = _sources(supervisor)
    write_finished = asyncio.Event()
    allow_reconciliation = asyncio.Event()
    stages: list[str] = []

    class _BatchAdder:
        async def add_urls(self, *_args: object, **_kwargs: Any) -> list[object]:
            async with supervisor.call_scope("batch write", None, None):
                stages.append("write")
            write_finished.set()
            await allow_reconciliation.wait()
            async with supervisor.call_scope("batch reconciliation", None, None):
                stages.append("reconciliation")
            return []

    api._batch_adder = _BatchAdder()  # type: ignore[assignment]
    task = asyncio.create_task(
        api._add_urls_batch("nb_1", ["https://one.test", "https://two.test"])
    )
    await asyncio.wait_for(write_finished.wait(), timeout=1.0)
    await supervisor.stop_accepting(1)

    idle = asyncio.create_task(supervisor.wait_for_idle(1, timeout=1.0))
    await asyncio.sleep(0)
    assert not idle.done(), "the outer batch workflow must remain admitted during reconciliation"

    allow_reconciliation.set()
    assert await asyncio.wait_for(task, timeout=1.0) == []
    await asyncio.wait_for(idle, timeout=1.0)
    assert stages == ["write", "reconciliation"]
