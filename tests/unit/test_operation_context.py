"""Whole-operation deadline and evidence contracts."""

from __future__ import annotations

import asyncio
import warnings
from types import SimpleNamespace

import httpx
import pytest

from notebooklm import NotebookLMClient, OperationTimeoutError, RPCError, WaitTimeoutError
from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._artifact.polling import ArtifactPollingService
from notebooklm._auth.single_flight import SingleFlight
from notebooklm._client_metrics import ClientMetrics
from notebooklm._idempotency import OperationJournal, attach_journal_entry
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._runtime.operation_context import (
    adopt_operation_journal_entry,
    current_operation_context,
)
from notebooklm.auth import AuthTokens
from notebooklm.outcomes import CommitState
from notebooklm.rpc import RPCMethod
from notebooklm.types import GenerationStatus
from tests._helpers.client_factory import build_client_shell_for_tests

pytestmark = pytest.mark.asyncio


def _supervisor(*, timeout: float | None = None) -> CallSupervisor:
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        max_concurrent_rpcs=1,
        operation_timeout=timeout,
    )
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    return supervisor


async def test_default_operation_deadline_uses_public_timeout_type() -> None:
    supervisor = _supervisor(timeout=0.01)

    with pytest.raises(OperationTimeoutError) as caught:
        async with supervisor.operation_scope("sources.add_url"):
            await asyncio.sleep(10)

    assert isinstance(caught.value, WaitTimeoutError)
    assert caught.value.operation_metadata is not None
    assert caught.value.operation_metadata.operation == "sources.add_url"
    cancelling = getattr(asyncio.current_task(), "cancelling", None)
    if callable(cancelling):
        assert cancelling() == 0


async def test_nested_explicit_operation_can_only_shorten_parent() -> None:
    supervisor = _supervisor()

    with pytest.raises(OperationTimeoutError):
        async with supervisor.operation_scope("outer", timeout=1.0):
            async with supervisor.operation_scope("inner", timeout=0.01) as lease:
                assert lease.context.remaining() is not None
                assert lease.context.remaining() <= 0.02
                await asyncio.sleep(10)


async def test_public_operation_none_is_unbounded_and_explicit_timeout_applies() -> None:
    supervisor = _supervisor(timeout=0.01)
    client = object.__new__(NotebookLMClient)
    client._collaborators = SimpleNamespace(call_supervisor=supervisor)

    async with client.operation(timeout=None):
        await asyncio.sleep(0.02)

    with pytest.raises(OperationTimeoutError):
        async with client.operation(timeout=0.01):
            await asyncio.sleep(10)


async def test_registered_child_gets_child_owned_workflow_context() -> None:
    supervisor = _supervisor()

    async with supervisor.operation_scope("parent", timeout=1.0) as parent:

        async def _child() -> None:
            context = current_operation_context(supervisor)
            assert context is not None
            assert context.owner_task is asyncio.current_task()
            assert context.journal is parent.context.journal
            assert context.absolute_deadline == parent.context.absolute_deadline

        child = await supervisor.spawn_child("exclusive-child", _child)
        await child


async def test_copied_task_does_not_inherit_parent_operation_context() -> None:
    supervisor = _supervisor()

    async with supervisor.operation_scope("parent", timeout=1.0):

        async def _copied_task() -> None:
            assert current_operation_context(supervisor) is None
            async with supervisor.call_scope("independent", "READ", None) as lease:
                assert lease.deadline is None

        await asyncio.create_task(_copied_task())


async def test_one_workflow_journal_retains_distinct_semantic_invocations() -> None:
    supervisor = _supervisor()

    async with supervisor.operation_scope("client.operation") as lease:
        first = adopt_operation_journal_entry(
            supervisor,
            method="CREATE_NOTEBOOK",
            operation="notebooks.create",
        )
        second = adopt_operation_journal_entry(
            supervisor,
            method="ADD_SOURCE",
            operation="sources.add_url",
        )

        assert first is not None and second is not None
        assert first.identity.invocation_id != second.identity.invocation_id
        assert first.identity.operation == "notebooks.create"
        assert second.identity.operation == "sources.add_url"
        assert lease.context.journal.entries == (first, second)


