"""Adversarial framing probes through production HTTPX and Web chat decoding."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
from typing import Any

import httpx

from notebooklm import NetworkError, RPCResponseTooLargeError

from .common import ScenarioResult
from .http import HttpFaultServer, Incremental, Reply
from .web import rpc_response
from .web_streaming import CHAT, CONVERSATION, TURNS, _frame
from .web_transfers import NOTEBOOK

VARIANTS = (
    "cap_below",
    "cap_at",
    "cap_above",
    "gzip_valid",
    "gzip_corrupt",
    "gzip_cap_above",
    "invalid_length",
    "large_fragmented_frame",
)
SCENARIOS = tuple(f"protocol_chat_{variant}" for variant in VARIANTS)
CHECKS = [
    "public_outcome",
    "no_partial_success",
    "response_settled",
    "bounded_consumption",
    "same_client_recovery",
    "exact_dispatches",
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
    from .web_scenarios import _cohort, _require_clean

    if name not in SCENARIOS:
        raise ValueError("unknown Web protocol scenario")
    result = result or ScenarioResult("web", name, operation_id)
    if (result.backend, result.scenario, result.operation_id) != ("web", name, operation_id):
        raise ValueError("protocol scenario identity mismatch")
    variant = name.removeprefix("protocol_chat_")
    answer = "Large 世界 answer " * (8192 if variant == "large_fragmented_frame" else 8)
    body = b")]}'" + _frame(answer, final=True)
    healthy = b")]}'" + _frame("Recovered", final=True)
    limit = len(body) + 1 if variant == "cap_below" else len(body)
    if variant in {"cap_above", "gzip_cap_above"}:
        limit -= 1
    expected_failure = variant in {"cap_above", "gzip_cap_above", "gzip_corrupt", "invalid_length"}
    wire = gzip.compress(body, mtime=0) if variant.startswith("gzip") else body
    if variant == "gzip_corrupt":
        # Real gzip member with a corrupt CRC, rather than a merely incomplete trailer.
        wire = wire[:-8] + bytes([wire[-8] ^ 0xFF]) + wire[-7:]
    headers = {"content-encoding": "gzip"} if variant.startswith("gzip") else {}
    if variant == "invalid_length":
        headers["content-length"] = "-1"
    checks = CHECKS + (["fragment_progress"] if variant == "large_fragmented_frame" else [])
    result.record(
        "plan",
        required_checks=checks,
        faults=[variant],
        transport="httpx",
        cohort_ids=[f"{operation_id}:0"],
        entry_point="chat.ask",
        budgets={
            "response_cap_bytes": limit,
            "fixture_wire_bytes": len(wire),
            "watchdog_s": 8,
            "cleanup_timeout_s": 2,
            "request_limit": 4,
            "commit_limit": 0,
        },
    )
    server = HttpFaultServer()
    reply = Reply(body=wire, headers=headers)
    action = (
        Incremental(reply, chunk_bytes=4096, interval=0.01)
        if variant == "large_fragmented_frame"
        else reply
    )
    server.enqueue(CHAT, action, Reply(body=healthy))
    server.enqueue(TURNS, *[Reply(body=rpc_response(TURNS.rpc_id or "", [])) for _ in range(2)])
    responses: list[httpx.Response] = []
    chunks: list[int] = []
    factory = server.client_factory

    class ObservedStream(httpx.AsyncByteStream):
        def __init__(self, stream: httpx.AsyncByteStream):
            self.stream = stream

        async def __aiter__(self):
            async for chunk in self.stream:
                chunks.append(len(chunk))
                yield chunk

        async def aclose(self):
            await self.stream.aclose()

    async def observe(response: httpx.Response):
        if response.request.url.path == CHAT.path:
            responses.append(response)
            if len(responses) == 1:
                response.stream = ObservedStream(response.stream)

    def observed_factory(**kwargs: Any):
        hooks = dict(kwargs.pop("event_hooks", {}))
        hooks["response"] = [*hooks.get("response", []), observe]
        return factory(event_hooks=hooks, **kwargs)

    server.client_factory = observed_factory
    async with _cohort(result, server, timeout=3, record_sleep=False) as client:
        # Supported per-instance chat cap; retain production transport and decoder.
        client.chat._chat_response_max_bytes = limit

        async def ask():
            return await client.chat.ask(
                NOTEBOOK, "Question", source_ids=[], conversation_id=CONVERSATION
            )

        error = None
        returned = None
        try:
            returned = await asyncio.wait_for(ask(), 8)
        except Exception as exc:
            error = exc
        result.record(
            "protocol_outcome",
            error=None if error is None else type(error).__name__,
            wire_bytes_observed=sum(chunks),
            expected_wire_bytes=len(wire),
            decoded_bytes=len(body),
            wire_digest=hashlib.sha256(wire).hexdigest(),
            limit_bytes=limit,
            error_bytes_read=getattr(error, "bytes_read", None),
        )
        if variant in {"cap_above", "gzip_cap_above"}:
            valid_outcome = (
                isinstance(error, RPCResponseTooLargeError) and error.limit_bytes == limit
            )
        elif expected_failure:
            valid_outcome = isinstance(error, NetworkError)
        else:
            valid_outcome = error is None and returned.answer == answer
        result.require("public_outcome", valid_outcome)
        result.require(
            "no_partial_success",
            returned is None if expected_failure else returned.answer == answer,
        )
        # Size enforcement is post-chunk: one decoded chunk of overshoot is permitted.
        result.require(
            "bounded_consumption",
            sum(chunks) <= len(wire)
            and (
                error.bytes_read == len(body)
                if isinstance(error, RPCResponseTooLargeError)
                else True
            ),
        )
        result.require(
            "response_settled",
            (len(responses) == (0 if variant == "invalid_length" else 1))
            and all(response.is_closed for response in responses),
        )
        if variant == "large_fragmented_frame":
            result.require(
                "fragment_progress",
                len(chunks) >= 2
                and sum(chunks) == len(body)
                and sum(event["phase"] == "response_chunk" for event in server.events) >= 2,
            )
        recovered = await asyncio.wait_for(ask(), 8)
        result.require("same_client_recovery", recovered.answer == "Recovered")
        result.require(
            "exact_dispatches",
            len(server.journal) == 4
            and sum(record.route == CHAT for record in server.journal) == 2,
        )
        result.require("no_commits", not server.committed)
    _require_clean(result, server)
    return result
