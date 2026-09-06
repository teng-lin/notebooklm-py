"""R14 adapter projections over the real Web client and loopback fault server.

The backend Web scenarios establish retry and commit facts.  These deliberately
thin scenarios compose that production client with each public adapter so they
prove its visible error contract without duplicating retry-policy coverage.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.exceptions import ToolError

from notebooklm.mcp.server import create_server
from notebooklm.server.app import create_app

from .common import ScenarioResult
from .http import Disconnect, HttpFaultServer, Reply, Route
from .web import build_fault_client, list_response

_READ = Route.rpc("wXbhsf")
_CREATE = Route.rpc("CCqFvf")
_CLOSE_TIMEOUT = 2.0

SCENARIOS: tuple[str, ...] = tuple(
    sorted(
        (
            "adapter_cli_ambiguous_create",
            "adapter_cli_transient_read",
            "adapter_mcp_ambiguous_create",
            "adapter_mcp_transient_read",
            "adapter_rest_ambiguous_create",
            "adapter_rest_transient_read",
        )
    )
)


def _record_trace(result: ScenarioResult, server: HttpFaultServer) -> None:
    result.record(
        "http_trace",
        requests=[
            {
                "sequence": item.sequence,
                "method": item.route.method,
                "host": item.route.host,
                "path": item.route.path,
                "rpc_id": item.route.rpc_id,
                "action": item.action,
            }
            for item in server.journal
        ],
        committed=list(server.committed),
    )


@asynccontextmanager
async def _fault_server(
    result: ScenarioResult, setup: Callable[[HttpFaultServer], None]
) -> AsyncIterator[HttpFaultServer]:
    server = HttpFaultServer()
    setup(server)
    await server.__aenter__()
    try:
        yield server
    finally:
        close_error: BaseException | None = None
        try:
            await asyncio.wait_for(server.aclose(), _CLOSE_TIMEOUT)
        except BaseException as exc:  # noqa: BLE001 - preserve teardown evidence
            close_error = exc
        _record_trace(result, server)
        result.record(
            "cleanup",
            active_handlers=server.active_handlers,
            server_errors=list(server.errors),
            remaining_actions=server.remaining(),
            server_close_error=None if close_error is None else type(close_error).__name__,
        )
        if close_error is not None:
            raise close_error
        result.require("server_handlers_drained", server.active_handlers == 0)
        result.require("server_had_no_errors", not server.errors)
        result.require("server_plan_consumed", server.remaining() == 0)


def _client_factory(
    server: HttpFaultServer,
    opened: list[Any],
    *,
    server_retries: int,
    recovery: list[str] | None = None,
) -> Callable[[], AsyncIterator[Any]]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[Any]:
        client = build_fault_client(
            server,
            timeout=0.5,
            server_error_max_retries=server_retries,
        )
        opened.append(client)
        await client.__aenter__()
        try:
            yield client
        except Exception:
            if recovery is not None:
                recovered = await client.notebooks.list()
                recovery.extend(notebook.id for notebook in recovered)
            raise
        finally:
            await client.close(drain=False)

    return factory


def _require_client_closed(result: ScenarioResult, opened: list[Any], *, count: int = 1) -> None:
    result.require("expected_clients_opened", len(opened) == count)
    result.require(
        "client_closed",
        len(opened) == count and all(not client._lifecycle.is_open() for client in opened),
    )


def _read_reply(notebook_id: str) -> Reply:
    return Reply(body=list_response(_READ.rpc_id or "", [(notebook_id, "Fault adapter")]))


def _enqueue_read(server: HttpFaultServer) -> None:
    server.enqueue(_READ, _read_reply("nb-before"), Reply(503), _read_reply("nb-recovered"))


def _enqueue_create(server: HttpFaultServer) -> None:
    server.enqueue(_READ, _read_reply("nb-before"), _read_reply("nb-recovered"))
    server.enqueue(_CREATE, Disconnect(commit_id="nb-committed"))


async def _rest_read(result: ScenarioResult) -> None:
    async with _fault_server(result, _enqueue_read) as server:
        opened: list[Any] = []
        app = create_app(client_factory=_client_factory(server, opened, server_retries=0))
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 55141))
        headers = {"Authorization": "Bearer adapter-fault-token", "Host": "127.0.0.1"}
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as caller,
        ):
            before = await caller.get("/v1/notebooks", headers=headers)
            response = await caller.get("/v1/notebooks", headers=headers)
            recovered = await caller.get("/v1/notebooks", headers=headers)
        body = response.json()
        result.record(
            "adapter_outcome",
            adapter="rest",
            status=response.status_code,
            category=body.get("error", {}).get("category"),
            retriable=body.get("error", {}).get("retriable"),
            preflight_status=before.status_code,
            recovery_status=recovered.status_code,
        )
        result.require(
            "rest_read_preflight_decoded", before.json()["notebooks"][0]["id"] == "nb-before"
        )
        result.require("rest_read_status", response.status_code == 502)
        result.require("rest_read_category", body.get("error", {}).get("category") == "server")
        result.require("rest_read_retriable", body.get("error", {}).get("retriable") is True)
        result.require("rest_read_fault_sent_once", len(server.journal) == 3)
        result.require(
            "rest_read_recovery_decoded", recovered.json()["notebooks"][0]["id"] == "nb-recovered"
        )
        _require_client_closed(result, opened)


async def _rest_create(result: ScenarioResult) -> None:
    async with _fault_server(result, _enqueue_create) as server:
        opened: list[Any] = []
        app = create_app(client_factory=_client_factory(server, opened, server_retries=5))
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 55142))
        headers = {"Authorization": "Bearer adapter-fault-token", "Host": "127.0.0.1"}
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as caller,
        ):
            before = await caller.get("/v1/notebooks", headers=headers)
            response = await caller.post(
                "/v1/notebooks",
                headers=headers,
                json={"title": "Committed once"},
            )
            recovered = await caller.get("/v1/notebooks", headers=headers)
        body = response.json()
        error = body.get("error", {})
        result.record(
            "adapter_outcome",
            adapter="rest",
            status=response.status_code,
            category=error.get("category"),
            retriable=error.get("retriable"),
            unconfirmed=error.get("unconfirmed"),
            commit_state=error.get("commit_state"),
            preflight_status=before.status_code,
            recovery_status=recovered.status_code,
        )
        result.require(
            "rest_create_preflight_decoded", before.json()["notebooks"][0]["id"] == "nb-before"
        )
        result.require("rest_create_status", response.status_code == 502)
        result.require("rest_create_category", error.get("category") == "rpc")
        result.require("rest_create_not_retriable", error.get("retriable") is False)
        result.require("rest_create_unconfirmed", error.get("unconfirmed") is True)
        result.require("rest_create_unknown_commit", error.get("commit_state") == "unknown")
        result.require(
            "rest_create_sent_once",
            len([row for row in server.journal if row.route == _CREATE]) == 1,
        )
        result.require("rest_create_committed_once", server.committed == ["nb-committed"])
        result.require(
            "rest_create_recovery_decoded", recovered.json()["notebooks"][0]["id"] == "nb-recovered"
        )
        _require_client_closed(result, opened)


async def _mcp_read(result: ScenarioResult) -> None:
    async with _fault_server(result, _enqueue_read) as server:
        opened: list[Any] = []
        mcp = create_server(client_factory=_client_factory(server, opened, server_retries=0))
        error: ToolError | None = None
        async with Client(mcp) as caller:
            before = await caller.call_tool("notebook_list", {})
            try:
                await caller.call_tool("notebook_list", {})
            except ToolError as exc:
                error = exc
            recovered = await caller.call_tool("notebook_list", {})
        message = "" if error is None else str(error)
        result.record(
            "adapter_outcome",
            adapter="mcp",
            code=message.partition(":")[0],
            retriable="retriable=true" in message,
        )
        result.require("mcp_read_tool_error", error is not None)
        result.require("mcp_read_code", message.startswith("SERVER:"))
        result.require("mcp_read_retriable", "retriable=true" in message)
        result.require(
            "mcp_read_preflight_decoded",
            before.structured_content["notebooks"][0]["id"] == "nb-before",
        )
        result.require("mcp_read_fault_sent_once", len(server.journal) == 3)
        result.require(
            "mcp_read_recovery_decoded",
            recovered.structured_content["notebooks"][0]["id"] == "nb-recovered",
        )
        _require_client_closed(result, opened)


async def _mcp_create(result: ScenarioResult) -> None:
    async with _fault_server(result, _enqueue_create) as server:
        opened: list[Any] = []
        mcp = create_server(client_factory=_client_factory(server, opened, server_retries=5))
        error: ToolError | None = None
        async with Client(mcp) as caller:
            before = await caller.call_tool("notebook_list", {})
            try:
                await caller.call_tool("notebook_create", {"title": "Committed once"})
            except ToolError as exc:
                error = exc
            recovered = await caller.call_tool("notebook_list", {})
        message = "" if error is None else str(error)
        result.record(
            "adapter_outcome",
            adapter="mcp",
            code=message.partition(":")[0],
            retriable="retriable=true" in message,
            unconfirmed="unconfirmed=true" in message,
        )
        result.require("mcp_create_tool_error", error is not None)
        result.require("mcp_create_code", message.startswith("RPC:"))
        result.require("mcp_create_not_retriable", "retriable=false" in message)
        result.require("mcp_create_unconfirmed", "unconfirmed=true" in message)
        result.require(
            "mcp_create_preflight_decoded",
            before.structured_content["notebooks"][0]["id"] == "nb-before",
        )
        result.require(
            "mcp_create_sent_once",
            len([row for row in server.journal if row.route == _CREATE]) == 1,
        )
        result.require("mcp_create_committed_once", server.committed == ["nb-committed"])
        result.require(
            "mcp_create_recovery_decoded",
            recovered.structured_content["notebooks"][0]["id"] == "nb-recovered",
        )
        _require_client_closed(result, opened)


async def _run_cli(
    scenario: str,
) -> dict[str, Any]:
    """Run Click in an isolated process, where its global stdio/auth patches are safe.

    ``CliRunner`` replaces process-global streams.  The child owns both its
    loopback server and its one worker thread, so no stress cohort can observe
    its test-only authentication patch or its Click stream replacement.
    """
    with tempfile.TemporaryDirectory(prefix="notebooklm-adapter-cli-") as directory:
        report = Path(directory) / "result.json"
        command = [
            sys.executable,
            "-m",
            "tests._fault_server.adapter_cli_worker",
            "--scenario",
            scenario,
            "--report",
            str(report),
        ]
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=6.0,
        )
        if completed.returncode != 0 or not report.is_file():
            raise AssertionError("isolated CLI fault worker did not produce a report")
        return json.loads(report.read_text(encoding="utf-8"))


async def _cli_read(result: ScenarioResult) -> None:
    worker = await _run_cli("read")
    result.record("http_trace", requests=worker["http_trace"], committed=worker["committed"])
    result.record(
        "adapter_outcome",
        adapter="cli",
        exit_code=worker["exit_code"],
        code=worker["code"],
        preflight_exit=worker["preflight_exit"],
        recovery_ids=worker["recovery_ids"],
    )
    result.require("cli_read_preflight_decoded", worker["preflight_id"] == "nb-before")
    result.require("cli_read_exit", worker["exit_code"] == 1)
    result.require("cli_read_code", worker["code"] == "NOTEBOOKLM_ERROR")
    result.require("cli_read_fault_sent_once", worker["request_count"] == 3)
    result.require("cli_read_same_client_recovery", worker["recovery_ids"] == ["nb-recovered"])
    result.require("cli_read_worker_cleanup", worker["clean"] is True)


async def _cli_create(result: ScenarioResult) -> None:
    worker = await _run_cli("create")
    result.record("http_trace", requests=worker["http_trace"], committed=worker["committed"])
    result.record(
        "adapter_outcome",
        adapter="cli",
        exit_code=worker["exit_code"],
        code=worker["code"],
        unconfirmed=worker["unconfirmed"],
        commit_state=worker["commit_state"],
        hint_is_unconfirmed=worker["hint_is_unconfirmed"],
        preflight_exit=worker["preflight_exit"],
        recovery_ids=worker["recovery_ids"],
    )
    result.require("cli_create_preflight_decoded", worker["preflight_id"] == "nb-before")
    result.require("cli_create_exit", worker["exit_code"] == 1)
    result.require("cli_create_code", worker["code"] == "UNCONFIRMED_WRITE")
    result.require("cli_create_unconfirmed", worker["unconfirmed"] is True)
    result.require("cli_create_unknown_commit", worker["commit_state"] == "unknown")
    result.require("cli_create_hint", worker["hint_is_unconfirmed"] is True)
    result.require("cli_create_sent_once", worker["create_requests"] == 1)
    result.require("cli_create_committed_once", worker["committed"] == ["nb-committed"])
    result.require("cli_create_same_client_recovery", worker["recovery_ids"] == ["nb-recovered"])
    result.require("cli_create_worker_cleanup", worker["clean"] is True)


_IMPLEMENTATIONS: dict[str, Callable[[ScenarioResult], Awaitable[None]]] = {
    "adapter_cli_ambiguous_create": _cli_create,
    "adapter_cli_transient_read": _cli_read,
    "adapter_mcp_ambiguous_create": _mcp_create,
    "adapter_mcp_transient_read": _mcp_read,
    "adapter_rest_ambiguous_create": _rest_create,
    "adapter_rest_transient_read": _rest_read,
}


async def run_scenario(
    name: str,
    *,
    operation_id: str,
    result: ScenarioResult | None = None,
) -> ScenarioResult:
    """Run one adapter case with a precise request/commit journal."""
    implementation = _IMPLEMENTATIONS.get(name)
    if implementation is None:
        raise ValueError(f"unknown adapter fault scenario {name!r}; choose from {SCENARIOS!r}")
    if result is None:
        result = ScenarioResult("web", name, operation_id)
    elif (result.backend, result.scenario, result.operation_id) != ("web", name, operation_id):
        raise ValueError("supplied ScenarioResult identity does not match adapter scenario")
    fault = "create:commit+disconnect" if "create" in name else "read:503"
    _, adapter, _ = name.split("_", 2)
    result.record(
        "plan",
        adapter=adapter,
        faults=[fault],
        cohort_ids=[operation_id],
        budgets={"rpc_timeout_s": 0.5, "cleanup_timeout_s": _CLOSE_TIMEOUT},
    )
    await implementation(result)
    return result


__all__ = ["SCENARIOS", "run_scenario"]