async def test_workflow_aggregation_selects_escaping_send_without_mutating_first_leaf() -> None:
    supervisor = _supervisor()
    failure = RPCError("second response was lost")

    with pytest.raises(RPCError) as caught:
        async with supervisor.operation_scope("client.operation"):
            first = adopt_operation_journal_entry(
                supervisor,
                method="CREATE_NOTEBOOK",
                operation="notebooks.create",
            )
            second = adopt_operation_journal_entry(
                supervisor,
                method="ADD_SOURCE",
                operation="sources.add_url",
            )
            assert first is not None and second is not None
            first.mark_dispatched()
            first.record(
                CommitState.CONFIRMED,
                "decoded first response",
                known_resource_ids=("notebook-1",),
            )
            second.source_id = "source-2"
            second.remember_resource_ids("source-2")
            second.mark_dispatched()
            attach_journal_entry(failure, second)
            escaping = failure.operation_metadata
            raise failure

    metadata = caught.value.operation_metadata
    assert metadata is not None
    assert metadata.invocation_id == second.identity.invocation_id
    assert metadata.operation == "sources.add_url"
    assert metadata.source_id == "source-2"
    assert metadata.known_resource_ids == ("notebook-1", "source-2")
    assert metadata.entries[0] == first.snapshot()
    assert metadata.entries[0].source_id is None
    assert metadata.entries[0].known_resource_ids == ("notebook-1",)
    assert metadata.entries[1] is escaping
    assert metadata.entries[1] == second.snapshot()
    assert metadata.entries[1].known_resource_ids == ("source-2",)


async def test_swallowed_owned_cancellation_still_raises_timeout() -> None:
    supervisor = _supervisor()

    with pytest.raises(OperationTimeoutError):
        async with supervisor.operation_scope("swallowed", timeout=0.01):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass
    cancelling = getattr(asyncio.current_task(), "cancelling", None)
    if callable(cancelling):
        assert cancelling() == 0


@pytest.mark.skipif(
    not hasattr(asyncio.Task, "uncancel"),
    reason="concurrent cancellation accounting is available on Python 3.11+",
)
async def test_swallowed_deadline_with_same_tick_external_cancel_stays_cancelled() -> None:
    supervisor = _supervisor()
    task = asyncio.current_task()
    assert task is not None

    with pytest.raises(asyncio.CancelledError):
        async with supervisor.operation_scope("concurrent", timeout=0.02) as lease:
            assert lease.context.absolute_deadline is not None
            lease.context.loop.call_at(lease.context.absolute_deadline, task.cancel)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

    # The operation context removed exactly its own request. The remaining
    # count belongs to the deliberately injected external cancellation.
    assert task.cancelling() == 1
    assert task.uncancel() == 0


async def test_retired_generation_wins_when_deadline_cancellation_is_swallowed() -> None:
    supervisor = _supervisor()

    with pytest.raises(asyncio.CancelledError):
        async with supervisor.operation_scope("forced-close", timeout=0.01):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                supervisor._current = None

    cancelling = getattr(asyncio.current_task(), "cancelling", None)
    if callable(cancelling):
        assert cancelling() == 0


async def test_settlement_absorbs_deadline_cancel_but_preserves_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = _supervisor()
    failure = RPCError("body failed")

    async def _held_settlement(*args: object, **kwargs: object) -> None:
        del args, kwargs
        await asyncio.sleep(10)

    monkeypatch.setattr(supervisor, "_await_settlement", _held_settlement)
    with pytest.raises(RPCError) as caught:
        async with supervisor.operation_scope("settlement", timeout=0.01):
            raise failure

    assert caught.value is failure
    assert failure.operation_metadata is not None
    assert failure.operation_metadata.operation == "settlement"
    cancelling = getattr(asyncio.current_task(), "cancelling", None)
    if callable(cancelling):
        assert cancelling() == 0


