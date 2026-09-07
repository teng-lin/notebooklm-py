"""Replay, budget, admission, and shared-flight Web socket scenarios."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx

from notebooklm import NetworkError, OperationTimeoutError, RateLimitError, ServerError
from notebooklm.client import NotebookLMClient
from notebooklm.outcomes import CommitState
from notebooklm.rpc import RPCMethod

from .common import ScenarioResult
from .http import Disconnect, HttpFaultServer, LogicalHostTransport, Reply, Route, Stall
from .web import (
    COOKIE_NAME,
    build_fault_client,
    create_response,
    homepage_response,
    list_response,
    rpc_response,
    rpc_status_response,
)

_READ = Route.rpc(RPCMethod.LIST_NOTEBOOKS.value)
_CREATE = Route.rpc(RPCMethod.CREATE_NOTEBOOK.value)
_RENAME = Route.rpc(RPCMethod.RENAME_NOTEBOOK.value)
_GET = Route.rpc(RPCMethod.GET_NOTEBOOK.value)
_HOME = Route.homepage()
_CLOSE_TIMEOUT = 2.0

SCENARIOS = (
    "auth_refresh_cancelled_waiter",
    "auth_refresh_old_generation",
    "connection_refusal_recovery",
    "configured_queue_expiry_no_dispatch",
    "create_disconnect_uncommitted",
    "queue_cancel_no_dispatch",
    "queue_expiry_no_dispatch",
    "read_disconnect_recovery",
    "rename_commit_loss_converges",
    "retry_auth_backoff_cancelled",
    "retry_auth_operation_deadline",
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
    "connection_refusal_recovery": (
        "loopback:connection-refused x3",
        "same transport:retarget",
        "read:recovery",
    ),
    "configured_queue_expiry_no_dispatch": (
        "rpc:permit-held",
        "queued create:configured-expiry",
        "rpc:recovery",
    ),
    "create_disconnect_uncommitted": ("create:request-observed", "socket:disconnect"),
    "queue_cancel_no_dispatch": ("rpc:permit-held", "queued rpc:cancel", "rpc:recovery"),
    "queue_expiry_no_dispatch": (
        "rpc:permit-held",
        "queued create:operation-expiry",
        "rpc:recovery",
    ),
    "read_disconnect_recovery": ("read:request-observed", "socket:disconnect", "read:200"),
    "rename_commit_loss_converges": (
        "rename:commit+disconnect",
        "rename:repeat same set",
        "get:final state",
    ),
    "retry_auth_backoff_cancelled": (
        "rpc:503+decoded-auth+429",
        "retry sleep:gate",
        "caller:cancel",
        "read:recovery",
    ),
    "retry_auth_operation_deadline": (
        "rpc:503+decoded-auth+429",
        "retry sleep:gate",
        "configured operation:expire",
        "read:recovery",
    ),
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


def _record_http_trace(result: ScenarioResult, server: HttpFaultServer) -> None:
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


async def _settle_cohort(
    result: ScenarioResult,
    server: HttpFaultServer,
    client: NotebookLMClient | None,
    primary_error: BaseException | None,
) -> list[BaseException]:
    cleanup_errors: list[BaseException] = []
    if client is not None:
        try:
            await asyncio.wait_for(client.close(drain=False), _CLOSE_TIMEOUT)
        except BaseException as exc:
            cleanup_errors.append(exc)
    try:
        await asyncio.wait_for(server.aclose(), _CLOSE_TIMEOUT)
    except BaseException as exc:
        cleanup_errors.append(exc)
    result.record(
        "cleanup",
        component="resilience",
        client_closed=client is None or not client._lifecycle.is_open(),
        requests=len(server.journal),
        remaining_actions=server.remaining(),
        server_errors=list(server.errors),
        primary_error=None if primary_error is None else type(primary_error).__name__,
        cleanup_error_types=[type(error).__name__ for error in cleanup_errors],
    )
    _record_http_trace(result, server)
    return cleanup_errors


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
    retry_sleep: Callable[[float], Awaitable[Any]] | None = None,
    async_client_factory: Callable[..., Any] | None = None,
) -> AsyncIterator[NotebookLMClient]:
    client: NotebookLMClient | None = None
    primary_error: BaseException | None = None

    async def no_sleep(seconds: float) -> None:
        result.record("sleep", seconds=seconds)

    try:
        await server.__aenter__()
        client = build_fault_client(
            server,
            timeout=timeout,
            rate_limit_max_retries=rate_retries,
            server_error_max_retries=server_retries,
            max_concurrent_rpcs=max_concurrent_rpcs,
            operation_timeout=operation_timeout,
            sleep=retry_sleep or no_sleep,
            async_client_factory=async_client_factory,
        )
        await client.__aenter__()
        yield client
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors = await _settle_cohort(result, server, client, primary_error)
        if primary_error is None:
            if cleanup_errors:
                raise cleanup_errors[0]
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


async def _connection_refusal_recovery(result: ScenarioResult) -> None:
    reserved = socket.socket()
    reserved.bind(("127.0.0.1", 0))
    refused_target = ("127.0.0.1", int(reserved.getsockname()[1]))
    reserved.close()
    transports: list[LogicalHostTransport] = []

    def client_factory(**kwargs: Any) -> httpx.AsyncClient:
        if "transport" in kwargs:
            raise TypeError("connection-refusal factory owns the transport")
        transport = LogicalHostTransport(
            {
                "notebook.google.com": refused_target,
                "accounts.google.com": refused_target,
            }
        )
        transports.append(transport)
        return httpx.AsyncClient(transport=transport, trust_env=False, **kwargs)

    server = HttpFaultServer()
    server.enqueue(
        _READ,
        Reply(body=list_response(_READ.rpc_id or "", [("probe", "Probe")])),
    )
    error: BaseException | None = None
    async with _cohort(
        result,
        server,
        timeout=10.0,
        server_retries=2,
        async_client_factory=client_factory,
    ) as client:
        try:
            await client.notebooks.list()
        except BaseException as caught:
            error = caught
        for transport in transports:
            transport.retarget("notebook.google.com", server.address)
            transport.retarget("accounts.google.com", server.address)
        probe = await client.notebooks.list()
        metrics = client.metrics_snapshot()
    result.require("refusal_network_error", isinstance(error, NetworkError))
    result.require("refusal_no_server_dispatch", len(_requests(server, _READ)) == 1)
    result.require("refusal_three_attempts", metrics.rpc_server_error_retries == 2)
    result.require("refusal_same_client_recovery", [row.id for row in probe] == ["probe"])
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


async def _rename_commit_loss_converges(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _RENAME,
        Disconnect(commit_id="rename:notebook-1:Stable"),
        Reply(body=rpc_response(_RENAME.rpc_id or "", None)),
    )
    server.enqueue(
        _GET,
        Reply(
            body=rpc_response(
                _GET.rpc_id or "",
                [["Stable", [], "notebook-1", "📘", None, [None, None, None, None]]],
            )
        ),
        Reply(
            body=rpc_response(
                _GET.rpc_id or "",
                [["Stable", [], "notebook-1", "📘", None, [None, None, None, None]]],
            )
        ),
    )
    async with _cohort(result, server, timeout=10.0, server_retries=1) as client:
        renamed = await client.notebooks.rename("notebook-1", "Stable")
        probe = await client.notebooks.get("notebook-1")
    attempts = _requests(server, _RENAME)
    result.require("rename_replayed_once", len(attempts) == 2)
    result.require(
        "rename_replayed_identical_set",
        len(attempts) == 2 and attempts[0].body_digest == attempts[1].body_digest,
    )
    result.require("rename_first_commit_recorded", server.committed == ["rename:notebook-1:Stable"])
    result.require("rename_result_converged", renamed.title == "Stable")
    result.require("rename_recovery", probe.title == "Stable")
    _clean(result, server)


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


async def _configured_queue_expiry_no_dispatch(result: ScenarioResult) -> None:
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
    async with _cohort(
        result,
        server,
        max_concurrent_rpcs=1,
        operation_timeout=0.05,
    ) as client:

        async def hold_without_default_deadline() -> list[Any]:
            async with client.operation(timeout=None):
                return await client.notebooks.list()

        held = asyncio.create_task(hold_without_default_deadline())
        await server.wait_for_requests(_READ, 1)
        try:
            await client.notebooks.create("Queued")
        except BaseException as caught:
            error = caught
        server.release("held-rpc")
        await held
        probe = await client.notebooks.list()
    metadata = getattr(error, "operation_metadata", None)
    result.require("configured_queue_timeout", isinstance(error, OperationTimeoutError))
    result.require(
        "configured_queue_not_sent",
        metadata is not None
        and metadata.commit_state is CommitState.NOT_SENT
        and not metadata.attempts,
    )
    result.require("configured_queue_zero_dispatch", len(_requests(server, _CREATE)) == 0)
    result.require("configured_queue_recovery", [row.id for row in probe] == ["probe"])
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
    async with _cohort(result, server, timeout=10.0, server_retries=2, rate_retries=1) as client:
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


async def _retry_auth_operation_deadline(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _READ,
        Reply(503),
        Reply(body=rpc_status_response(_READ.rpc_id or "", 16)),
        Reply(429, headers={"Retry-After": "5"}),
        Reply(body=list_response(_READ.rpc_id or "", [("probe", "Probe")])),
    )
    server.enqueue(_HOME, Reply(body=homepage_response()))
    backoff_started = asyncio.Event()
    release_backoff = asyncio.Event()

    async def gated_sleep(seconds: float) -> None:
        result.record("sleep", seconds=seconds)
        if seconds >= 4.0:
            backoff_started.set()
            await release_backoff.wait()

    error: BaseException | None = None
    async with _cohort(
        result,
        server,
        timeout=10.0,
        operation_timeout=0.2,
        server_retries=3,
        rate_retries=3,
        retry_sleep=gated_sleep,
    ) as client:
        task = asyncio.create_task(client.notebooks.list())
        await asyncio.wait_for(backoff_started.wait(), 1.0)
        try:
            await task
        except BaseException as caught:
            error = caught
        attempts_before_recovery = len(_requests(server, _READ))
        release_backoff.set()
        await asyncio.sleep(0)
        probe = await client.notebooks.list()
    result.require("aggregate_deadline_timeout", isinstance(error, OperationTimeoutError))
    result.require("aggregate_deadline_three_attempts", attempts_before_recovery == 3)
    result.require("aggregate_deadline_one_refresh", len(_requests(server, _HOME)) == 1)
    result.require("aggregate_deadline_recovery", [row.id for row in probe] == ["probe"])
    _clean(result, server)


async def _retry_auth_backoff_cancelled(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _READ,
        Reply(503),
        Reply(body=rpc_status_response(_READ.rpc_id or "", 16)),
        Reply(429, headers={"Retry-After": "5"}),
        Reply(body=list_response(_READ.rpc_id or "", [("probe", "Probe")])),
    )
    server.enqueue(_HOME, Reply(body=homepage_response()))
    backoff_started = asyncio.Event()
    release_backoff = asyncio.Event()

    async def gated_sleep(seconds: float) -> None:
        result.record("sleep", seconds=seconds)
        if seconds >= 4.0:
            backoff_started.set()
            await release_backoff.wait()

    async with _cohort(
        result,
        server,
        timeout=10.0,
        server_retries=3,
        rate_retries=3,
        retry_sleep=gated_sleep,
    ) as client:
        task = asyncio.create_task(client.notebooks.list())
        await asyncio.wait_for(backoff_started.wait(), 1.0)
        task.cancel()
        cancelled = await asyncio.gather(task, return_exceptions=True)
        attempts_before_recovery = len(_requests(server, _READ))
        release_backoff.set()
        await asyncio.sleep(0)
        probe = await client.notebooks.list()
    result.require(
        "backoff_cancelled_publicly",
        len(cancelled) == 1 and isinstance(cancelled[0], asyncio.CancelledError),
    )
    result.require("backoff_cancelled_three_attempts", attempts_before_recovery == 3)
    result.require("backoff_cancelled_one_refresh", len(_requests(server, _HOME)) == 1)
    result.require("backoff_cancelled_recovery", [row.id for row in probe] == ["probe"])
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
    result.require("shared_server_budget_recovery", [row.id for row in probe] == ["must-not-run"])
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
    client: NotebookLMClient | None = None
    old_error: BaseException | None = None
    primary_error: BaseException | None = None
    try:
        await server.__aenter__()
        client = build_fault_client(server)
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
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        server.release("old-refresh")
        cleanup_errors = await _settle_cohort(result, server, client, primary_error)
        if primary_error is None and cleanup_errors:
            raise cleanup_errors[0]
    assert client is not None
    result.require("old_refresh_waiter_failed", old_error is not None)
    result.require("new_generation_succeeded", [row.id for row in current] == ["new"])
    result.require("old_refresh_did_not_publish", auth_state.csrf_token == newest_csrf)
    result.require(
        "new_cookie_retained",
        auth_state.cookies.get((COOKIE_NAME, ".google.com", "/")) == newest_cookie,
    )
    result.require("old_refresh_two_homepages", len(_requests(server, _HOME)) == 2)
    result.require("old_refresh_recovery", [row.id for row in probe] == ["probe"])
    result.require("resilience_client_closed", not client._lifecycle.is_open())
    _clean(result, server)


IMPLEMENTATIONS: dict[str, Callable[[ScenarioResult], Awaitable[None]]] = {
    "auth_refresh_cancelled_waiter": _auth_refresh_cancelled_waiter,
    "auth_refresh_old_generation": _auth_refresh_old_generation,
    "connection_refusal_recovery": _connection_refusal_recovery,
    "configured_queue_expiry_no_dispatch": _configured_queue_expiry_no_dispatch,
    "create_disconnect_uncommitted": _create_disconnect_uncommitted,
    "queue_cancel_no_dispatch": _queue_cancel_no_dispatch,
    "queue_expiry_no_dispatch": _queue_expiry_no_dispatch,
    "read_disconnect_recovery": _read_disconnect_recovery,
    "rename_commit_loss_converges": _rename_commit_loss_converges,
    "retry_auth_backoff_cancelled": _retry_auth_backoff_cancelled,
    "retry_auth_operation_deadline": _retry_auth_operation_deadline,
    "retry_auth_rate_exhaustion": _retry_auth_rate_exhaustion,
    "retry_auth_server_budget": _retry_auth_server_budget,
}


_CLEAN_CHECKS = (
    "resilience_client_closed",
    "resilience_server_no_errors",
    "resilience_handlers_settled",
    "resilience_expected_remaining",
)

REQUIRED_CHECKS: dict[str, tuple[str, ...]] = {
    "auth_refresh_cancelled_waiter": (
        "refresh_cancelled_waiter_escapes",
        "refresh_survivor_succeeds",
        "refresh_cancel_one_homepage",
        "refresh_cancel_rpc_count",
        "refresh_cancel_recovery",
        *_CLEAN_CHECKS,
    ),
    "auth_refresh_old_generation": (
        "old_refresh_waiter_failed",
        "new_generation_succeeded",
        "old_refresh_did_not_publish",
        "new_cookie_retained",
        "old_refresh_two_homepages",
        "old_refresh_recovery",
        *_CLEAN_CHECKS,
    ),
    "connection_refusal_recovery": (
        "refusal_network_error",
        "refusal_no_server_dispatch",
        "refusal_three_attempts",
        "refusal_same_client_recovery",
        *_CLEAN_CHECKS,
    ),
    "configured_queue_expiry_no_dispatch": (
        "configured_queue_timeout",
        "configured_queue_not_sent",
        "configured_queue_zero_dispatch",
        "configured_queue_recovery",
        *_CLEAN_CHECKS,
    ),
    "create_disconnect_uncommitted": (
        "uncommitted_create_network_error",
        "uncommitted_create_unknown",
        "uncommitted_create_unconfirmed",
        "uncommitted_create_sent_once",
        "uncommitted_create_no_service_commit",
        "uncommitted_create_recovery",
        *_CLEAN_CHECKS,
    ),
    "queue_cancel_no_dispatch": (
        "queued_read_cancelled",
        "queued_read_zero_dispatch",
        "queued_read_recovery",
        *_CLEAN_CHECKS,
    ),
    "queue_expiry_no_dispatch": (
        "queued_create_operation_timeout",
        "queued_create_not_sent",
        "queued_create_zero_dispatch",
        "queued_create_recovery",
        *_CLEAN_CHECKS,
    ),
    "read_disconnect_recovery": (
        "read_loss_replayed_once",
        "read_loss_result",
        "read_loss_recovery",
        "read_loss_no_commit",
        *_CLEAN_CHECKS,
    ),
    "rename_commit_loss_converges": (
        "rename_replayed_once",
        "rename_replayed_identical_set",
        "rename_first_commit_recorded",
        "rename_result_converged",
        "rename_recovery",
        *_CLEAN_CHECKS,
    ),
    "retry_auth_backoff_cancelled": (
        "backoff_cancelled_publicly",
        "backoff_cancelled_three_attempts",
        "backoff_cancelled_one_refresh",
        "backoff_cancelled_recovery",
        *_CLEAN_CHECKS,
    ),
    "retry_auth_operation_deadline": (
        "aggregate_deadline_timeout",
        "aggregate_deadline_three_attempts",
        "aggregate_deadline_one_refresh",
        "aggregate_deadline_recovery",
        *_CLEAN_CHECKS,
    ),
    "retry_auth_rate_exhaustion": (
        "composed_rate_error",
        "composed_rate_rpc_count",
        "composed_rate_one_refresh",
        "composed_rate_recovery",
        *_CLEAN_CHECKS,
    ),
    "retry_auth_server_budget": (
        "shared_server_budget_error",
        "shared_server_budget_three_attempts",
        "shared_server_budget_one_refresh",
        "shared_server_budget_recovery",
        *_CLEAN_CHECKS,
    ),
}

_DEFAULT_BUDGET: dict[str, float | int | str] = {
    "scenario_timeout_s": 12.0,
    "rpc_timeout_s": 0.5,
    "rate_limit_max_retries": 2,
    "server_error_max_retries": 2,
    "cleanup_timeout_s": _CLOSE_TIMEOUT,
    "retry_clock": "record_only",
}
BUDGETS: dict[str, dict[str, float | int | str]] = {
    name: dict(_DEFAULT_BUDGET) for name in SCENARIOS
}
BUDGETS["connection_refusal_recovery"].update(rpc_timeout_s=10.0)
BUDGETS["auth_refresh_old_generation"].update(retry_clock="real")
BUDGETS["configured_queue_expiry_no_dispatch"].update(operation_timeout_s=0.05)
BUDGETS["queue_expiry_no_dispatch"].update(operation_timeout_s=0.05)
BUDGETS["read_disconnect_recovery"].update(rpc_timeout_s=10.0, server_error_max_retries=1)
BUDGETS["rename_commit_loss_converges"].update(rpc_timeout_s=10.0, server_error_max_retries=1)
BUDGETS["retry_auth_backoff_cancelled"].update(
    rpc_timeout_s=10.0,
    rate_limit_max_retries=3,
    server_error_max_retries=3,
    retry_clock="gated_instance",
)
BUDGETS["retry_auth_operation_deadline"].update(
    rpc_timeout_s=10.0,
    operation_timeout_s=0.2,
    rate_limit_max_retries=3,
    server_error_max_retries=3,
    retry_clock="gated_instance",
)
BUDGETS["retry_auth_rate_exhaustion"].update(rpc_timeout_s=10.0, rate_limit_max_retries=1)
BUDGETS["retry_auth_server_budget"].update(rpc_timeout_s=10.0, server_error_max_retries=1)

__all__ = ["BUDGETS", "IMPLEMENTATIONS", "PLANS", "REQUIRED_CHECKS", "SCENARIOS"]
