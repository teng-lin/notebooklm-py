"""Pytest-only real MCP startup: synthetic stored credentials, real auth owners."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.streamable_http import streamable_http_client

from tests._fault_server.common import ScenarioResult
from tests._fault_server.http import HttpFaultServer, Reply, Route, Stall
from tests._fault_server.mcp_startup_cleanup import settle_calls_and_upstream, settle_http_worker
from tests._fault_server.mcp_stdio_session import stdio_session
from tests._fault_server.web import NEW_CSRF, NEW_SESSION, homepage_response, list_response

pytestmark = pytest.mark.allow_no_vcr
_READ = Route.rpc("wXbhsf")
_ROTATE = Route("POST", "accounts.google.com", "/RotateCookies")


@pytest.fixture
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    """Subprocess ownership requires the Proactor loop on Windows."""
    if sys.platform == "win32":
        return asyncio.WindowsProactorEventLoopPolicy()
    return asyncio.get_event_loop_policy()


async def _observe(path: Path, predicate, *, timeout: float = 10):
    async def poll():
        while True:
            if path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
                if predicate(value):
                    return value
            await asyncio.sleep(0.025)

    return await asyncio.wait_for(poll(), timeout)


@asynccontextmanager
async def _mcp_connection(directory: Path, port: int, transport: str):
    args = [
        "-m",
        "tests._fault_server.mcp_auth_startup_worker",
        "--port",
        str(port),
        "--directory",
        str(directory),
        "--transport",
        transport,
    ]
    env = {key: value for key, value in os.environ.items() if not key.startswith("NOTEBOOKLM_")}
    env.update(
        NOTEBOOKLM_HOME=str(directory),
        NOTEBOOKLM_PROFILE="agent-auth-startup",
        NOTEBOOKLM_TRANSPORT="httpx",
    )
    with (directory / "stderr.log").open("w", encoding="utf-8") as errors:
        if transport == "stdio":
            params = StdioServerParameters(command=sys.executable, args=args, env=env)
            async with stdio_session(params, directory, errors) as session:
                yield session
        else:
            process = await asyncio.create_subprocess_exec(
                sys.executable, *args, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=errors
            )
            primary = None
            try:

                async def ready():
                    while not (directory / "ready").exists():
                        assert process.returncode is None, "MCP worker exited before readiness"
                        await asyncio.sleep(0.025)
                    return json.loads((directory / "ready").read_text(encoding="utf-8"))["url"]

                url = await asyncio.wait_for(ready(), 10)
                async with (
                    streamable_http_client(url) as (reader, writer, _),
                    ClientSession(reader, writer) as session,
                ):
                    yield session
            except BaseException as error:
                primary = error
                raise
            finally:
                await settle_http_worker(process, directory, primary)
            assert process.returncode == 0


@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("fault", ["stall", "failure", "cancel-waiter", "shutdown"])
async def test_stored_auth_mcp_startup(tmp_path: Path, transport: str, fault: str) -> None:
    """Declare the complete pytest-only cohort before allocating its owned resources."""
    result = ScenarioResult("web", f"mcp_stored_auth_{fault}", f"pytest-{transport}-{fault}")
    required = [
        "discovery_during_open",
        "synthetic_auth_routed",
        "worker_settled",
        "opening_count_bounded",
        "exact_network_work",
        "secrets_excluded",
        "persisted_state_contract",
        "parent_owners_settled",
    ]
    if fault != "shutdown":
        required.append("healthy_tool_recovery")
    if fault == "failure":
        required.append("opening_failure_visible")
    if fault == "cancel-waiter":
        required.append("shared_waiter_isolated")
    gate_order = ["rotate", "homepage_opening_gate", "initialize", "discovery"]
    if fault == "cancel-waiter":
        gate_order += ["two_waiters_join", "cancel_one_waiter", "observe_one_cancellation"]
    if fault == "shutdown":
        gate_order += ["shutdown_before_gate_release"]
    elif fault == "failure":
        gate_order += ["release_opening", "failed_tool_observed", "retry_opening", "healthy_tool"]
    else:
        gate_order += ["release_opening", "healthy_tool"]
    result.record(
        "plan",
        backend="web",
        transport=transport,
        pytest_only=True,
        public_entry_points=[
            "NotebookLMClient.from_storage",
            "MCP.initialize",
            "MCP.list_tools",
            "MCP.server_info",
            "MCP.notebook_list",
        ],
        fixture="synthetic-stored-cookies-and-token-issuance",
        faults=[fault],
        cohort_ids=[result.operation_id],
        required_checks=required,
        gate_order=gate_order,
        limits={
            "upstream_requests": 2 + int(fault != "shutdown") + int(fault == "failure"),
            "application_commits": 0,
            "client_opens": 2 if fault == "failure" else 1,
            "server_retries": 0,
            "rate_limit_retries": 0,
        },
        budgets={
            "scenario_operation_timeout_s": 45,
            "opening_gate_timeout_s": 10,
            "discovery_call_timeout_s": 3,
            "state_observation_timeout_s": 10,
            "tool_timeout_s": 5,
            "http_readiness_timeout_s": 10,
            "http_child_lifetime_s": 25,
            "stdio_receiver_drain_s": 2,
            "worker_graceful_stop_s": 5,
            "worker_forced_stop_s": 2,
            "upstream_close_s": 2,
            "caller_settlement_s": 2,
            "caller_settlement_retry_s": 2,
        },
    )
    try:
        await asyncio.wait_for(_exercise_stored_auth(tmp_path, transport, fault, result), 45)
        assert set(result.checks) == set(required)
        assert all(result.checks.values())
    except BaseException as error:
        result.record("failure", error_type=type(error).__name__)
        raise
    finally:
        # Captured by pytest on failure: never serialize credentials or raw exceptions.
        print(json.dumps({"events": result.events, "checks": result.checks}))


async def _exercise_stored_auth(
    tmp_path: Path, transport: str, fault: str, result: ScenarioResult
) -> None:
    """Discovery, retry and shared-open ownership cross the real auth/network path."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": name,
                        "value": "synthetic-" + name,
                        "domain": ".google.com",
                        "path": "/",
                        "expires": -1,
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "None",
                    }
                    for name in ("SID", "__Secure-1PSIDTS")
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    # Fresh files intentionally suppress rotation; model a stored older session.
    os.utime(storage, (1_700_000_000, 1_700_000_000))
    upstream = HttpFaultServer()
    upstream.enqueue(
        _ROTATE,
        Reply(
            headers={
                "Set-Cookie": "__Secure-1PSIDTS=synthetic-rotated; Domain=.google.com; Path=/; Secure"
            }
        ),
    )
    first = Reply(503) if fault == "failure" else Reply(body=homepage_response())
    upstream.enqueue(Route.homepage(), Stall("headers", "opening", first))
    if fault == "failure":
        upstream.enqueue(Route.homepage(), Reply(body=homepage_response()))
    if fault != "shutdown":
        upstream.enqueue(
            _READ, Reply(body=list_response("wXbhsf", [("nb-recovered", "Recovered")]))
        )
    report = tmp_path / "report.json"
    calls: list[asyncio.Task] = []
    primary = None
    try:
        await upstream.__aenter__()
        async with _mcp_connection(tmp_path, upstream.address[1], transport) as session:
            await upstream.wait_for_gate("opening", timeout=10)
            await asyncio.wait_for(session.initialize(), 3)
            listed = await asyncio.wait_for(session.list_tools(), 3)
            assert {"server_info", "notebook_list"} <= {tool.name for tool in listed.tools}
            info = await asyncio.wait_for(session.call_tool("server_info", {}), 3)
            assert not info.isError
            assert not upstream.gate("opening").is_set()
            result.require("discovery_during_open", True)
            assert [row.route for row in upstream.journal] == [_ROTATE, Route.homepage()]
            assert upstream.journal[0].cookie_names == ("SID", "__Secure-1PSIDTS")
            assert upstream.journal[1].cookie_values["__Secure-1PSIDTS"] == "synthetic-rotated"
            result.require("synthetic_auth_routed", True)
            if fault != "shutdown":
                request_id = session._request_id
                first_call = asyncio.create_task(session.call_tool("notebook_list", {}))
                calls.append(first_call)
                await _observe(report, lambda state: state["waiters"] >= 1)
                if fault == "cancel-waiter":
                    survivor = asyncio.create_task(session.call_tool("notebook_list", {}))
                    calls.append(survivor)
                    await _observe(report, lambda state: state["waiters"] >= 2)
                    # Native task cancellation does not send MCP cancellation.
                    await session.send_notification(
                        types.ClientNotification(
                            types.CancelledNotification(
                                params=types.CancelledNotificationParams(
                                    requestId=request_id, reason="fault waiter departed"
                                )
                            )
                        )
                    )
                    first_call.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first_call
                    cancelled = await _observe(
                        report, lambda state: state["cancelled_waiters"] >= 1
                    )
                    assert cancelled["cancelled_waiters"] == 1
                    assert cancelled["opens"] == 1
                    assert not survivor.done()
                    result.require("shared_waiter_isolated", True)
                    first_call = survivor
                upstream.release("opening")
                response = await asyncio.wait_for(first_call, 5)
                if fault == "failure":
                    assert response.isError
                    result.require("opening_failure_visible", True)
                    response = await asyncio.wait_for(session.call_tool("notebook_list", {}), 5)
                assert not response.isError
                assert response.structuredContent["notebooks"][0]["id"] == "nb-recovered"
                result.require("healthy_tool_recovery", True)
        state = await _observe(report, lambda state: state.get("settled"))
        assert state["http_closed"] and state["client_closed"]
        assert state["library_clients_created"] == int(fault != "shutdown")
        assert not state["report_errors"]
        result.require("worker_settled", True)
        assert state["opens"] == (2 if fault == "failure" else 1)
        assert len(state["errors"]) == int(fault == "failure")
        assert state["cancelled_waiters"] == int(fault == "cancel-waiter")
        result.require("opening_count_bounded", True)
        reads = [row for row in upstream.journal if row.route == _READ]
        assert len(reads) == int(fault != "shutdown")
        for row in reads:
            assert row.csrf == NEW_CSRF and row.session_id == NEW_SESSION
            assert row.cookie_values["__Secure-1PSIDTS"] == (
                "synthetic-__Secure-1PSIDTS" if fault == "failure" else "synthetic-rotated"
            )
        assert upstream.remaining() == 0
        assert len(upstream.journal) == 2 + int(fault != "shutdown") + int(fault == "failure")
        assert not upstream.committed
        result.require("exact_network_work", True)
        result.record(
            "http_trace",
            requests=len(upstream.journal),
            rotates=sum(row.route == _ROTATE for row in upstream.journal),
            homepages=sum(row.route == Route.homepage() for row in upstream.journal),
            notebook_reads=len(reads),
            commits=len(upstream.committed),
        )
        logs = (tmp_path / "stderr.log").read_text(encoding="utf-8")
        assert all(
            secret not in logs
            for secret in (
                "synthetic-rotated",
                "synthetic-SID",
                "synthetic-__Secure-1PSIDTS",
                NEW_CSRF,
                NEW_SESSION,
            )
        )
        result.require("secrets_excluded", True)
        assert any(
            cookie["value"]
            == (
                "synthetic-__Secure-1PSIDTS"
                if fault in {"failure", "shutdown"}
                else "synthetic-rotated"
            )
            for cookie in json.loads(storage.read_text(encoding="utf-8"))["cookies"]
        )
        result.require("persisted_state_contract", True)
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            await settle_calls_and_upstream(calls, upstream, tmp_path, primary)
        finally:
            result.record(
                "cleanup",
                active_handlers=upstream.active_handlers,
                pending_callers=sum(not task.done() for task in calls),
                primary_error=None if primary is None else type(primary).__name__,
            )
    assert upstream.active_handlers == 0
    assert not upstream.errors
    assert all(task.done() for task in calls)
    result.require("parent_owners_settled", True)