async def test_rpc_queue_expiry_uses_operation_timeout_and_never_dispatches() -> None:
    supervisor = _supervisor()
    occupied = asyncio.Event()
    release = asyncio.Event()

    async def _holder() -> None:
        async with supervisor.call_scope("holder", "READ", None):
            occupied.set()
            await release.wait()

    holder = asyncio.create_task(_holder())
    await occupied.wait()
    dispatched = False
    try:
        with pytest.raises(OperationTimeoutError):
            async with supervisor.operation_scope("queued", timeout=0.01):
                async with supervisor.call_scope("queued rpc", "WRITE", None):
                    dispatched = True
    finally:
        release.set()
        await holder
    assert dispatched is False


async def test_external_cancellation_is_not_translated() -> None:
    supervisor = _supervisor()
    entered = asyncio.Event()

    async def _wait() -> None:
        async with supervisor.operation_scope("external", timeout=10.0):
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(_wait())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_timeout_carries_dispatched_mutation_evidence() -> None:
    supervisor = _supervisor()
    dispatched = False

    async def _create() -> None:
        nonlocal dispatched
        async with supervisor.operation_scope("create", timeout=1.0):
            entry = OperationJournal("notebooks.create").new_entry(method="CREATE_NOTEBOOK")
            entry.mark_dispatched()
            dispatched = True
            await asyncio.sleep(10)

    with pytest.raises(OperationTimeoutError) as caught:
        await asyncio.wait_for(_create(), timeout=5.0)

    assert dispatched
    metadata = caught.value.operation_metadata
    assert metadata is not None
    assert metadata.commit_state is CommitState.UNKNOWN
    assert metadata.method == "CREATE_NOTEBOOK"
    assert metadata.entries
    assert classify(caught.value).category is ErrorCategory.RPC


async def test_no_write_operation_timeout_projects_as_timeout() -> None:
    supervisor = _supervisor()

    with pytest.raises(OperationTimeoutError) as caught:
        async with supervisor.operation_scope("read", timeout=0.01):
            await asyncio.sleep(10)

    classified = classify(caught.value)
    assert classified.category is ErrorCategory.TIMEOUT
    assert classified.retriable is True


async def test_web_terminal_auto_adopts_active_operation_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = build_client_shell_for_tests(
        AuthTokens(csrf_token="csrf", session_id="session", cookies={})
    )
    never = asyncio.Event()
    post_entered = asyncio.Event()

    async with client:

        async def _post(*args: object, **kwargs: object) -> httpx.Response:
            del args, kwargs
            post_entered.set()
            await never.wait()
            raise AssertionError("unreachable")

        terminal = client._web_runtime.composed.transport
        monkeypatch.setattr(terminal._kernel, "post", _post)

        async def _create() -> None:
            # Keep this a post-dispatch metadata test on slow/instrumented
            # platforms, rather than expiring during request preparation.
            async with client.operation(timeout=1.0):
                await client._web_runtime.executor.rpc_call(
                    RPCMethod.CREATE_NOTEBOOK,
                    ["title", None],
                )

        with pytest.raises(OperationTimeoutError) as caught:
            await asyncio.wait_for(_create(), timeout=5.0)

    assert post_entered.is_set()
    metadata = caught.value.operation_metadata
    assert metadata is not None
    assert metadata.commit_state is CommitState.UNKNOWN
    assert metadata.method == RPCMethod.CREATE_NOTEBOOK.value
    assert metadata.operation == "create_notebook"


async def test_expired_context_refuses_nested_dispatch() -> None:
    supervisor = _supervisor()
    dispatched = False

    async def _invoke(_lease: object) -> None:
        nonlocal dispatched
        dispatched = True

    with pytest.raises(OperationTimeoutError):
        async with supervisor.operation_scope("outer", timeout=0.01) as lease:
            lease.context.absolute_deadline = lease.context.loop.time()
            await supervisor.run("rpc", "LIST", None, _invoke)

    assert dispatched is False


async def test_other_client_context_does_not_supply_a_deadline() -> None:
    first = _supervisor()
    second = _supervisor()

    async with (
        first.operation_scope("first", timeout=1.0),
        second.call_scope("second", None, None) as lease,
    ):
        assert lease.deadline is None


