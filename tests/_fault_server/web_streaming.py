"""Web chat socket frames based on unit/test_streaming_chat_wire.py."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from urllib.parse import urlsplit

import httpx

from notebooklm import NetworkError
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import get_query_url

from .common import ScenarioResult
from .http import HttpFaultServer, Reply, Route, Stall, Truncate
from .web import rpc_response
from .web_transfers import NOTEBOOK, _probe, _recover, _transfer_trace

CHAT = Route("POST", "notebook.google.com", urlsplit(get_query_url()).path)
TURNS = Route.rpc(RPCMethod.GET_CONVERSATION_TURNS.value)
CONVERSATION = "00000000-0000-4000-8000-000000000300"


def _frame(text: str, *, final: bool) -> bytes:
    answer = [text, None, [CONVERSATION, "reply-id", 123], None, [[], None, None, [], 1]]
    inner = json.dumps([answer, None, None, None, final], ensure_ascii=False)
    chunk = json.dumps([["wrb.fr", None, inner]], ensure_ascii=False)
    return f"\n{len(chunk)}\n{chunk}\n".encode()


async def chat_case(result: ScenarioResult, variant: str) -> None:
    from .web_scenarios import _cohort, _requests, _require_clean

    server = HttpFaultServer()
    answer_text = "Socket answer café 世界"
    good = b")]}'" + _frame(answer_text, final=True)
    partial_answer = b")]}'" + _frame("Partial answer café 世界", final=False)
    complete = partial_answer + _frame(answer_text, final=True)
    cut = partial_answer.index("世".encode()) + 1
    received_prefix = asyncio.Event()
    observed_chunks: list[bytes] = []
    original_factory = server.client_factory

    class ObservedStream(httpx.AsyncByteStream):
        def __init__(self, stream: httpx.AsyncByteStream) -> None:
            self.stream = stream

        async def __aiter__(self):
            async for chunk in self.stream:
                observed_chunks.append(chunk)
                if sum(map(len, observed_chunks)) >= cut:
                    received_prefix.set()
                yield chunk

        async def aclose(self) -> None:
            await self.stream.aclose()

    async def observe_response(response: httpx.Response) -> None:
        if response.request.url.path == CHAT.path and len(server.journal) >= 4:
            response.stream = ObservedStream(response.stream)

    def factory(**kwargs):
        hooks = dict(kwargs.pop("event_hooks", {}))
        hooks["response"] = [*hooks.get("response", []), observe_response]
        return original_factory(event_hooks=hooks, **kwargs)

    server.client_factory = factory
    server.enqueue(TURNS, *[Reply(body=rpc_response(TURNS.rpc_id or "", [])) for _ in range(2)])
    server.enqueue(CHAT, Reply(body=good))
    if variant == "multibyte_fragmented_success":
        server.enqueue(
            CHAT, Stall("body", "held-chat", Reply(body=complete), prefix=complete[:cut])
        )
    elif variant == "success":
        server.enqueue(CHAT, Reply(body=complete))
    elif variant in {"partial_frame_disconnect", "partial_answer_disconnect"}:
        prefix = partial_answer[:cut] if variant == "partial_frame_disconnect" else partial_answer
        server.enqueue(CHAT, Truncate(prefix, len(complete)))
    else:
        server.enqueue(
            CHAT,
            Stall(
                "body",
                "held-chat",
                Reply(body=complete),
                prefix=partial_answer[:cut] if variant == "partial_frame_stall" else partial_answer,
            ),
        )
    _probe(server)
    error: BaseException | None = None
    async with _cohort(result, server, timeout=0.2, record_sleep=False) as client:
        baseline = await client.chat.ask(
            NOTEBOOK, "Fixture question", source_ids=[], conversation_id=CONVERSATION
        )
        result.require("valid_chat_baseline", baseline.answer == answer_text)
        task = asyncio.create_task(
            client.chat.ask(
                NOTEBOOK, "Next fixture question", source_ids=[], conversation_id=CONVERSATION
            )
        )
        if variant == "multibyte_fragmented_success":
            await server.wait_for_gate("held-chat")
            await asyncio.wait_for(received_prefix.wait(), 1)
            result.require("multibyte_prefix_received", b"".join(observed_chunks) == complete[:cut])
            result.require("partial_codepoint_waits_for_continuation", not task.done())
            server.release("held-chat")
        if variant == "partial_answer_cancel":
            await server.wait_for_gate("held-chat")
            task.cancel()
        answer = None
        try:
            answer = await task
        except BaseException as exc:
            error = exc
        if variant in {"success", "multibyte_fragmented_success"}:
            result.require("terminal_frame_decoded", error is None and answer.answer == answer_text)
        elif variant == "partial_answer_cancel":
            result.require("chat_cancel_propagated", isinstance(error, asyncio.CancelledError))
        else:
            result.require("chat_transport_failure", isinstance(error, NetworkError))
            result.require("chat_outcome_metadata", error.operation_metadata is not None)
        result.record("outcome", error=None if error is None else type(error).__name__)
        result.require(
            "no_partial_public_answer",
            answer is None or variant in {"success", "multibyte_fragmented_success"},
        )
        if variant == "multibyte_fragmented_success":
            result.require("fragmented_bytes_reassembled", b"".join(observed_chunks) == complete)
            result.record(
                "chat_fragmentation",
                chunk_bytes=[len(chunk) for chunk in observed_chunks],
                split_inside_utf8=True,
            )
        await _recover(result, client)
        server.release("held-chat")
    result.require("chat_never_replayed", len(_requests(server, CHAT)) == 2)
    _transfer_trace(result, server)
    _require_clean(result, server)


VARIANTS = (
    "success",
    "multibyte_fragmented_success",
    "partial_frame_disconnect",
    "partial_frame_stall",
    "partial_answer_disconnect",
    "partial_answer_stall",
    "partial_answer_cancel",
)
IMPLEMENTATIONS = {f"chat_{case}": partial(chat_case, variant=case) for case in VARIANTS}
PLANS = {
    name: (("chat:success-baseline", name, "same-client:recovery"), 1) for name in IMPLEMENTATIONS
}
