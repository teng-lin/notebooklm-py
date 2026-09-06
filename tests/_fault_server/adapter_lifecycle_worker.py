"""Isolated live adapter cohorts (MCP's download counter is process-owned).

All upstream traffic uses the production Web assembly and real loopback sockets.
Only spool allocation and the downstream ASGI send boundary are observed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import signal
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from notebooklm._adapter_support import client_generation_epoch
from notebooklm.mcp import _fileroutes
from notebooklm.mcp._filelink import FileLinkSigner, FileTransferConfig
from notebooklm.mcp.server import create_server
from notebooklm.server.app import create_app

from .adapter_listener import DisconnectGate, live_listener
from .adapter_scenarios import _fault_server, _require_client_closed
from .common import ScenarioResult
from .http import HttpFaultServer, Reply, Stall
from .web import build_fault_client, rpc_response
from .web_streaming import CHAT, CONVERSATION, TURNS, _frame
from .web_transfers import ASSET, GET_NOTEBOOK, LIST_ASSETS, MEDIA, NOTEBOOK, _audio_rows, _probe


async def _until(predicate: Any) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), 1)


async def download_case(result: ScenarioResult, adapter: str) -> None:
    # Valid PCM WAV; enough bytes for the outgoing prefix to be strictly partial.
    samples = bytes(128 * 1024)
    media = MEDIA[:4] + (36 + len(samples)).to_bytes(4, "little") + MEDIA[8:40]
    media += len(samples).to_bytes(4, "little") + samples
    opened: list[Any] = []
    transports: list[httpx.AsyncClient] = []
    spools: list[Path] = []
    gate: DisconnectGate | None = None

    def setup(server: HttpFaultServer) -> None:
        server.enqueue(
            LIST_ASSETS,
            *[
                Reply(body=rpc_response(LIST_ASSETS.rpc_id or "", [_audio_rows()]))
                for _ in range(3)
            ],
        )
        server.enqueue(ASSET, *[Reply(body=media) for _ in range(3)])

    # _fault_server uses the default hosts; add the explicit logical asset host
    # before binding the instance (the transport fails closed for every other host).
    def configured(server: HttpFaultServer) -> None:
        server._hosts.add(ASSET.host)
        setup(server)

    with tempfile.TemporaryDirectory(prefix="fault-adapter-spools-") as root:

        def spool() -> str:
            path = Path(tempfile.mkdtemp(dir=root))
            spools.append(path)
            return str(path)

        async with _fault_server(result, configured) as upstream:

            @asynccontextmanager
            async def factory():
                def transfer_factory(**kwargs: Any) -> httpx.AsyncClient:
                    transport = upstream.client_factory(**kwargs)
                    transports.append(transport)
                    return transport

                client = build_fault_client(
                    upstream,
                    timeout=1,
                    server_error_max_retries=0,
                    transfer_client_factory=transfer_factory,
                )
                opened.append(client)
                async with client:
                    yield client

            if adapter == "rest":
                app = create_app(client_factory=factory, _download_temp_factory=spool)
                route = f"/v1/notebooks/{NOTEBOOK}/artifacts/download"
                kwargs: dict[str, Any] = {
                    "method": "POST",
                    "json": {"type": "audio"},
                    "headers": {"Authorization": "Bearer adapter-fault-token"},
                }
            else:
                config = FileTransferConfig(
                    signer=FileLinkSigner(b"fault-adapter-signing-key-32-bytes"),
                    base_url="https://adapter.invalid",
                    _download_temp_factory=spool,
                )
                mcp = create_server(client_factory=factory, file_transfer=config)
                app = mcp.http_app()
                route = config.download_url({"nb": NOTEBOOK, "atype": "audio"}).removeprefix(
                    config.base_url
                )
                kwargs = {"method": "GET", "headers": {}}
            gate = DisconnectGate(app)
            try:
                async with live_listener(gate) as (address, listener):
                    async with httpx.AsyncClient(base_url=address, timeout=2) as caller:
                        baseline = await caller.request(url=route, **kwargs)
                        result.require(
                            "valid_transfer_baseline",
                            baseline.status_code == 200 and baseline.content == media,
                        )
                        await _until(lambda: len(spools) == 1 and not spools[0].exists())
                        headers = {**kwargs["headers"], "x-fault-gate": "disconnect"}
                        fault_kwargs = {**kwargs, "headers": headers}
                        async with caller.stream(url=route, **fault_kwargs) as response:
                            prefix = await response.aiter_raw().__anext__()
                            await asyncio.wait_for(gate.prefix_sent.wait(), 1)
                            result.require(
                                "caller_received_prefix",
                                response.status_code == 200
                                and 0 < len(prefix) < len(media)
                                and media.startswith(prefix),
                            )
                            result.require(
                                "spool_held_during_stream", len(spools) == 2 and spools[-1].exists()
                            )
                            files = list(spools[-1].iterdir())
                            result.require(
                                "upstream_spooled_complete_before_abort",
                                len(files) == 1 and files[0].read_bytes() == media,
                            )
                            if adapter == "mcp":
                                result.require(
                                    "mcp_slot_held", _fileroutes._inflight_downloads == 1
                                )
                        await asyncio.wait_for(gate.disconnected.wait(), 1)
                        gate.release.set()
                        await asyncio.wait_for(gate.settled.wait(), 1)
                        await _until(lambda: not spools[-1].exists())
                        result.require("disconnect_finalizer_removed_spool", not spools[1].exists())
                        if adapter == "mcp":
                            result.require(
                                "mcp_slot_released", _fileroutes._inflight_downloads == 0
                            )
                        else:
                            limiter = app.state.notebooklm.limiters
                            result.require(
                                "rest_limiter_released",
                                limiter._download._value == limiter.download_limit,
                            )
                        recovered = await caller.request(url=route, **kwargs)
                        result.require(
                            "recovery_transfer_decoded",
                            recovered.status_code == 200 and recovered.content == media,
                        )
                        await _until(
                            lambda: len(spools) == 3 and all(not path.exists() for path in spools)
                        )
                    await _until(lambda: not listener.server_state.tasks)
                    result.require("listener_handlers_settled", not listener.server_state.tasks)
            finally:
                gate.release.set()
                result.record(
                    "adapter_cleanup",
                    spool_count=len(spools),
                    spools_removed=all(not p.exists() for p in spools),
                    transfer_clients_closed=all(c.is_closed for c in transports),
                    downstream_disconnected=gate.disconnected.is_set(),
                    response_settled=gate.settled.is_set(),
                )
            _require_client_closed(result, opened)
            result.require(
                "all_transfer_clients_closed",
                bool(transports) and all(c.is_closed for c in transports),
            )
            result.require(
                "exact_asset_fetches", sum(r.route == ASSET for r in upstream.journal) == 3
            )
            result.record(
                "adapter_outcome",
                adapter=adapter,
                prefix_bytes=len(prefix),
                content_bytes=len(media),
                content_sha256=hashlib.sha256(media).hexdigest(),
                baseline_status=baseline.status_code,
                recovery_status=recovered.status_code,
            )


async def chat_case(result: ScenarioResult) -> None:
    opened: list[Any] = []
    good = b")]}'" + _frame("Socket answer", final=True)

    def setup(server: HttpFaultServer) -> None:
        server.enqueue(
            GET_NOTEBOOK,
            *[
                Reply(body=rpc_response(GET_NOTEBOOK.rpc_id or "", [["Notebook", [], NOTEBOOK]]))
                for _ in range(2)
            ],
        )
        server.enqueue(TURNS, *[Reply(body=rpc_response(TURNS.rpc_id or "", [])) for _ in range(2)])
        server.enqueue(CHAT, Reply(body=good), Stall("headers", "ask-entered", Reply(body=good)))
        _probe(server)

    async with _fault_server(result, setup) as upstream:

        @asynccontextmanager
        async def factory():
            client = build_fault_client(upstream, timeout=2, server_error_max_retries=0)
            opened.append(client)
            async with client:
                yield client

        mcp = create_server(client_factory=factory)
        app = mcp.http_app(stateless_http=True, json_response=True)
        gate = DisconnectGate(app)
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
        }
        args = {
            "notebook": NOTEBOOK,
            "question": "Detached fixture question",
            "conversation_id": CONVERSATION,
        }

        def tool(name: str, arguments: dict[str, Any], identifier: int = 1) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }

        def payload(response: httpx.Response) -> dict[str, Any]:
            response.raise_for_status()
            return response.json()["result"]["structuredContent"]

        registry = None
        try:
            async with live_listener(gate) as (address, listener):
                async with httpx.AsyncClient(
                    base_url=address, timeout=2, headers=headers
                ) as caller:
                    baseline = payload(
                        await caller.post(
                            "/mcp", json=tool("chat_ask", {**args, "question": "Baseline question"})
                        )
                    )
                    result.require("valid_chat_baseline", baseline["answer"] == "Socket answer")
                    registry = mcp._lifespan_result.chat_tasks
                    result.require("finite_job_budget", registry._job_timeout == 3)
                    async with caller.stream(
                        "POST",
                        "/mcp",
                        headers={"x-fault-gate": "disconnect"},
                        json=tool("chat_start", args, 2),
                    ) as response:
                        prefix = await response.aiter_raw().__anext__()
                        await asyncio.wait_for(gate.prefix_sent.wait(), 1)
                        await upstream.wait_for_gate("ask-entered")
                        result.require(
                            "caller_received_start_prefix",
                            response.status_code == 200 and bool(prefix),
                        )
                        entries = list(registry._tasks.values())
                        result.require("one_task_accepted", len(entries) == 1)
                        entry = entries[0]
                        result.require(
                            "ask_running_at_response_boundary",
                            entry.started_at is not None
                            and entry.task is not None
                            and not entry.task.done(),
                        )
                    await asyncio.wait_for(gate.disconnected.wait(), 1)
                    gate.release.set()
                    await asyncio.wait_for(gate.settled.wait(), 1)
                    result.require(
                        "accepted_client_epoch_preserved",
                        entry.accepted_epoch == client_generation_epoch(opened[0]),
                    )
                    result.require(
                        "detached_ask_survives_disconnect",
                        not entry.task.done() and entry.error is None,
                    )
                    duplicate = payload(await caller.post("/mcp", json=tool("chat_start", args, 3)))
                    result.require(
                        "duplicate_attaches_to_same_job",
                        duplicate["status"] == "already_running"
                        and duplicate["task_id"] == entry.task_id,
                    )
                    status = payload(
                        await caller.post(
                            "/mcp", json=tool("chat_status", {"task_id": entry.task_id}, 4)
                        )
                    )
                    result.require(
                        "status_pending_after_disconnect",
                        status["status"] == "pending" and status["state"] == "generating",
                    )
                    upstream.release("ask-entered")
                    await asyncio.wait_for(asyncio.shield(entry.task), 1)
                    completed = payload(
                        await caller.post(
                            "/mcp", json=tool("chat_status", {"task_id": entry.task_id}, 5)
                        )
                    )
                    result.require(
                        "detached_result_decoded",
                        completed["status"] == "completed"
                        and completed["answer"] == "Socket answer",
                    )
                    recovery = payload(await caller.post("/mcp", json=tool("notebook_list", {}, 6)))
                    result.require(
                        "same_provider_recovery", recovery["notebooks"][0]["id"] == "recovered"
                    )
                    result.record(
                        "adapter_outcome",
                        adapter="mcp",
                        task_state=completed["status"],
                        accepted_epoch=entry.accepted_epoch,
                        task_count=len(registry._tasks),
                        duplicate_status=duplicate["status"],
                        downstream_prefix_bytes=len(prefix),
                    )
                await _until(lambda: not listener.server_state.tasks)
                result.require("listener_handlers_settled", not listener.server_state.tasks)
        finally:
            gate.release.set()
            upstream.release("ask-entered")
            if registry is not None:
                await asyncio.wait_for(registry.aclose(), 2)
            result.record(
                "adapter_cleanup",
                downstream_disconnected=gate.disconnected.is_set(),
                response_settled=gate.settled.is_set(),
                jobs_settled=registry is not None
                and all(e.task is None or e.task.done() for e in registry._tasks.values()),
            )
        _require_client_closed(result, opened)
        result.require("chat_never_replayed", sum(r.route == CHAT for r in upstream.journal) == 2)
        result.require("no_job_failure", entry.error is None and entry.task.done())


async def run(name: str, result: ScenarioResult | None = None) -> ScenarioResult:
    adapter = "rest" if "rest" in name else "mcp"
    if result is None:
        result = ScenarioResult("web", name, "isolated-lifecycle")
    chat = "chat_start" in name
    result.record(
        "plan",
        adapter=adapter,
        faults=["caller:disconnect"],
        gates=(
            [
                "valid-chat",
                "ask-entered",
                "downstream-prefix",
                "caller-disconnect",
                "response-settled",
                "ask-release",
            ]
            if chat
            else ["valid-transfer", "downstream-prefix", "caller-disconnect", "response-settled"]
        ),
        expected_dispatches={"chat": 2, "detached_jobs": 1}
        if chat
        else {"artifact_list": 3, "asset": 3},
        budgets={
            "rpc_timeout_s": 2 if chat else 1,
            "job_timeout_s": 3 if chat else None,
            "scenario_timeout_s": 6,
            "cleanup_timeout_s": 2,
        },
    )
    case = chat_case(result) if chat else download_case(result, adapter)
    loop = asyncio.get_running_loop()
    initial_tasks = asyncio.all_tasks()
    previous_handler = loop.get_exception_handler()
    unhandled: list[str] = []

    def capture_exception(_loop: Any, context: dict[str, Any]) -> None:
        unhandled.append(type(context.get("exception")).__name__)

    loop.set_exception_handler(capture_exception)
    # The parent requests orderly cancellation before its bounded kill fallback.
    # This signal belongs only to the dedicated worker process and event loop.
    owner = asyncio.current_task()
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, owner.cancel)
    try:
        await asyncio.wait_for(case, 6)
        await asyncio.sleep(0)
        remaining_tasks = asyncio.all_tasks() - initial_tasks
        result.record(
            "task_cleanup", pending_tasks=len(remaining_tasks), unhandled_exceptions=unhandled
        )
        result.require("no_pending_owned_tasks", not remaining_tasks)
        result.require("no_unhandled_task_exceptions", not unhandled)
    finally:
        if sys.platform != "win32":
            loop.remove_signal_handler(signal.SIGTERM)
        loop.set_exception_handler(previous_handler)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = ScenarioResult("web", args.scenario, "isolated-lifecycle")
    failure = False
    try:
        asyncio.run(run(args.scenario, result))
    except (Exception, asyncio.CancelledError) as exc:
        result.record("worker_failure", exception_type=type(exc).__name__)
        failure = True
    finally:
        args.report.write_text(
            json.dumps({"events": result.events, "checks": result.checks}), encoding="utf-8"
        )
    if failure:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