async def test_poll_follower_warns_for_ignored_knobs_and_callback_cardinality() -> None:
    supervisor = _supervisor()
    service = ArtifactPollingService(supervisor=supervisor)
    release = asyncio.Event()

    async def _poll(_notebook_id: str, task_id: str) -> GenerationStatus:
        await release.wait()
        return GenerationStatus(task_id=task_id, status="completed")

    leader = asyncio.create_task(
        service.wait_for_completion("nb", "task", timeout=10.0, poll_status=_poll)
    )
    while service.poll_registry.get(("nb", "task")) is None:
        await asyncio.sleep(0)

    callback_statuses: list[str] = []
    with pytest.warns(DeprecationWarning) as caught:
        follower = asyncio.create_task(
            service.wait_for_completion(
                "nb",
                "task",
                timeout=20.0,
                poll_status=_poll,
                on_status_change=lambda status: callback_statuses.append(status.status),
            )
        )
        await asyncio.sleep(0)
    assert len(caught) == 2
    assert "timeout=20.0" in str(caught[0].message)
    assert "on_status_change" in str(caught[1].message)

    release.set()
    await asyncio.gather(leader, follower)
    assert callback_statuses == ["completed"]


async def test_poll_follower_with_identical_effective_knobs_is_silent() -> None:
    supervisor = _supervisor()
    service = ArtifactPollingService(supervisor=supervisor)
    release = asyncio.Event()

    async def _poll(_notebook_id: str, task_id: str) -> GenerationStatus:
        await release.wait()
        return GenerationStatus(task_id=task_id, status="completed")

    leader = asyncio.create_task(
        service.wait_for_completion("nb", "task", timeout=10.0, poll_status=_poll)
    )
    while service.poll_registry.get(("nb", "task")) is None:
        await asyncio.sleep(0)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        follower = asyncio.create_task(
            service.wait_for_completion("nb", "task", timeout=10.0, poll_status=_poll)
        )
        await asyncio.sleep(0)
    assert caught == []
    release.set()
    await asyncio.gather(leader, follower)


async def test_poll_leader_survives_first_waiters_operation_deadline() -> None:
    supervisor = _supervisor()
    service = ArtifactPollingService(supervisor=supervisor)
    poll_started = asyncio.Event()
    release = asyncio.Event()

    async def _poll(_notebook_id: str, task_id: str) -> GenerationStatus:
        poll_started.set()
        await release.wait()
        return GenerationStatus(task_id=task_id, status="completed")

    async def _first_waiter() -> GenerationStatus:
        async with supervisor.operation_scope("first waiter", timeout=0.05):
            return await service.wait_for_completion(
                "nb",
                "shared-task",
                timeout=10.0,
                poll_status=_poll,
            )

    first = asyncio.create_task(_first_waiter())
    await poll_started.wait()
    pending = service.poll_registry.get(("nb", "shared-task"))
    assert pending is not None
    _future, poll_task = pending
    while poll_task is None:
        await asyncio.sleep(0)
        pending = service.poll_registry.get(("nb", "shared-task"))
        assert pending is not None
        _future, poll_task = pending
    assert poll_task is not None
    follower = asyncio.create_task(
        service.wait_for_completion(
            "nb",
            "shared-task",
            timeout=10.0,
            poll_status=_poll,
        )
    )
    await asyncio.sleep(0)

    with pytest.raises(OperationTimeoutError):
        await first
    assert not poll_task.done()

    release.set()
    assert (await follower).status == "completed"


async def test_operation_timeout_detaches_from_shared_auth_producer() -> None:
    supervisor = _supervisor()
    single_flight = SingleFlight()
    release = asyncio.Event()

    async def _producer() -> str:
        await release.wait()
        return "fresh"

    _is_leader, flight = single_flight.claim(("profile", "refresh"), _producer)
    with pytest.raises(OperationTimeoutError):
        async with supervisor.operation_scope("auth.refresh", timeout=0.01):
            await single_flight.await_flight(flight)

    assert flight.task is not None and not flight.task.done()
    release.set()
    assert await single_flight.await_flight(flight) == "fresh"
