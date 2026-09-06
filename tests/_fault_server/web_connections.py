"""Public read recovery with established HTTP connection reuse and slow peers."""

from __future__ import annotations

import asyncio
from functools import partial

from .common import ScenarioResult
from .http import HttpFaultServer, Reply, Transfer
from .web import list_response
from .web_transfers import READ, upload_case


def _read_reply() -> Reply:
    return Reply(body=list_response(READ.rpc_id or "", [("connection-read", "Read")]))


async def connection_case(result: ScenarioResult, *, restart: bool) -> None:
    from .web_scenarios import _cohort, _requests, _require_clean

    server = HttpFaultServer(keep_alive=True)
    server.enqueue(READ, _read_reply(), _read_reply(), _read_reply())
    async with _cohort(result, server, timeout=0.5, record_sleep=False) as client:
        for _ in range(2):
            notebooks = await client.notebooks.list()
            result.require(
                f"read_{len(server.journal)}_decoded", notebooks[0].id == "connection-read"
            )
        requests = _requests(server, READ)
        original_connection = requests[0].connection_id
        result.require("actual_connection_reuse", requests[1].connection_id == original_connection)
        address = server.address
        if restart:
            await server.aclose()
            # Keep the captured physical endpoint unchanged. Rebuilding the
            # client/factory would not prove its old pool can recover.
            server._server = await asyncio.start_server(server._accept, *address)
        else:
            writers = tuple(server._writers)
            result.require("one_reused_peer_connection", len(writers) == 1)
            writers[0].close()
            await asyncio.wait_for(writers[0].wait_closed(), 2)
            await server.wait_for_event("handler_settled")
        recovered = await client.notebooks.list()
        result.require("same_client_recovered", recovered[0].id == "connection-read")
        result.require(
            "new_connection_after_peer_loss",
            _requests(server, READ)[-1].connection_id != original_connection,
        )
        result.record(
            "connection_trace",
            connection_ids=[r.connection_id for r in _requests(server, READ)],
            listener_restarted=restart,
        )
    result.require("exact_read_dispatches", len(_requests(server, READ)) == 3)
    result.require("no_mutation_commits", not server.committed)
    _require_clean(result, server)


async def slow_read_case(result: ScenarioResult) -> None:
    from .web_scenarios import _cohort, _requests, _require_clean

    server = HttpFaultServer(keep_alive=True)
    server.enqueue(
        READ,
        _read_reply(),
        Transfer(response=_read_reply(), prefix_bytes=1, gates={"body_prefix": "consume-read"}),
        _read_reply(),
    )
    async with _cohort(result, server, record_sleep=False) as client:
        baseline = await client.notebooks.list()
        result.require("slow_consumer_baseline", baseline[0].id == "connection-read")
        task = asyncio.create_task(client.notebooks.list())
        await server.wait_for_gate("consume-read")
        result.require("read_waits_for_request_consumer", not task.done())
        result.require("request_prefix_observed", _requests(server, READ)[1].body_bytes > 0)
        server.release("consume-read")
        response = await task
        result.require("read_progress_after_release", response[0].id == "connection-read")
        result.require("body_consumption_completed", _requests(server, READ)[1].body_complete)
        probe = await client.notebooks.list()
        result.require("same_client_recovery", probe[0].id == "connection-read")
    result.record("transfer_trace", phases=server.events)
    result.require("bounded_read_requests", len(_requests(server, READ)) == 3)
    _require_clean(result, server)


IMPLEMENTATIONS = {
    "connection_peer_close": partial(connection_case, restart=False),
    "connection_server_restart": partial(connection_case, restart=True),
    "connection_slow_read_consumer": slow_read_case,
    "connection_slow_upload_consumer": partial(upload_case, variant="body_stall"),
}
PLANS = {
    name: (("connection:successful-baseline", name, "same-client:recovery"), 1)
    for name in IMPLEMENTATIONS
}
