"""Accepted-kickoff generation faults across polling and operation budgets."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from notebooklm import OperationTimeoutError, ServerError
from notebooklm._app.generate import execute_generation
from notebooklm._app.generation_requests import AudioGenerationRequest
from notebooklm.outcomes import CommitState
from notebooklm.rpc import RPCMethod

from .common import ScenarioResult
from .http import HttpFaultServer, Reply, Route, Stall
from .web import build_fault_client, list_response, rpc_response

_CREATE_ARTIFACT = Route.rpc(RPCMethod.CREATE_ARTIFACT.value)
_LIST_ARTIFACTS = Route.rpc(RPCMethod.LIST_ARTIFACTS.value)
_READ = Route.rpc(RPCMethod.LIST_NOTEBOOKS.value)
_CLOSE_TIMEOUT = 2.0


async def _resolve_notebook(_client: Any, notebook_id: str) -> str:
    return notebook_id


async def _resolve_sources(
    _client: Any, _notebook_id: str, source_ids: tuple[str, ...]
) -> list[str]:
    return list(source_ids)


def _request(*, timeout: float) -> AudioGenerationRequest:
    return AudioGenerationRequest(
        notebook_id="nb-workflow",
        source_ids=(),
        wait=True,
        timeout=timeout,
        interval=0.01,
    )


def _kickoff(task_id: str) -> Reply:
    return Reply(
        body=rpc_response(
            _CREATE_ARTIFACT.rpc_id or "",
            [[task_id, "Audio", 1, None, 1]],
        )
    )


def _completed_poll(task_id: str) -> Reply:
    artifact = [
        task_id,
        "Audio",
        1,
        None,
        3,
        None,
        [
            None,
            None,
            None,
            None,
            None,
            [["https://lh3.googleusercontent.com/audio", None, "audio/wav"]],
        ],
    ]
    return Reply(body=rpc_response(_LIST_ARTIFACTS.rpc_id or "", [[artifact]]))


@asynccontextmanager
async def _cohort(
    result: ScenarioResult,
    server: HttpFaultServer,
    *,
    operation_timeout: float | None = None,
    rpc_timeout: float = 1.0,
    poll_sleep: Callable[[float], Awaitable[Any]] | None = None,
    owned_tasks: list[asyncio.Task[Any]] | None = None,
) -> AsyncIterator[Any]:
    client = None
    primary_error: BaseException | None = None
    try:
        await server.__aenter__()
        client = build_fault_client(
            server,
            timeout=rpc_timeout,
            server_error_max_retries=0,
            operation_timeout=operation_timeout,
        )
        if poll_sleep is not None:
            client.artifacts._polling._sleep = poll_sleep
        await client.__aenter__()
        yield client
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        for task in owned_tasks or []:
            if not task.done():
                task.cancel()
        if owned_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*owned_tasks, return_exceptions=True), _CLOSE_TIMEOUT
                )
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            if client is not None:
                await asyncio.wait_for(client.close(drain=False), _CLOSE_TIMEOUT)
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            await asyncio.wait_for(server.aclose(), _CLOSE_TIMEOUT)
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            server.assert_drained()
        except BaseException as error:
            cleanup_errors.append(error)
        result.record(
            "http_trace",
            requests=[
                {
                    "sequence": item.sequence,
                    "rpc_id": item.route.rpc_id,
                    "action": item.action,
                }
                for item in server.journal
            ],
        )
        result.record(
            "cleanup",
            owner="generation",
            client_closed=client is None or not client._lifecycle.is_open(),
            active_handlers=server.active_handlers,
            server_errors=list(server.errors),
            remaining=server.remaining(),
            cleanup_error_types=[type(error).__name__ for error in cleanup_errors],
            primary_error_type=(None if primary_error is None else type(primary_error).__name__),
        )
        exit_error = next(
            (
                error
                for error in cleanup_errors
                if isinstance(error, (KeyboardInterrupt, SystemExit))
            ),
            None,
        )
        if exit_error is not None and not isinstance(
            primary_error, (KeyboardInterrupt, SystemExit)
        ):
            raise exit_error
        if primary_error is None:
            result.require(
                "generation_client_closed", client is None or not client._lifecycle.is_open()
            )
            result.require("generation_handlers_settled", server.active_handlers == 0)
            result.require("generation_server_clean", not server.errors and server.remaining() == 0)
            if cleanup_errors:
                raise cleanup_errors[0]


async def accepted_kickoff_poll_exhaustion(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(_CREATE_ARTIFACT, _kickoff("task-1"))
    server.enqueue(_LIST_ARTIFACTS, *(Reply(503) for _ in range(4)))
    server.enqueue(
        _READ,
        Reply(body=list_response(_READ.rpc_id or "", [("recovered", "Recovered")])),
    )
    error: BaseException | None = None
    backoffs: list[float] = []

    async def record_backoff(delay: float) -> None:
        backoffs.append(delay)
        await asyncio.sleep(0)

    async with _cohort(result, server, poll_sleep=record_backoff) as client:
        try:
            await execute_generation(
                _request(timeout=12.0),
                client,
                notebook_resolver=_resolve_notebook,
                source_resolver=_resolve_sources,
            )
        except BaseException as caught:
            error = caught
        recovered = await client.notebooks.list()
    metadata = getattr(error, "operation_metadata", None)
    result.record(
        "generation_poll_failure",
        error=None if error is None else type(error).__name__,
        commit_state=(None if metadata is None else str(metadata.commit_state)),
        known_ids=[] if metadata is None else list(metadata.known_resource_ids),
    )
    result.record("retry_arithmetic", clock="injected_instance_sleep", delays=backoffs)
    result.require("poll_exhaustion_backoff_arithmetic", backoffs == [2.0, 4.0, 8.0])
    result.require("poll_exhaustion_server_error", isinstance(error, ServerError))
    result.require(
        "poll_exhaustion_one_kickoff",
        sum(item.route == _CREATE_ARTIFACT for item in server.journal) == 1,
    )
    result.require(
        "poll_exhaustion_four_polls",
        sum(item.route == _LIST_ARTIFACTS for item in server.journal) == 4,
    )
    result.require(
        "poll_exhaustion_kickoff_retained",
        metadata is not None
        and metadata.commit_state is CommitState.CONFIRMED
        and "task-1" in metadata.known_resource_ids,
    )
    result.require("poll_exhaustion_recovery", [row.id for row in recovered] == ["recovered"])


async def shared_poller_original_operation_timeout(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _CREATE_ARTIFACT,
        Stall("headers", "kickoff", _kickoff("task-4")),
    )
    server.enqueue(
        _LIST_ARTIFACTS,
        Stall("headers", "poll", _completed_poll("task-4")),
    )
    server.enqueue(
        _READ,
        Reply(body=list_response(_READ.rpc_id or "", [("recovered", "Recovered")])),
    )
    error: BaseException | None = None
    tasks: list[asyncio.Task[Any]] = []
    async with _cohort(
        result, server, operation_timeout=2.0, rpc_timeout=4.0, owned_tasks=tasks
    ) as client:
        execution = asyncio.create_task(
            execute_generation(
                _request(timeout=5.0),
                client,
                notebook_resolver=_resolve_notebook,
                source_resolver=_resolve_sources,
            )
        )
        tasks.append(execution)
        await server.wait_for_requests(_CREATE_ARTIFACT, 1)
        await asyncio.sleep(0.5)
        server.release("kickoff")
        await server.wait_for_requests(_LIST_ARTIFACTS, 1)
        follower = asyncio.create_task(
            client.artifacts.wait_for_completion(
                "nb-workflow",
                "task-4",
                initial_interval=0.01,
                max_interval=10.0,
                timeout=5.0,
            )
        )
        tasks.append(follower)
        await asyncio.sleep(0)
        try:
            await execution
        except BaseException as caught:
            error = caught
        key = ("nb-workflow", "task-4")
        result.require(
            "shared_timeout_leader_survives_original",
            client.artifacts._poll_registry.get(key) is not None,
        )
        polls_before_release = sum(item.route == _LIST_ARTIFACTS for item in server.journal)
        server.release("poll")
        follower_status = await follower
        recovered = await client.notebooks.list()
        result.require("shared_timeout_public_error", isinstance(error, OperationTimeoutError))
        result.require(
            "shared_timeout_one_kickoff",
            sum(item.route == _CREATE_ARTIFACT for item in server.journal) == 1,
        )
        result.require("shared_timeout_one_poll", polls_before_release == 1)
        result.require(
            "shared_timeout_follower_retains_task",
            follower_status.task_id == "task-4" and follower_status.is_complete,
        )
        result.require(
            "shared_timeout_registry_settled", client.artifacts._poll_registry.get(key) is None
        )
        result.require("shared_timeout_recovery", [row.id for row in recovered] == ["recovered"])


IMPLEMENTATIONS: dict[str, Callable[[ScenarioResult], Awaitable[None]]] = {
    "workflow_generation_poll_exhaustion": accepted_kickoff_poll_exhaustion,
    "workflow_generation_shared_original_timeout": shared_poller_original_operation_timeout,
}

SCENARIOS = tuple(IMPLEMENTATIONS)

REQUIRED_CHECKS = {
    "workflow_generation_poll_exhaustion": (
        "poll_exhaustion_backoff_arithmetic",
        "poll_exhaustion_server_error",
        "poll_exhaustion_one_kickoff",
        "poll_exhaustion_four_polls",
        "poll_exhaustion_kickoff_retained",
        "poll_exhaustion_recovery",
        "generation_client_closed",
        "generation_handlers_settled",
        "generation_server_clean",
    ),
    "workflow_generation_shared_original_timeout": (
        "shared_timeout_leader_survives_original",
        "shared_timeout_public_error",
        "shared_timeout_one_kickoff",
        "shared_timeout_one_poll",
        "shared_timeout_follower_retains_task",
        "shared_timeout_registry_settled",
        "shared_timeout_recovery",
        "generation_client_closed",
        "generation_handlers_settled",
        "generation_server_clean",
    ),
}

BUDGETS = {
    "workflow_generation_poll_exhaustion": {
        "scenario_timeout_s": 15.0,
        "cleanup_timeout_s": _CLOSE_TIMEOUT,
        "poll_timeout_s": 12.0,
        "poll_backoff_arithmetic_s": [2.0, 4.0, 8.0],
        "rpc_timeout_s": 1.0,
    },
    "workflow_generation_shared_original_timeout": {
        "scenario_timeout_s": 15.0,
        "cleanup_timeout_s": _CLOSE_TIMEOUT,
        "operation_timeout_s": 2.0,
        "rpc_timeout_s": 4.0,
        "poll_timeout_s": 5.0,
    },
}

__all__ = ["BUDGETS", "IMPLEMENTATIONS", "REQUIRED_CHECKS", "SCENARIOS"]
