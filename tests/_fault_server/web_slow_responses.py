"""Continuous progress against the public, explicitly scoped operation deadline.

Standalone chat_timeout and asset HTTPX timeouts are inactivity budgets, not a
promised total duration. These cases exercise CallSupervisor.operation instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from notebooklm import OperationTimeoutError

from .common import ScenarioResult
from .http import HttpFaultServer, Incremental, Reply
from .web import rpc_response
from .web_streaming import CHAT, CONVERSATION, TURNS, _frame
from .web_transfers import ASSET, LIST_ASSETS, MEDIA, NOTEBOOK, _audio_rows, _download

CADENCE = 0.1
INACTIVITY = 4.0
AGGREGATE = 2.0
WATCHDOG = 8.0
SCENARIOS = ("slow_download_operation_deadline", "slow_chat_operation_deadline")
_CHECKS = [
    "multiple_received_chunks",
    "progress_before_inactivity",
    "aggregate_deadline",
    "incomplete_at_deadline",
    "responses_closed",
    "peer_disconnect_settled",
    "server_incremental_evidence",
    "same_operation_recovery",
    "bounded_requests",
    "no_commits",
    "client_closed",
    "server_required_gates_observed",
    "server_plan_consumed",
    "server_had_no_errors",
    "server_handlers_drained",
]


async def run_scenario(
    name: str, *, operation_id: str, result: ScenarioResult | None = None
) -> ScenarioResult:
    from .web_scenarios import _cohort, _requests, _require_clean

    if name not in SCENARIOS:
        raise ValueError("unknown slow response scenario")
    if result is None:
        result = ScenarioResult("web", name, operation_id)
    elif (result.backend, result.scenario, result.operation_id) != ("web", name, operation_id):
        raise ValueError("scenario identity mismatch")
    download = name == SCENARIOS[0]
    result.record(
        "plan",
        faults=["bounded_incremental_response", "explicit_operation_deadline"],
        cohort_ids=[f"{operation_id}:0"],
        transport="httpx",
        entry_point="artifacts.download_audio" if download else "chat.ask",
        budgets={
            "cadence_s": CADENCE,
            "inactivity_s": INACTIVITY,
            "operation_timeout_s": AGGREGATE,
            "operation_watchdog_s": WATCHDOG,
            "cleanup_timeout_s": 2,
            "request_limit": 4,
            "commit_limit": 0,
        },
        required_checks=_CHECKS
        + (
            ["old_destination_preserved", "download_owners_settled"]
            if download
            else ["no_partial_answer"]
        ),
    )
    server = HttpFaultServer(hosts=["lh3.googleusercontent.com"])
    route = ASSET if download else CHAT
    body = MEDIA if download else b")]}'" + _frame("Complete answer " * 20, final=True)
    good = Reply(body=body, headers={"content-type": "audio/wav"} if download else {})
    # Both fixtures provide at least forty paced slices, exceeding twice the budget.
    server.enqueue(
        route, Incremental(good, chunk_bytes=max(1, len(body) // 60), interval=CADENCE), good
    )
    prerequisite = LIST_ASSETS if download else TURNS
    payload = [_audio_rows()] if download else []
    server.enqueue(
        prerequisite,
        *[Reply(body=rpc_response(prerequisite.rpc_id or "", payload)) for _ in range(2)],
    )
    observed: list[tuple[float, int]] = []
    responses: list[httpx.Response] = []
    original_factory = server.client_factory

    class ObservedStream(httpx.AsyncByteStream):
        def __init__(self, stream: httpx.AsyncByteStream) -> None:
            self.stream = stream

        async def __aiter__(self):
            async for chunk in self.stream:
                observed.append((time.monotonic(), len(chunk)))
                yield chunk

        async def aclose(self) -> None:
            await self.stream.aclose()

    async def observe(response: httpx.Response) -> None:
        if response.request.url.path == route.path:
            responses.append(response)
            if len(responses) == 1:
                response.stream = ObservedStream(response.stream)

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["timeout"] = httpx.Timeout(INACTIVITY)
        hooks = dict(kwargs.pop("event_hooks", {}))
        hooks["response"] = [*hooks.get("response", []), observe]
        return original_factory(event_hooks=hooks, **kwargs)

    server.client_factory = factory
    with tempfile.TemporaryDirectory(prefix="fault-slow-") as directory:
        destination = Path(directory) / "audio.wav"
        destination.write_bytes(b"old destination")
        async with _cohort(
            result, server, timeout=INACTIVITY, record_sleep=False, transfer_client_factory=factory
        ) as client:

            async def invoke() -> Any:
                if download:
                    return await _download(client, destination, batch=False)
                return await client.chat.ask(
                    NOTEBOOK, "Question", source_ids=[], conversation_id=CONVERSATION
                )

            async def scoped() -> Any:
                async with client.operation(timeout=AGGREGATE):
                    return await invoke()

            started = time.monotonic()
            error = None
            answer = None
            try:
                answer = await asyncio.wait_for(scoped(), WATCHDOG)
            except Exception as exc:
                error = exc
            elapsed = time.monotonic() - started
            times = [stamp for stamp, _ in observed]
            received = sum(size for _, size in observed)
            result.record(
                "slow_response",
                elapsed_s=elapsed,
                receive_offsets_s=[stamp - started for stamp in times],
                received_bytes=received,
                expected_bytes=len(body),
                expected_digest=hashlib.sha256(body).hexdigest(),
                error=None if error is None else type(error).__name__,
            )
            result.require("multiple_received_chunks", len(times) >= 4)
            result.require(
                "progress_before_inactivity",
                all(
                    later - earlier < INACTIVITY
                    for earlier, later in zip(
                        [started, *times], [*times, started + elapsed], strict=True
                    )
                ),
            )
            result.require(
                "aggregate_deadline",
                isinstance(error, OperationTimeoutError)
                and AGGREGATE * 0.8 <= elapsed < INACTIVITY,
            )
            result.require("incomplete_at_deadline", 0 < received < len(body))
            result.require("responses_closed", len(responses) == 1 and responses[0].is_closed)
            await server.wait_for_event("handler_settled", count=2)
            result.require("peer_disconnect_settled", server.active_handlers == 0)
            sent = [event for event in server.events if event["phase"] == "response_chunk"]
            result.require(
                "server_incremental_evidence",
                len(sent) >= 4 and received <= sent[-1]["response_bytes"] < len(body),
            )
            result.record("delivery", chunks=sent)
            if download:
                result.require(
                    "old_destination_preserved", destination.read_bytes() == b"old destination"
                )
                owner = client.artifacts._asset_downloads
                result.require(
                    "download_owners_settled",
                    not owner._clients
                    and not owner._tasks
                    and list(Path(directory).iterdir()) == [destination]
                    and not any(
                        t.name.startswith(f"artifact-dl-writer-{destination.name}")
                        for t in threading.enumerate()
                    ),
                )
            else:
                result.require("no_partial_answer", answer is None)
            recovered = await asyncio.wait_for(invoke(), WATCHDOG)
            result.require(
                "same_operation_recovery",
                destination.read_bytes() == body
                if download
                else recovered.answer == "Complete answer " * 20,
            )
            result.require(
                "bounded_requests", len(_requests(server, route)) == 2 and len(server.journal) == 4
            )
            result.require("no_commits", not server.committed)
        _require_clean(result, server)
    return result
