"""Mixed admission, transfer, and shared-poll Web socket scenarios."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from notebooklm import NotebookLMError, RPCError

from .common import ScenarioResult
from .http import HttpFaultServer, Reply, Route, Stall
from .web import homepage_response, list_response, rpc_response, rpc_status_response
from .web_transfers import ASSET, ASSET_URL, LIST_ASSETS, MEDIA, NOTEBOOK, READ

HOME = Route.homepage()


def _audio_row(artifact_id: str) -> list[Any]:
    return [
        artifact_id,
        f"Audio {artifact_id}",
        1,
        None,
        3,
        None,
        [None, None, None, None, None, [[ASSET_URL, None, "audio/wav"]]],
    ]


def _artifact_reply(*artifact_ids: str) -> Reply:
    return Reply(
        body=rpc_response(
            LIST_ASSETS.rpc_id or "",
            [[_audio_row(artifact_id) for artifact_id in artifact_ids]],
        )
    )


async def shared_poll_last_waiter_cancelled(result: ScenarioResult) -> None:
    from .web_scenarios import _cohort, _requests, _require_clean

    task_id = "poll-fault"
    server = HttpFaultServer()
    server.enqueue(
        LIST_ASSETS,
        Stall("headers", "poll-reply", _artifact_reply(task_id)),
    )
    server.enqueue(
        READ,
        Reply(body=list_response(READ.rpc_id or "", [("recovered", "Ready")])),
    )
    async with _cohort(result, server, record_sleep=False) as client:
        first = asyncio.create_task(
            client.artifacts.wait_for_completion(
                NOTEBOOK, task_id, initial_interval=0.01, max_interval=0.01, timeout=2.0
            )
        )
        await server.wait_for_requests(LIST_ASSETS, 1)
        second = asyncio.create_task(
            client.artifacts.wait_for_completion(
                NOTEBOOK, task_id, initial_interval=0.01, max_interval=0.01, timeout=2.0
            )
        )
        await asyncio.sleep(0)
        first.cancel()
        first_outcome = await asyncio.gather(first, return_exceptions=True)
        key = (NOTEBOOK, task_id)
        result.require(
            "poll_survives_first_cancel", client.artifacts._poll_registry.get(key) is not None
        )
        second.cancel()
        second_outcome = await asyncio.gather(second, return_exceptions=True)
        result.require(
            "poll_survives_last_cancel", client.artifacts._poll_registry.get(key) is not None
        )
        server.release("poll-reply")
        for _ in range(100):
            if client.artifacts._poll_registry.get(key) is None:
                break
            await asyncio.sleep(0)
        probe = await client.notebooks.list()
        result.require(
            "poll_both_callers_cancelled",
            isinstance(first_outcome[0], asyncio.CancelledError)
            and isinstance(second_outcome[0], asyncio.CancelledError),
        )
        result.require("poll_single_transport_request", len(_requests(server, LIST_ASSETS)) == 1)
        result.require("poll_registry_settled", client.artifacts._poll_registry.get(key) is None)
        result.require("poll_recovery", [row.id for row in probe] == ["recovered"])
    _require_clean(result, server)


async def shared_refresh_failure_then_recovery(result: ScenarioResult) -> None:
    from .web_scenarios import _cohort, _requests, _require_clean

    stale = Stall("headers", "stale-auth", Reply(body=rpc_status_response(READ.rpc_id or "", 16)))
    server = HttpFaultServer()
    server.enqueue(
        READ,
        stale,
        stale,
        Reply(body=rpc_status_response(READ.rpc_id or "", 16)),
        Reply(body=list_response(READ.rpc_id or "", [("recovered", "Ready")])),
    )
    server.enqueue(
        HOME,
        Stall("headers", "failed-refresh", Reply(body=b"<html>missing bootstrap</html>")),
        Reply(body=homepage_response()),
    )
    async with _cohort(result, server, record_sleep=False) as client:
        first = asyncio.create_task(client.notebooks.list())
        second = asyncio.create_task(client.notebooks.list())
        await server.wait_for_requests(READ, 2)
        server.release("stale-auth")
        await server.wait_for_requests(HOME, 1)
        await asyncio.sleep(0)
        server.release("failed-refresh")
        failures = await asyncio.gather(first, second, return_exceptions=True)
        first_wave_refreshes = len(_requests(server, HOME))
        recovered = await client.notebooks.list()
        result.require(
            "shared_refresh_failure_public_errors",
            len(failures) == 2 and all(isinstance(error, RPCError) for error in failures),
        )
        result.require("shared_refresh_failure_one_flight", first_wave_refreshes == 1)
        result.require("shared_refresh_failure_no_replay", len(_requests(server, READ)) == 4)
        result.require("shared_refresh_failure_retries_later", len(_requests(server, HOME)) == 2)
        result.require(
            "shared_refresh_failure_recovery", [row.id for row in recovered] == ["recovered"]
        )
    _require_clean(result, server)


async def mixed_rpc_transfer_poll_progress(result: ScenarioResult) -> None:
    from .web_scenarios import _cohort, _requests, _require_clean

    poll_id = "poll-fault"
    server = HttpFaultServer(hosts=["lh3.googleusercontent.com"])
    server.enqueue(
        READ,
        Stall(
            "headers",
            "rpc-holder",
            Reply(body=list_response(READ.rpc_id or "", [("held", "Held")])),
        ),
        Reply(body=list_response(READ.rpc_id or "", [("body-free", "Body free")])),
        Reply(body=list_response(READ.rpc_id or "", [("recovered", "Ready")])),
    )
    server.enqueue(
        LIST_ASSETS,
        _artifact_reply("audio-fault", poll_id),
        _artifact_reply("audio-fault", poll_id),
    )
    server.enqueue(
        ASSET,
        Stall(
            "body",
            "asset-body",
            Reply(body=MEDIA, headers={"content-type": "audio/wav"}),
            prefix=MEDIA[:20],
        ),
    )
    with tempfile.TemporaryDirectory(prefix="fault-mixed-") as directory:
        destination = Path(directory) / "audio.wav"
        async with _cohort(
            result,
            server,
            max_concurrent_rpcs=1,
            transfer_timeout=2.0,
            record_sleep=False,
        ) as client:
            holder = asyncio.create_task(client.notebooks.list())
            await server.wait_for_requests(READ, 1)
            download = asyncio.create_task(
                client.artifacts.download_audio(
                    NOTEBOOK, str(destination), artifact_id="audio-fault"
                )
            )
            poll = asyncio.create_task(
                client.artifacts.wait_for_completion(
                    NOTEBOOK,
                    poll_id,
                    initial_interval=0.01,
                    max_interval=0.01,
                    timeout=2.0,
                )
            )
            await asyncio.sleep(0)
            result.require("mixed_queued_before_release", not _requests(server, LIST_ASSETS))
            server.release("rpc-holder")
            await holder
            await server.wait_for_requests(LIST_ASSETS, 2)
            await server.wait_for_event("response_prefix")
            body_free = await client.notebooks.list()
            result.require(
                "mixed_body_outside_rpc_permit", [row.id for row in body_free] == ["body-free"]
            )
            result.require("mixed_download_still_gated", not download.done())
            server.release("asset-body")
            downloaded, polled = await asyncio.gather(download, poll)
            probe = await client.notebooks.list()
            result.require("mixed_download_integrity", Path(downloaded).read_bytes() == MEDIA)
            result.require("mixed_poll_complete", polled.is_complete)
            result.require("mixed_two_descriptor_rpcs", len(_requests(server, LIST_ASSETS)) == 2)
            result.require("mixed_recovery", [row.id for row in probe] == ["recovered"])
            result.require(
                "mixed_poll_registry_empty", not client.artifacts._poll_registry.active_tasks()
            )
            result.require(
                "mixed_transfer_clients_empty", not client.artifacts._asset_downloads._clients
            )
    _require_clean(result, server)


async def close_mixed_load_and_reopen(result: ScenarioResult) -> None:
    from .web_scenarios import _cohort, _requests, _require_clean

    poll_id = "poll-close"
    server = HttpFaultServer(hosts=["lh3.googleusercontent.com"])
    server.enqueue(
        LIST_ASSETS,
        _artifact_reply("audio-fault", poll_id),
    )
    server.enqueue(
        ASSET,
        Stall(
            "body",
            "asset-close",
            Reply(body=MEDIA, headers={"content-type": "audio/wav"}),
            prefix=MEDIA[:20],
        ),
    )
    server.enqueue(
        READ,
        Stall(
            "headers",
            "active-read",
            Reply(body=list_response(READ.rpc_id or "", [("held", "Held")])),
        ),
        Reply(body=list_response(READ.rpc_id or "", [("reopened", "Reopened")])),
    )
    with tempfile.TemporaryDirectory(prefix="fault-close-mixed-") as directory:
        destination = Path(directory) / "audio.wav"
        async with _cohort(
            result,
            server,
            max_concurrent_rpcs=1,
            transfer_timeout=2.0,
            record_sleep=False,
        ) as client:
            download = asyncio.create_task(
                client.artifacts.download_audio(
                    NOTEBOOK, str(destination), artifact_id="audio-fault"
                )
            )
            await server.wait_for_event("response_prefix")
            holder = asyncio.create_task(client.notebooks.list())
            await server.wait_for_requests(READ, 1)
            leader = asyncio.create_task(
                client.artifacts.wait_for_completion(
                    NOTEBOOK,
                    poll_id,
                    initial_interval=0.01,
                    max_interval=0.01,
                    timeout=2.0,
                )
            )
            key = (NOTEBOOK, poll_id)
            for _ in range(100):
                if client.artifacts._poll_registry.get(key) is not None:
                    break
                await asyncio.sleep(0)
            result.require(
                "close_mixed_poll_registered",
                client.artifacts._poll_registry.get(key) is not None,
            )
            follower = asyncio.create_task(
                client.artifacts.wait_for_completion(
                    NOTEBOOK,
                    poll_id,
                    initial_interval=0.01,
                    max_interval=0.01,
                    timeout=2.0,
                )
            )
            queued = asyncio.create_task(client.notebooks.list())
            await asyncio.sleep(0)
            result.require("close_mixed_read_queued", len(_requests(server, READ)) == 1)
            result.require("close_mixed_poll_rpc_queued", len(_requests(server, LIST_ASSETS)) == 1)
            await client.close(drain=False)
            outcomes = await asyncio.gather(
                download, holder, leader, follower, queued, return_exceptions=True
            )
            result.record("close_mixed_outcomes", types=[type(item).__name__ for item in outcomes])
            server.release("asset-close")
            server.release("active-read")
            await client.__aenter__()
            reopened = await client.notebooks.list()
            result.require(
                "close_mixed_tasks_terminated",
                all(
                    isinstance(outcome, (asyncio.CancelledError, NotebookLMError, RuntimeError))
                    for outcome in outcomes
                ),
            )
            result.require("close_mixed_no_queued_dispatch", len(_requests(server, READ)) == 2)
            result.require("close_mixed_no_poll_dispatch", len(_requests(server, LIST_ASSETS)) == 1)
            result.require("close_mixed_reopened", [row.id for row in reopened] == ["reopened"])
            result.require(
                "close_mixed_poll_registry_empty",
                not client.artifacts._poll_registry.active_tasks(),
            )
            result.require(
                "close_mixed_transfer_clients_empty", not client.artifacts._asset_downloads._clients
            )
    _require_clean(result, server)


IMPLEMENTATIONS: dict[str, Callable[[ScenarioResult], Awaitable[None]]] = {
    "close_mixed_load_and_reopen": close_mixed_load_and_reopen,
    "mixed_rpc_transfer_poll_progress": mixed_rpc_transfer_poll_progress,
    "shared_poll_last_waiter_cancelled": shared_poll_last_waiter_cancelled,
    "shared_refresh_failure_then_recovery": shared_refresh_failure_then_recovery,
}

PLANS = {
    "close_mixed_load_and_reopen": (
        (
            "asset-body:gated",
            "read:active@gate",
            "poll:leader+follower queued",
            "read:queued",
            "close+reopen",
        ),
        1,
    ),
    "mixed_rpc_transfer_poll_progress": (
        ("rpc:permit-held", "download+poll:queued", "asset-body:gated", "rpc:progress"),
        1,
    ),
    "shared_poll_last_waiter_cancelled": (
        ("poll:shared@gate", "waiter1:cancel", "waiter2:cancel", "leader:complete"),
        1,
    ),
    "shared_refresh_failure_then_recovery": (
        ("rpc:decoded-auth x2", "homepage:shared failure", "later refresh", "rpc:recovery"),
        1,
    ),
}

_CLEAN_CHECKS = (
    "client_closed",
    "server_required_gates_observed",
    "server_plan_consumed",
    "server_had_no_errors",
    "server_handlers_drained",
)

REQUIRED_CHECKS: dict[str, tuple[str, ...]] = {
    "close_mixed_load_and_reopen": (
        "close_mixed_poll_registered",
        "close_mixed_read_queued",
        "close_mixed_poll_rpc_queued",
        "close_mixed_tasks_terminated",
        "close_mixed_no_queued_dispatch",
        "close_mixed_no_poll_dispatch",
        "close_mixed_reopened",
        "close_mixed_poll_registry_empty",
        "close_mixed_transfer_clients_empty",
        *_CLEAN_CHECKS,
    ),
    "mixed_rpc_transfer_poll_progress": (
        "mixed_queued_before_release",
        "mixed_body_outside_rpc_permit",
        "mixed_download_still_gated",
        "mixed_download_integrity",
        "mixed_poll_complete",
        "mixed_two_descriptor_rpcs",
        "mixed_recovery",
        "mixed_poll_registry_empty",
        "mixed_transfer_clients_empty",
        *_CLEAN_CHECKS,
    ),
    "shared_poll_last_waiter_cancelled": (
        "poll_survives_first_cancel",
        "poll_survives_last_cancel",
        "poll_both_callers_cancelled",
        "poll_single_transport_request",
        "poll_registry_settled",
        "poll_recovery",
        *_CLEAN_CHECKS,
    ),
    "shared_refresh_failure_then_recovery": (
        "shared_refresh_failure_public_errors",
        "shared_refresh_failure_one_flight",
        "shared_refresh_failure_no_replay",
        "shared_refresh_failure_retries_later",
        "shared_refresh_failure_recovery",
        *_CLEAN_CHECKS,
    ),
}

BUDGETS: dict[str, dict[str, float | int | str]] = {
    "close_mixed_load_and_reopen": {
        "scenario_timeout_s": 8.0,
        "rpc_timeout_s": 0.5,
        "transfer_timeout_s": 2.0,
        "poll_timeout_s": 2.0,
        "cleanup_timeout_s": 2.0,
        "retry_clock": "real",
    },
    "mixed_rpc_transfer_poll_progress": {
        "scenario_timeout_s": 8.0,
        "rpc_timeout_s": 0.5,
        "transfer_timeout_s": 2.0,
        "poll_timeout_s": 2.0,
        "cleanup_timeout_s": 2.0,
        "retry_clock": "real",
    },
    "shared_poll_last_waiter_cancelled": {
        "scenario_timeout_s": 8.0,
        "rpc_timeout_s": 0.5,
        "poll_timeout_s": 2.0,
        "cleanup_timeout_s": 2.0,
        "retry_clock": "real",
    },
    "shared_refresh_failure_then_recovery": {
        "scenario_timeout_s": 8.0,
        "rpc_timeout_s": 0.5,
        "cleanup_timeout_s": 2.0,
        "retry_clock": "real",
    },
}

__all__ = ["BUDGETS", "IMPLEMENTATIONS", "PLANS", "REQUIRED_CHECKS"]
