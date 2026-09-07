"""Repeated real pool and library-admission saturation on a single Web client."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from notebooklm import OperationTimeoutError
from notebooklm.rpc import RPCMethod

from .common import ScenarioResult
from .http import HttpFaultServer, Reply, Route, Stall
from .web import list_response
from .web_transfers import READ

SCENARIOS = ("saturation_transport_pool", "saturation_library_permit")
_CREATE = Route.rpc(RPCMethod.CREATE_NOTEBOOK.value)
_CYCLES = 3
_POOL_TIMEOUT = 0.4
_QUEUE_TIMEOUT = 0.8
_WATCHDOG = 6.0


async def _until(predicate: Any) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 2)


def _pool_timeout(error: BaseException | None) -> bool:
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, httpx.PoolTimeout):
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


async def run_scenario(
    name: str, *, operation_id: str, result: ScenarioResult | None = None
) -> ScenarioResult:
    from .web_scenarios import _cohort, _require_clean

    if name not in SCENARIOS:
        raise ValueError("unknown saturation scenario")
    if result is None:
        result = ScenarioResult("web", name, operation_id)
    elif (result.backend, result.scenario, result.operation_id) != ("web", name, operation_id):
        raise ValueError("scenario identity mismatch")
    pool_case = name == SCENARIOS[0]
    checks = [
        "owned_cleanup_completed",
        "cancelled_queued_no_dispatch",
        "exact_dispatch_count",
        "no_commits",
        "client_closed",
        "server_required_gates_observed",
        "server_plan_consumed",
        "server_had_no_errors",
        "server_handlers_drained",
    ]
    per_cycle = (
        "active_observed",
        "queue_observed",
        "cancelled_queue_settled",
        "deadline_distinct",
        "survivor_progress",
        "recovery",
        "owned_resources_baseline",
    )
    checks += [f"cycle_{cycle}_{check}" for cycle in range(_CYCLES) for check in per_cycle]
    result.record(
        "plan",
        faults=[name, "queued_cancel", "active_cancel", "repeat_three_cycles"],
        cohort_ids=[f"{operation_id}:0"],
        transport="httpx",
        required_checks=checks,
        budgets={
            "pool_timeout_s": _POOL_TIMEOUT,
            "queue_operation_timeout_s": _QUEUE_TIMEOUT,
            "watchdog_s": _WATCHDOG,
            "cleanup_timeout_s": 2,
            "task_cleanup_timeout_s": 2.5,
            "request_limit": _CYCLES * 3,
            "commit_limit": 0,
        },
    )
    server = HttpFaultServer()
    good = Reply(body=list_response(READ.rpc_id or "", [("healthy", "Healthy")]))
    for cycle in range(_CYCLES):
        server.enqueue(READ, Stall("headers", f"active-{cycle}", good), good, good)
    clients: list[httpx.AsyncClient] = []
    original_factory = server.client_factory

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["limits"] = httpx.Limits(
            max_connections=1 if pool_case else 8, max_keepalive_connections=0
        )
        kwargs["timeout"] = httpx.Timeout(4, pool=_POOL_TIMEOUT)
        client = original_factory(**kwargs)
        clients.append(client)
        return client

    server.client_factory = factory
    owned: list[asyncio.Task[Any]] = []
    async with _cohort(
        result,
        server,
        timeout=4,
        server_retries=0,
        rate_retries=0,
        record_sleep=False,
        max_concurrent_rpcs=16 if pool_case else 1,
    ) as client:
        transport_pool = clients[0]._transport._inner._pool
        supervisor = client._collaborators.call_supervisor
        generation = supervisor._current
        assert generation is not None
        primary_error = None
        try:
            for cycle in range(_CYCLES):

                def require(label: str, condition: bool, cycle: int = cycle) -> None:
                    result.require(f"cycle_{cycle}_{label}", condition)

                active = asyncio.create_task(client.notebooks.list())
                owned.append(active)
                await server.wait_for_gate(f"active-{cycle}", timeout=3)
                require(
                    "active_observed", not active.done() and len(server.journal) == cycle * 3 + 1
                )
                cancelled = asyncio.create_task(client.notebooks.create("must not dispatch"))
                owned.append(cancelled)

                def queued() -> bool:
                    if pool_case:
                        return len(transport_pool._requests) == 2
                    semaphore = generation.semaphore
                    return semaphore is not None and bool(semaphore._waiters)

                await _until(queued)
                require("queue_observed", not cancelled.done())
                cancelled.cancel()
                outcome = (await asyncio.gather(cancelled, return_exceptions=True))[0]
                require("cancelled_queue_settled", isinstance(outcome, asyncio.CancelledError))

                async def expiring() -> Any:
                    async with client.operation(timeout=_QUEUE_TIMEOUT if not pool_case else 3):
                        return await client.notebooks.list()

                started = time.monotonic()
                error = None
                try:
                    await asyncio.wait_for(expiring(), _WATCHDOG)
                except Exception as exc:
                    error = exc
                elapsed = time.monotonic() - started
                require(
                    "deadline_distinct",
                    (
                        _pool_timeout(error)
                        if pool_case
                        else isinstance(error, OperationTimeoutError)
                    )
                    and (_POOL_TIMEOUT if pool_case else _QUEUE_TIMEOUT) * 0.8 <= elapsed < 3,
                )
                result.record(
                    "saturation_deadline",
                    cycle=cycle,
                    elapsed_s=elapsed,
                    error_type=None if error is None else type(error).__name__,
                    queue_owner="transport" if pool_case else "library",
                )
                survivor = asyncio.create_task(client.notebooks.list())
                owned.append(survivor)
                await _until(queued)
                active.cancel()
                cancelled_active = (await asyncio.gather(active, return_exceptions=True))[0]
                server.release(f"active-{cycle}")
                recovered = await asyncio.wait_for(survivor, _WATCHDOG)
                require(
                    "survivor_progress",
                    isinstance(cancelled_active, asyncio.CancelledError)
                    and [item.id for item in recovered] == ["healthy"],
                )
                probe = await asyncio.wait_for(client.notebooks.list(), _WATCHDOG)
                require("recovery", [item.id for item in probe] == ["healthy"])
                await _until(lambda: not server.active_handlers)
                semaphore = generation.semaphore
                require(
                    "owned_resources_baseline",
                    not transport_pool.connections
                    and not transport_pool._requests
                    and generation.in_flight == 0
                    and not generation.depths
                    and not supervisor._settlement_tasks
                    and semaphore is not None
                    and semaphore._value == (16 if pool_case else 1)
                    and not semaphore._waiters
                    and all(task.done() for task in owned)
                    and not server._writers,
                )
                result.record(
                    "resource_baseline",
                    cycle=cycle,
                    connections=0,
                    pool_requests=0,
                    active_calls=0,
                    owned_tasks_pending=0,
                    server_writers=0,
                )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            # Release peers before settlement so cancellation cannot strand a gate.
            for cycle in range(_CYCLES):
                server.release(f"active-{cycle}")
            for task in owned:
                if not task.done():
                    task.cancel()
            cleanup_error = None
            pending = {task for task in owned if not task.done()}
            try:
                if pending:
                    _, pending = await asyncio.wait(pending, timeout=2)
                if pending:
                    for task in pending:
                        task.cancel()
                    _, pending = await asyncio.wait(pending, timeout=0.5)
                if pending:
                    raise TimeoutError("saturation task cleanup did not settle")
            except BaseException as error:
                cleanup_error = error
            finally:
                # Retrieve finished errors without awaiting cancellation-resistant tasks.
                for task in owned:
                    if task.done() and not task.cancelled():
                        task.exception()
                result.record(
                    "saturation_cleanup",
                    pending_tasks=sum(not t.done() for t in owned),
                    error_type=None if cleanup_error is None else type(cleanup_error).__name__,
                    primary_error=None if primary_error is None else type(primary_error).__name__,
                )
            if primary_error is None:
                if cleanup_error is not None:
                    raise cleanup_error
                result.require("owned_cleanup_completed", all(task.done() for task in owned))
        result.require(
            "cancelled_queued_no_dispatch", all(r.route != _CREATE for r in server.journal)
        )
        result.require("exact_dispatch_count", len(server.journal) == _CYCLES * 3)
        result.require("no_commits", not server.committed)
    _require_clean(result, server)
    return result
