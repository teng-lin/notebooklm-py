"""Deterministic public-Web-API cohorts for the local fault harness."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

import httpx

from notebooklm import (
    DecodingError,
    NetworkError,
    NotebookLMError,
    RateLimitError,
    RPCTimeoutError,
    ServerError,
)
from notebooklm.client import NotebookLMClient
from notebooklm.outcomes import CommitState
from notebooklm.rpc import RPCMethod

from .common import ScenarioResult
from .http import Disconnect, HttpFaultServer, Reply, Route, Stall, Truncate
from .web import (
    COOKIE_NAME,
    NEW_COOKIE,
    NEW_CSRF,
    NEW_SESSION,
    OLD_COOKIE,
    OLD_CSRF,
    OLD_SESSION,
    build_fault_client,
    create_response,
    homepage_response,
    list_response,
    rpc_response,
)

_READ = Route.rpc(RPCMethod.LIST_NOTEBOOKS.value)
_CREATE = Route.rpc(RPCMethod.CREATE_NOTEBOOK.value)
_HOME = Route.homepage()
_LOGIN = Route.login()

SCENARIOS: tuple[str, ...] = tuple(
    sorted(
        (
            "auth_refresh",
            "auth_refresh_coalesced",
            "auth_refresh_login_redirect",
            "auth_refresh_malformed",
            "cancel_blocked_read",
            "close_reopen",
            "committed_create_disconnect",
            "delayed_headers",
            "malformed_payload",
            "rate_limit_exhaustion",
            "rate_limit_recovery",
            "server_error_exhaustion",
            "server_error_recovery",
            "stalled_body",
            "truncated_body",
            "valid_read_create",
        )
    )
)

_PLANS: dict[str, tuple[tuple[str, ...], int]] = {
    "auth_refresh": (("rpc:400", "homepage:tokens+secure-cookie", "rpc:200"), 1),
    "auth_refresh_coalesced": (
        ("rpc:400x6@gate", "homepage:tokens+secure-cookie", "rpc:200x6"),
        6,
    ),
    "auth_refresh_login_redirect": (
        ("rpc:400", "homepage:302-login", "login:html"),
        1,
    ),
    "auth_refresh_malformed": (("rpc:400", "homepage:malformed"), 1),
    "cancel_blocked_read": (("rpc:stall-headers", "caller:cancel"), 1),
    "close_reopen": (("rpc:stall-headers", "client:close", "client:reopen", "rpc:200"), 1),
    "committed_create_disconnect": (("create:commit", "socket:disconnect"), 1),
    "delayed_headers": (("rpc:stall-headers", "deadline:expire"), 1),
    "malformed_payload": (("rpc:200-malformed-payload",), 1),
    "rate_limit_exhaustion": (("rpc:429x3", "retry:exhaust"), 1),
    "rate_limit_recovery": (("rpc:429-retry-after", "rpc:200"), 1),
    "server_error_exhaustion": (("rpc:503x3", "retry:exhaust"), 1),
    "server_error_recovery": (("rpc:503", "rpc:200"), 1),
    "stalled_body": (("rpc:headers+partial-body", "deadline:expire"), 1),
    "truncated_body": (("rpc:truncated-body-x3", "retry:exhaust"), 1),
    "valid_read_create": (("read:200", "create:200"), 1),
}

_CLOSE_TIMEOUT = 2.0


async def _recording_sleep(result: ScenarioResult, seconds: float) -> None:
    result.record("sleep", seconds=seconds)


@asynccontextmanager
async def _cohort(
    result: ScenarioResult,
    server: HttpFaultServer,
    *,
    timeout: float = 0.5,
    rate_retries: int = 2,
    server_retries: int = 2,
    record_sleep: bool = True,
) -> AsyncIterator[NotebookLMClient]:
    client: NotebookLMClient | None = None
    close_error: BaseException | None = None
    server_close_error: BaseException | None = None
    await server.__aenter__()
    try:

        async def sleep(seconds: float) -> None:
            await _recording_sleep(result, seconds)

        client = build_fault_client(
            server,
            timeout=timeout,
            rate_limit_max_retries=rate_retries,
            server_error_max_retries=server_retries,
            sleep=sleep if record_sleep else None,
        )
        await client.__aenter__()
        yield client
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.close(drain=False), _CLOSE_TIMEOUT)
            except BaseException as exc:
                close_error = exc
        try:
            await asyncio.wait_for(server.aclose(), _CLOSE_TIMEOUT)
        except BaseException as exc:
            server_close_error = exc
        _record_http_trace(result, server)
        result.record(
            "cleanup",
            client_closed=client is None or not client._lifecycle.is_open(),
            active_handlers=server.active_handlers,
            close_error=None if close_error is None else type(close_error).__name__,
            server_close_error=(
                None if server_close_error is None else type(server_close_error).__name__
            ),
            server_errors=list(server.errors),
            remaining_actions=server.remaining(),
        )
        if close_error is not None:
            raise close_error
        if server_close_error is not None:
            raise server_close_error
        result.require("client_closed", client is None or not client._lifecycle.is_open())


def _requests(server: HttpFaultServer, route: Route) -> list[Any]:
    return [record for record in server.journal if record.route == route]


def _record_error(result: ScenarioResult, exc: BaseException | None) -> None:
    result.record(
        "outcome",
        error=None if exc is None else type(exc).__name__,
        message=None if exc is None else str(exc),
    )


def _generation(value: str | None, *, old: str, new: str) -> int | str | None:
    if value is None:
        return None
    if value == old:
        return 1
    if value == new:
        return 2
    return "other-synthetic"


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
                "csrf_generation": _generation(record.csrf, old=OLD_CSRF, new=NEW_CSRF),
                "session_generation": _generation(
                    record.session_id, old=OLD_SESSION, new=NEW_SESSION
                ),
                "cookie_generation": _generation(
                    record.cookie_values.get(COOKIE_NAME), old=OLD_COOKIE, new=NEW_COOKIE
                ),
            }
            for record in server.journal
        ],
        committed=list(server.committed),
    )


def _require_clean(result: ScenarioResult, server: HttpFaultServer) -> None:
    result.require("server_plan_consumed", server.remaining() == 0)
    result.require("server_had_no_errors", not server.errors)
    result.require("server_handlers_drained", server.active_handlers == 0)


async def _valid_read_create(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(_READ, Reply(body=list_response(_READ.rpc_id or "", [("nb-1", "Fault Lab")])))
    server.enqueue(
        _CREATE,
        Reply(body=create_response(_CREATE.rpc_id or "", "nb-created", "Created locally")),
    )
    async with _cohort(result, server, timeout=10.0) as client:
        notebooks = await client.notebooks.list()
        created = await client.notebooks.create("Created locally")
        result.record(
            "decoded",
            list_ids=[notebook.id for notebook in notebooks],
            created_id=created.id,
        )
    result.require("read_decoded", [notebook.id for notebook in notebooks] == ["nb-1"])
    result.require("create_decoded", created.id == "nb-created")
    result.require("one_request_each", len(server.journal) == 2)
    _require_clean(result, server)


async def _rate_limit_recovery(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _READ,
        Reply(429, headers={"Retry-After": "0.25"}),
        Reply(body=list_response(_READ.rpc_id or "", [("nb-rate", "Recovered")])),
    )
    async with _cohort(result, server, timeout=10.0) as client:
        notebooks = await client.notebooks.list()
    sleeps = [event["seconds"] for event in result.events if event["kind"] == "sleep"]
    result.require("rate_limit_recovered", [item.id for item in notebooks] == ["nb-rate"])
    result.require("fractional_retry_after_rounded_up", sleeps == [1])
    result.require("rate_limit_request_count", len(_requests(server, _READ)) == 2)
    _require_clean(result, server)


async def _rate_limit_exhaustion(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(_READ, *(Reply(429, headers={"Retry-After": "0"}) for _ in range(3)))
    error: BaseException | None = None
    async with _cohort(result, server, timeout=10.0) as client:
        try:
            await client.notebooks.list()
        except Exception as exc:
            error = exc
    _record_error(result, error)
    result.require("rate_limit_public_error", isinstance(error, RateLimitError))
    result.require("rate_limit_budget_bounded", len(_requests(server, _READ)) == 3)
    _require_clean(result, server)


async def _server_error_recovery(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _READ,
        Reply(503),
        Reply(body=list_response(_READ.rpc_id or "", [("nb-503", "Recovered")])),
    )
    async with _cohort(result, server, timeout=10.0) as client:
        notebooks = await client.notebooks.list()
    result.require("server_error_recovered", [item.id for item in notebooks] == ["nb-503"])
    result.require("server_error_request_count", len(_requests(server, _READ)) == 2)
    _require_clean(result, server)


async def _server_error_exhaustion(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(_READ, *(Reply(503) for _ in range(3)))
    error: BaseException | None = None
    async with _cohort(result, server, timeout=10.0) as client:
        try:
            await client.notebooks.list()
        except Exception as exc:
            error = exc
    _record_error(result, error)
    result.require("server_error_public_error", isinstance(error, ServerError))
    result.require("server_error_budget_bounded", len(_requests(server, _READ)) == 3)
    _require_clean(result, server)


async def _malformed_payload(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(_READ, Reply(body=rpc_response(_READ.rpc_id or "", "not-a-list")))
    error: BaseException | None = None
    async with _cohort(result, server) as client:
        try:
            await client.notebooks.list()
        except Exception as exc:
            error = exc
    _record_error(result, error)
    result.require("malformed_public_decode_error", isinstance(error, DecodingError))
    result.require("malformed_not_retried", len(_requests(server, _READ)) == 1)
    _require_clean(result, server)


async def _truncated_body(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(_READ, *(Truncate(b"partial", 100) for _ in range(3)))
    error: BaseException | None = None
    async with _cohort(result, server, timeout=10.0) as client:
        try:
            await client.notebooks.list()
        except Exception as exc:
            error = exc
    _record_error(result, error)
    result.require("truncated_public_network_error", isinstance(error, NetworkError))
    result.require("truncated_retry_budget", len(_requests(server, _READ)) == 3)
    _require_clean(result, server)


async def _timeout_scenario(result: ScenarioResult, *, phase: Literal["headers", "body"]) -> None:
    server = HttpFaultServer()
    gate = f"stall-{phase}"
    reply = Reply(body=list_response(_READ.rpc_id or "", []))
    server.enqueue(_READ, Stall(phase, gate, reply, prefix=b")]}'"))
    error: BaseException | None = None
    async with _cohort(result, server, timeout=0.08) as client:
        try:
            await asyncio.wait_for(client.notebooks.list(), 1.5)
        except Exception as exc:
            error = exc
        finally:
            server.release(gate)
            await asyncio.sleep(0)
    _record_error(result, error)
    result.require(f"{phase}_public_timeout", isinstance(error, RPCTimeoutError))
    result.require(f"{phase}_aggregate_deadline_bounded", len(_requests(server, _READ)) == 1)
    _require_clean(result, server)


async def _auth_refresh(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _READ,
        Reply(400),
        Reply(body=list_response(_READ.rpc_id or "", [("nb-auth", "Fresh")])),
    )
    server.enqueue(
        _HOME,
        Reply(
            body=homepage_response(),
            headers={
                "Set-Cookie": (f"{COOKIE_NAME}={NEW_COOKIE}; Domain=.google.com; Path=/; Secure")
            },
        ),
    )
    async with _cohort(result, server) as client:
        notebooks = await client.notebooks.list()
        auth = client.auth
    calls = _requests(server, _READ)
    result.record(
        "auth_generations",
        csrf=[call.csrf for call in calls],
        session=[call.session_id for call in calls],
        cookie=[call.cookie_values.get(COOKIE_NAME) for call in calls],
    )
    result.require("auth_refresh_succeeded", [item.id for item in notebooks] == ["nb-auth"])
    result.require("one_homepage_refresh", len(_requests(server, _HOME)) == 1)
    result.require("csrf_changed", [call.csrf for call in calls] == [OLD_CSRF, NEW_CSRF])
    result.require(
        "session_changed", [call.session_id for call in calls] == [OLD_SESSION, NEW_SESSION]
    )
    result.require(
        "cookie_changed",
        [call.cookie_values.get(COOKIE_NAME) for call in calls] == [OLD_COOKIE, NEW_COOKIE],
    )
    result.require(
        "public_auth_updated", (auth.csrf_token, auth.session_id) == (NEW_CSRF, NEW_SESSION)
    )
    _require_clean(result, server)


async def _auth_refresh_coalesced(result: ScenarioResult) -> None:
    count = 6
    server = HttpFaultServer()
    stale = Stall("headers", "stale-batch", Reply(400))
    fresh = Reply(body=list_response(_READ.rpc_id or "", [("nb-shared", "Shared")]))
    server.enqueue(_READ, *(stale for _ in range(count)), *(fresh for _ in range(count)))
    server.enqueue(
        _HOME,
        Reply(
            body=homepage_response(),
            headers={
                "Set-Cookie": (f"{COOKIE_NAME}={NEW_COOKIE}; Domain=.google.com; Path=/; Secure")
            },
        ),
    )
    calls: list[asyncio.Task[list[Any]]] = []
    async with _cohort(result, server) as client:
        try:
            calls = [asyncio.create_task(client.notebooks.list()) for _ in range(count)]
            await server.wait_for_requests(_READ, count)
            server.release("stale-batch")
            notebooks = await asyncio.wait_for(asyncio.gather(*calls), _CLOSE_TIMEOUT)
        finally:
            server.release("stale-batch")
            for call in calls:
                if not call.done():
                    call.cancel()
            if calls:
                await asyncio.gather(*calls, return_exceptions=True)
    requests = _requests(server, _READ)
    result.require("coalesced_results", all(rows[0].id == "nb-shared" for rows in notebooks))
    result.require("coalesced_one_refresh", len(_requests(server, _HOME)) == 1)
    result.require("coalesced_request_count", len(requests) == count * 2)
    result.require(
        "coalesced_fresh_replays", sum(call.csrf == NEW_CSRF for call in requests) == count
    )
    _require_clean(result, server)


async def _failed_refresh(result: ScenarioResult, *, redirect: bool) -> None:
    server = HttpFaultServer()
    server.enqueue(_READ, Reply(400))
    if redirect:
        server.enqueue(
            _HOME,
            Reply(302, headers={"Location": "https://accounts.google.com/ServiceLogin"}),
        )
        server.enqueue(_LOGIN, Reply(body=b"<html>sign in</html>"))
    else:
        server.enqueue(_HOME, Reply(body=b"<html>missing bootstrap fields</html>"))
    error: BaseException | None = None
    async with _cohort(result, server) as client:
        try:
            await client.notebooks.list()
        except Exception as exc:
            error = exc
    _record_error(result, error)
    label = "login" if redirect else "malformed"
    result.require(f"{label}_refresh_original_status", isinstance(error, httpx.HTTPStatusError))
    result.require(f"{label}_refresh_failure_cause", isinstance(error.__cause__, ValueError))
    result.require(f"{label}_refresh_no_replay", len(_requests(server, _READ)) == 1)
    result.require(f"{label}_one_homepage", len(_requests(server, _HOME)) == 1)
    if redirect:
        result.require("logical_login_followed", len(_requests(server, _LOGIN)) == 1)
    _require_clean(result, server)


async def _committed_create_disconnect(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(_CREATE, Disconnect(commit_id="nb-committed"))
    error: BaseException | None = None
    async with _cohort(result, server, server_retries=5) as client:
        try:
            await client.notebooks.create("Committed once")
        except Exception as exc:
            error = exc
    _record_error(result, error)
    result.require("create_public_network_error", isinstance(error, NetworkError))
    result.require(
        "create_unknown_commit", getattr(error, "commit_state", None) is CommitState.UNKNOWN
    )
    result.require("create_unconfirmed", bool(getattr(error, "unconfirmed", False)))
    result.require("create_sent_once", len(_requests(server, _CREATE)) == 1)
    result.require("create_committed_once", server.committed == ["nb-committed"])
    _require_clean(result, server)


async def _cancel_blocked_read(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _READ, Stall("headers", "cancel-read", Reply(body=list_response(_READ.rpc_id or "", [])))
    )
    cancelled = False
    async with _cohort(result, server, server_retries=0) as client:
        task = asyncio.create_task(client.notebooks.list())
        await server.wait_for_requests(_READ, 1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        server.release("cancel-read")
        await asyncio.sleep(0)
    result.require("blocked_read_cancelled", cancelled)
    result.require("cancel_sent_once", len(_requests(server, _READ)) == 1)
    _require_clean(result, server)


async def _close_reopen(result: ScenarioResult) -> None:
    server = HttpFaultServer()
    server.enqueue(
        _READ,
        Stall("headers", "close-read", Reply(body=list_response(_READ.rpc_id or "", []))),
        Reply(body=list_response(_READ.rpc_id or "", [("nb-reopen", "Reopened")])),
    )
    first_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    server_close_error: BaseException | None = None
    reopened: list[Any] = []
    await server.__aenter__()
    client = build_fault_client(server, server_error_max_retries=0)
    try:
        await client.__aenter__()
        task = asyncio.create_task(client.notebooks.list())
        await server.wait_for_requests(_READ, 1)
        await asyncio.wait_for(client.close(drain=False), _CLOSE_TIMEOUT)
        try:
            await asyncio.wait_for(task, _CLOSE_TIMEOUT)
        except BaseException as exc:
            first_error = exc
        server.release("close-read")
        await client.__aenter__()
        reopened = await client.notebooks.list()
        await asyncio.wait_for(client.close(drain=False), _CLOSE_TIMEOUT)
    finally:
        server.release("close-read")
        try:
            await asyncio.wait_for(client.close(drain=False), _CLOSE_TIMEOUT)
        except BaseException as exc:
            cleanup_error = exc
        try:
            await asyncio.wait_for(server.aclose(), _CLOSE_TIMEOUT)
        except BaseException as exc:
            server_close_error = exc
        _record_http_trace(result, server)
        result.record(
            "cleanup",
            client_closed=not client._lifecycle.is_open(),
            active_handlers=server.active_handlers,
            close_error=None if cleanup_error is None else type(cleanup_error).__name__,
            server_close_error=(
                None if server_close_error is None else type(server_close_error).__name__
            ),
            server_errors=list(server.errors),
            remaining_actions=server.remaining(),
        )
        if cleanup_error is not None:
            raise cleanup_error
        if server_close_error is not None:
            raise server_close_error
        result.require("client_closed", not client._lifecycle.is_open())
    _record_error(result, first_error)
    result.require("active_read_terminated", isinstance(first_error, NotebookLMError))
    result.require("reopen_succeeded", [item.id for item in reopened] == ["nb-reopen"])
    result.require("close_reopen_request_count", len(_requests(server, _READ)) == 2)
    _require_clean(result, server)


_IMPLEMENTATIONS: dict[str, Callable[[ScenarioResult], Awaitable[None]]] = {
    "auth_refresh": _auth_refresh,
    "auth_refresh_coalesced": _auth_refresh_coalesced,
    "auth_refresh_login_redirect": lambda result: _failed_refresh(result, redirect=True),
    "auth_refresh_malformed": lambda result: _failed_refresh(result, redirect=False),
    "cancel_blocked_read": _cancel_blocked_read,
    "close_reopen": _close_reopen,
    "committed_create_disconnect": _committed_create_disconnect,
    "delayed_headers": lambda result: _timeout_scenario(result, phase="headers"),
    "malformed_payload": _malformed_payload,
    "rate_limit_exhaustion": _rate_limit_exhaustion,
    "rate_limit_recovery": _rate_limit_recovery,
    "server_error_exhaustion": _server_error_exhaustion,
    "server_error_recovery": _server_error_recovery,
    "stalled_body": lambda result: _timeout_scenario(result, phase="body"),
    "truncated_body": _truncated_body,
    "valid_read_create": _valid_read_create,
}


async def run_scenario(
    name: str,
    *,
    operation_id: str,
    result: ScenarioResult | None = None,
) -> ScenarioResult:
    """Run one bounded Web cohort and retain evidence on every failure path."""
    implementation = _IMPLEMENTATIONS.get(name)
    if implementation is None:
        raise ValueError(f"unknown web fault scenario {name!r}; choose from {SCENARIOS!r}")
    if result is None:
        result = ScenarioResult("web", name, operation_id)
    elif (result.backend, result.scenario, result.operation_id) != ("web", name, operation_id):
        raise ValueError("supplied ScenarioResult identity does not match this web operation")
    faults, cohort_count = _PLANS[name]
    result.record(
        "plan",
        faults=list(faults),
        cohort_ids=[f"{operation_id}:{index}" for index in range(cohort_count)],
    )
    await implementation(result)
    return result


__all__ = ["SCENARIOS", "run_scenario"]
