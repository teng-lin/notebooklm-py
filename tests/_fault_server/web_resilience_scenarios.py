"""Replay, budget, admission, and shared-flight Web socket scenarios."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from notebooklm import NetworkError, OperationTimeoutError, RateLimitError, ServerError
from notebooklm.client import NotebookLMClient
from notebooklm.outcomes import CommitState
from notebooklm.rpc import RPCMethod

from .common import ScenarioResult
from .http import Disconnect, HttpFaultServer, Reply, Route, Stall
from .web import (
    COOKIE_NAME,
    build_fault_client,
    create_response,
    homepage_response,
    list_response,
    rpc_status_response,
)

_READ = Route.rpc(RPCMethod.LIST_NOTEBOOKS.value)
_CREATE = Route.rpc(RPCMethod.CREATE_NOTEBOOK.value)
_HOME = Route.homepage()
_CLOSE_TIMEOUT = 2.0

SCENARIOS = (
    "auth_refresh_cancelled_waiter",
    "auth_refresh_old_generation",
    "create_disconnect_uncommitted",
    "queue_cancel_no_dispatch",
    "queue_expiry_no_dispatch",
    "read_disconnect_recovery",
    "retry_auth_rate_exhaustion",
    "retry_auth_server_budget",
)

PLANS: dict[str, tuple[str, ...]] = {
    "auth_refresh_cancelled_waiter": (
        "two rpc:decoded-auth@gate",
        "homepage:refresh@gate",
        "one waiter:cancel",
        "survivor:rpc:200",
    ),
    "auth_refresh_old_generation": (
        "old homepage:refresh@gate",
        "client:close+reopen",
        "new refresh:rpc:200",
        "old response:release",
    ),
    "create_disconnect_uncommitted": ("create:request-observed", "socket:disconnect"),
    "queue_cancel_no_dispatch": ("rpc:permit-held", "queued rpc:cancel", "rpc:recovery"),
    "queue_expiry_no_dispatch": (
        "rpc:permit-held",
        "queued create:operation-expiry",
        "rpc:recovery",
    ),
    "read_disconnect_recovery": ("read:request-observed", "socket:disconnect", "read:200"),
    "retry_auth_rate_exhaustion": (
        "rpc:503",
        "rpc:decoded-auth",
        "homepage:refresh",
        "rpc:503",
        "rpc:429x2",
    ),
    "retry_auth_server_budget": (
        "rpc:503",
        "rpc:decoded-auth",
        "homepage:refresh",
        "rpc:503:exhaust",
    ),
}


def _requests(server: HttpFaultServer, route: Route) -> list[Any]:
    return [record for record in server.journal if record.route == route]


def _clean(result: ScenarioResult, server: HttpFaultServer, *, remaining: int = 0) -> None:
    result.require("resilience_server_no_errors", not server.errors)
    result.require("resilience_handlers_settled", server.active_handlers == 0)
    result.require("resilience_expected_remaining", server.remaining() == remaining)


@asynccontextmanager
async def _cohort(
    result: ScenarioResult,
    server: HttpFaultServer,
    *,
    timeout: float = 0.5,
    rate_retries: int = 2,
    server_retries: int = 2,
    max_concurrent_rpcs: int | None = 16,
    operation_timeout: float | None = None,
    real_sleep: bool = False,
) -> AsyncIterator[NotebookLMClient]:
    await server.__aenter__()
    client: NotebookLMClient | None = None

    async def no_sleep(seconds: float) -> None:
        result.record("sleep", seconds=seconds)

    try:
        client = build_fault_client(
            server,
            timeout=timeout,
            rate_limit_max_retries=rate_retries,
            server_error_max_retries=server_retries,
            max_concurrent_rpcs=max_concurrent_rpcs,
            operation_timeout=operation_timeout,
            sleep=None if real_sleep else no_sleep,
        )
        await client.__aenter__()
        yield client
    finally:
        if client is not None:
            await asyncio.wait_for(client.close(drain=False), _CLOSE_TIMEOUT)
        await asyncio.wait_for(server.aclose(), _CLOSE_TIMEOUT)
        result.record(
            "resilience_cleanup",
            client_closed=client is None or not client._lifecycle.is_open(),
            requests=len(server.journal),
            remaining_actions=server.remaining(),
            server_errors=list(server.errors),
        )
        result.record(
            "http_trace",
            requests=[
                {
                    "sequence": record.sequence,
                    "method": record.route.method,
                    "logical_host": record.route.host,
                    "path": record.route.path,
                    "rpc_id": record.route.rpc_id,
                    "action": record.action,
                }
                for record in server.journal
            ],
            committed=list(server.committed),
        )
        result.require(
            "resilience_client_closed", client is None or not client._lifecycle.is_open()
        )


async def _read_disconnect_recovery(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _READ,
        Disconnect(),
        Reply(body=list_response(_READ.rpc_id or "", [("nb-retry", "Recovered")])),
        Reply(body=list_response(_READ.rpc_id or "", [("nb-probe", "Probe")])),
    )
    async with _cohort(result, server, timeout=10.0, server_retries=1) as client:
        rows = await client.notebooks.list()
        probe = await client.notebooks.list()
    result.require("read_loss_replayed_once", len(_requests(server, _READ)) == 3)
    result.require("read_loss_result", [row.id for row in rows] == ["nb-retry"])
    result.require("read_loss_recovery", [row.id for row in probe] == ["nb-probe"])
    result.require("read_loss_no_commit", server.committed == [])
    _clean(result, server)


async def _create_disconnect_uncommitted(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _CREATE,
        Disconnect(),
        Reply(body=create_response(_CREATE.rpc_id or "", "must-not-run", "Duplicate")),
    )
    server.enqueue(_READ, Reply(body=list_response(_READ.rpc_id or "", [])))
    error: BaseException | None = None
    async with _cohort(result, server, server_retries=2) as client:
        try:
            await client.notebooks.create("Unresolved")
        except BaseException as caught:
            error = caught
        probe = await client.notebooks.list()
    result.require("uncommitted_create_network_error", isinstance(error, NetworkError))
    result.require(
        "uncommitted_create_unknown", getattr(error, "commit_state", None) is CommitState.UNKNOWN
    )
    result.require("uncommitted_create_unconfirmed", bool(getattr(error, "unconfirmed", False)))
    result.require("uncommitted_create_sent_once", len(_requests(server, _CREATE)) == 1)
    result.require("uncommitted_create_no_service_commit", server.committed == [])
    result.require("uncommitted_create_recovery", probe == [])
    _clean(result, server, remaining=1)


async def _queue_expiry_no_dispatch(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    holder_reply = Reply(body=list_response(_READ.rpc_id or "", [("held", "Held")]))
    server.enqueue(
        _READ,
        Stall("headers", "held-rpc", holder_reply),
        Reply(body=list_response(_READ.rpc_id or "", [("probe", "Probe")])),
    )
    server.enqueue(
        _CREATE,
        Reply(body=create_response(_CREATE.rpc_id or "", "must-not-run", "Queued")),
    )
    error: BaseException | None = None
    async with _cohort(result, server, max_concurrent_rpcs=1) as client:
        held = asyncio.create_task(client.notebooks.list())
        await server.wait_for_requests(_READ, 1)
        try:
            async with client.operation(timeout=0.05):
                await client.notebooks.create("Queued")
        except BaseException as caught:
            error = caught
        server.release("held-rpc")
        await held
        probe = await client.notebooks.list()
    metadata = getattr(error, "operation_metadata", None)
    result.require("queued_create_operation_timeout", isinstance(error, OperationTimeoutError))
    result.require(
        "queued_create_not_sent",
        metadata is not None
        and metadata.commit_state is CommitState.NOT_SENT
        and not metadata.attempts,
    )
    result.require("queued_create_zero_dispatch", len(_requests(server, _CREATE)) == 0)
    result.require("queued_create_recovery", [row.id for row in probe] == ["probe"])
    _clean(result, server, remaining=1)


async def _queue_cancel_no_dispatch(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    reply = Reply(body=list_response(_READ.rpc_id or "", [("ok", "OK")]))
    server.enqueue(_READ, Stall("headers", "held-rpc", reply), reply)
    async with _cohort(result, server, max_concurrent_rpcs=1) as client:
        held = asyncio.create_task(client.notebooks.list())
        await server.wait_for_requests(_READ, 1)
        queued = asyncio.create_task(client.notebooks.list())
        await asyncio.sleep(0)
        queued.cancel()
        cancelled = await asyncio.gather(queued, return_exceptions=True)
        server.release("held-rpc")
        await held
        probe = await client.notebooks.list()
    result.require("queued_read_cancelled", isinstance(cancelled[0], asyncio.CancelledError))
    result.require("queued_read_zero_dispatch", len(_requests(server, _READ)) == 2)
    result.require("queued_read_recovery", [row.id for row in probe] == ["ok"])
    _clean(result, server)


async def _retry_auth_rate_exhaustion(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _READ,
        Reply(503),
        Reply(body=rpc_status_response(_READ.rpc_id or "", 16)),
        Reply(503),
        Reply(429),
        Reply(429),
        Reply(body=list_response(_READ.rpc_id or "", [("probe", "Probe")])),
    )
    server.enqueue(_HOME, Reply(body=homepage_response()))
    error: BaseException | None = None
    async with _cohort(
        result, server, timeout=10.0, server_retries=2, rate_retries=1
    ) as client:
        try:
            await client.notebooks.list()
        except BaseException as caught:
            error = caught
        probe = await client.notebooks.list()
    result.require("composed_rate_error", isinstance(error, RateLimitError))
    result.require("composed_rate_rpc_count", len(_requests(server, _READ)) == 6)
    result.require("composed_rate_one_refresh", len(_requests(server, _HOME)) == 1)
    result.require("composed_rate_recovery", [row.id for row in probe] == ["probe"])
    _clean(result, server)


async def _retry_auth_server_budget(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _READ,
        Reply(503),
        Reply(body=rpc_status_response(_READ.rpc_id or "", 16)),
        Reply(503),
        Reply(body=list_response(_READ.rpc_id or "", [("must-not-run", "Sentinel")])),
        Reply(body=list_response(_READ.rpc_id or "", [("probe", "Probe")])),
    )
    server.enqueue(_HOME, Reply(body=homepage_response()))
    error: BaseException | None = None
    async with _cohort(result, server, timeout=10.0, server_retries=1) as client:
        try:
            await client.notebooks.list()
        except BaseException as caught:
            error = caught
        attempts_before_recovery = len(_requests(server, _READ))
        probe = await client.notebooks.list()
    result.require("shared_server_budget_error", isinstance(error, ServerError))
    result.require("shared_server_budget_three_attempts", attempts_before_recovery == 3)
    result.require("shared_server_budget_one_refresh", len(_requests(server, _HOME)) == 1)
    result.require(
        "shared_server_budget_recovery", [row.id for row in probe] == ["must-not-run"]
    )
    _clean(result, server, remaining=1)


async def _auth_refresh_cancelled_waiter(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    auth = Reply(body=rpc_status_response(_READ.rpc_id or "", 16))
    fresh = Reply(body=list_response(_READ.rpc_id or "", [("shared", "Shared")]))
    server.enqueue(
        _READ,
        Stall("headers", "stale-pair", auth),
        Stall("headers", "stale-pair", auth),
        fresh,
        Reply(body=list_response(_READ.rpc_id or "", [("probe", "Probe")])),
    )
    server.enqueue(_HOME, Stall("headers", "refresh", Reply(body=homepage_response())))
    async with _cohort(result, server) as client:
        first = asyncio.create_task(client.notebooks.list())
        second = asyncio.create_task(client.notebooks.list())
        await server.wait_for_requests(_READ, 2)
        server.release("stale-pair")
        await server.wait_for_requests(_HOME, 1)
        first.cancel()
        first_outcome = await asyncio.gather(first, return_exceptions=True)
        server.release("refresh")
        survivor = await second
        probe = await client.notebooks.list()
    result.require(
        "refresh_cancelled_waiter_escapes",
        isinstance(first_outcome[0], asyncio.CancelledError),
    )
    result.require("refresh_survivor_succeeds", [row.id for row in survivor] == ["shared"])
    result.require("refresh_cancel_one_homepage", len(_requests(server, _HOME)) == 1)
    result.require("refresh_cancel_rpc_count", len(_requests(server, _READ)) == 4)
    result.require("refresh_cancel_recovery", [row.id for row in probe] == ["probe"])
    _clean(result, server)


async def _auth_refresh_old_generation(result: ScenarioResult) -> None:
    newest_csrf = "csrf-generation-3"
    newest_session = "session-generation-3"
    newest_cookie = "cookie-generation-3"
    server = HttpFaultServer()
    auth = Reply(body=rpc_status_response(_READ.rpc_id or "", 16))
    server.enqueue(
        _READ,
        auth,
        auth,
        Reply(body=list_response(_READ.rpc_id or "", [("new", "New generation")])),
        Reply(body=list_response(_READ.rpc_id or "", [("probe", "Probe")])),
    )
    server.enqueue(
        _HOME,
        Stall("headers", "old-refresh", Reply(body=homepage_response())),
        Reply(
            body=homepage_response(csrf=newest_csrf, session=newest_session),
            headers={
                "Set-Cookie": (f"{COOKIE_NAME}={newest_cookie}; Domain=.google.com; Path=/; Secure")
            },
        ),
    )
    await server.__aenter__()
    client = build_fault_client(server)
    old_error: BaseException | None = None
    try:
        await client.__aenter__()
        old = asyncio.create_task(client.notebooks.list())
        await server.wait_for_requests(_HOME, 1)
        await client.close(drain=False)
        old_outcome = await asyncio.gather(old, return_exceptions=True)
        old_error = old_outcome[0] if isinstance(old_outcome[0], BaseException) else None
        await client.__aenter__()
        current = await client.notebooks.list()
        server.release("old-refresh")
        await server.wait_for_event("handler_settled", count=4)
        probe = await client.notebooks.list()
        auth_state = client.auth
    finally:
        server.release("old-refresh")
        await asyncio.wait_for(client.close(drain=False), _CLOSE_TIMEOUT)
        await asyncio.wait_for(server.aclose(), _CLOSE_TIMEOUT)
    result.record(
        "http_trace",
        requests=[
            {
                "sequence": record.sequence,
                "method": record.route.method,
                "logical_host": record.route.host,
                "path": record.route.path,
                "rpc_id": record.route.rpc_id,
                "action": record.action,
            }
            for record in server.journal
        ],
        committed=list(server.committed),
    )
    result.require("old_refresh_waiter_failed", old_error is not None)
    result.require("new_generation_succeeded", [row.id for row in current] == ["new"])
    result.require("old_refresh_did_not_publish", auth_state.csrf_token == newest_csrf)
    result.require(
        "new_cookie_retained",
        auth_state.cookies.get((COOKIE_NAME, ".google.com", "/")) == newest_cookie,
    )
    result.require("old_refresh_two_homepages", len(_requests(server, _HOME)) == 2)
    result.require("old_refresh_recovery", [row.id for row in probe] == ["probe"])
    _clean(result, server)


IMPLEMENTATIONS: dict[str, Callable[[ScenarioResult], Awaitable[None]]] = {
    "auth_refresh_cancelled_waiter": _auth_refresh_cancelled_waiter,
    "auth_refresh_old_generation": _auth_refresh_old_generation,
    "create_disconnect_uncommitted": _create_disconnect_uncommitted,
    "queue_cancel_no_dispatch": _queue_cancel_no_dispatch,
    "queue_expiry_no_dispatch": _queue_expiry_no_dispatch,
    "read_disconnect_recovery": _read_disconnect_recovery,
    "retry_auth_rate_exhaustion": _retry_auth_rate_exhaustion,
    "retry_auth_server_budget": _retry_auth_server_budget,
}


__all__ = ["IMPLEMENTATIONS", "PLANS", "SCENARIOS"]
