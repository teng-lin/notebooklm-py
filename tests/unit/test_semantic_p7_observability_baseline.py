"""Pre-P7 equality baseline for semantic web metrics and telemetry.

The P0 ``metrics_contract`` freezes raw ``NotebookLMClient.rpc_call`` success,
transport-error, and decode-error branches.  P7 moves ownership below the
semantic backend, so this companion baseline drives representative semantic
calls through the production-composed web backend and real HTTP middleware
over an offline ``httpx.MockTransport``.

Regenerate intentionally with::

    uv run pytest tests/unit/test_semantic_p7_observability_baseline.py \
      --update-baselines -q

CI never passes ``--update-baselines``; it only compares the derived matrix to
the committed fixture.

The committed fixture remains the frozen pre-P7 artifact. P8 deliberately
removes the auth-snapshot lock from ordinary RPC request materialization:
those calls consume an immutable provider generation instead. The exact 12
affected cells are allocated below and normalized only for the final all-field
historical comparison. Auth refresh still records its coordinator lock, and
Chat still records its request-id lock.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest

import notebooklm.rpc as rpc_module
from notebooklm import correlation_id
from notebooklm._chat.workflow import ChatWorkflowService
from notebooklm._records import ChatAskInput
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import DecodingError
from notebooklm.rpc import RPCMethod
from notebooklm.types import ClientMetricsSnapshot, RpcTelemetryEvent

_BASELINE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "baselines"
    / "semantic_p7_observability.json"
)
_CORRELATION_ID = "semantic-p7-observability"
_CHAT_STREAM_LABEL = "CHAT_STREAM"
_LOCK_WAIT_FIELDS = frozenset({"lock_wait_seconds_total", "lock_wait_seconds_max"})
_P8_ORDINARY_RPC_LOCK_WAIT_CELLS = frozenset(
    {
        ("successful_read", "lock_wait_seconds_total"),
        ("successful_read", "lock_wait_seconds_max"),
        ("successful_mutation", "lock_wait_seconds_total"),
        ("successful_mutation", "lock_wait_seconds_max"),
        ("rate_limit_retry_then_success", "lock_wait_seconds_total"),
        ("rate_limit_retry_then_success", "lock_wait_seconds_max"),
        ("server_retry_then_success", "lock_wait_seconds_total"),
        ("server_retry_then_success", "lock_wait_seconds_max"),
        ("terminal_transport_error", "lock_wait_seconds_total"),
        ("terminal_transport_error", "lock_wait_seconds_max"),
        ("decode_error_after_transport_success", "lock_wait_seconds_total"),
        ("decode_error_after_transport_success", "lock_wait_seconds_max"),
    }
)


@dataclass(frozen=True, slots=True)
class _WireOutcome:
    label: str
    status_code: int | None = None
    headers: dict[str, str] | None = None
    content: bytes = b")]}'\n[]"
    error_type: type[httpx.RequestError] | None = None


class _AwaitedEventCollector:
    """Async callback whose completed count proves emission was awaited."""

    def __init__(self) -> None:
        self.started = 0
        self.completed = 0
        self.events: list[RpcTelemetryEvent] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, event: RpcTelemetryEvent) -> None:
        self.started += 1
        self.entered.set()
        await self.release.wait()
        self.events.append(event)
        self.completed += 1


def _auth(*, generation: str = "initial") -> AuthTokens:
    return AuthTokens(
        cookies={"SID": f"{generation}-sid"},
        csrf_token=f"{generation}-csrf",
        session_id=f"{generation}-session",
    )


def _chat_response_body() -> bytes:
    answer = "The deterministic semantic chat answer is long enough."
    inner = json.dumps([[answer, None, ["conversation-1", 12345], None, [1]]])
    chunk = json.dumps([["wrb.fr", None, inner]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n".encode()


def _request_label(request: httpx.Request) -> str:
    if "GenerateFreeFormStreamed" in str(request.url):
        return _CHAT_STREAM_LABEL
    rpc_id = parse_qs(request.url.query.decode()).get("rpcids", [None])[0]
    for method in RPCMethod:
        if method.value == rpc_id:
            return method.name
    return f"UNKNOWN_RPC:{rpc_id}"


def _float_population(value: float) -> str:
    if value == 0.0:
        return "zero-float"
    if value > 0.0:
        return "positive-float"
    return "negative-float"


def _normalized_snapshot(snapshot: ClientMetricsSnapshot) -> dict[str, int | str]:
    normalized: dict[str, int | str] = {}
    for field in dataclasses.fields(snapshot):
        value = getattr(snapshot, field.name)
        normalized[field.name] = _float_population(value) if isinstance(value, float) else value
    return normalized


def _normalized_events(events: list[RpcTelemetryEvent]) -> list[dict[str, object]]:
    return [
        {
            "method": event.method,
            "status": event.status,
            "elapsed_seconds": _float_population(event.elapsed_seconds),
            "request_id": event.request_id,
            "error_type": event.error_type,
        }
        for event in events
    ]


def _normalize_approved_p8_lock_wait_delta(observed: dict[str, object]) -> dict[str, object]:
    """Allocate only P8's 12 approved cells to the frozen pre-P7 population."""
    normalized = copy.deepcopy(observed)
    scenarios = normalized["scenarios"]
    assert isinstance(scenarios, dict)
    for scenario, field in _P8_ORDINARY_RPC_LOCK_WAIT_CELLS:
        metrics = scenarios[scenario]["metrics_snapshot"]
        assert metrics[field] == "zero-float"
        metrics[field] = "positive-float"
    return normalized


async def _run_scenario(
    *,
    outcomes: list[_WireOutcome],
    operation: Callable[[Any], Awaitable[object]],
    decode_error: bool = False,
    refresh_enabled: bool = False,
    retry_budget: int = 1,
    expected_events: int = 1,
) -> dict[str, object]:
    attempts: list[str] = []
    pending = list(outcomes)
    refresh_calls = 0
    collector = _AwaitedEventCollector()

    def handler(request: httpx.Request) -> httpx.Response:
        label = _request_label(request)
        attempts.append(label)
        if not pending:
            raise AssertionError(f"unexpected HTTP attempt: {label}")
        outcome = pending.pop(0)
        if label != outcome.label:
            raise AssertionError(f"expected {outcome.label} attempt, got {label}")
        if outcome.error_type is not None:
            raise outcome.error_type("offline terminal transport failure", request=request)
        assert outcome.status_code is not None
        return httpx.Response(
            outcome.status_code,
            headers=outcome.headers,
            content=outcome.content,
            request=request,
        )

    async_client_type = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return async_client_type(transport=httpx.MockTransport(handler), **kwargs)

    def decode_response(
        _raw: str,
        rpc_id: str,
        *,
        allow_null: bool = False,
        raise_on_null_status: bool = False,
    ) -> object:
        del allow_null, raise_on_null_status
        if decode_error:
            raise DecodingError("semantic observability decode drift", method_id=rpc_id)
        if rpc_id == RPCMethod.GET_CONVERSATION_TURNS.value:
            return [[]]
        return []

    async def refresh() -> AuthTokens:
        nonlocal refresh_calls
        refresh_calls += 1
        await asyncio.sleep(0)
        return _auth(generation="refreshed")

    async def no_sleep(_seconds: float) -> None:
        return None

    with (
        patch.object(rpc_module, "decode_response", decode_response),
        patch.object(httpx, "AsyncClient", client_factory),
        patch.object(asyncio, "sleep", no_sleep),
        patch.object(NotebookLMClient, "refresh_auth", lambda _client: refresh()),
    ):
        client = NotebookLMClient(
            _auth(),
            on_rpc_event=collector,
            rate_limit_max_retries=retry_budget,
            server_error_max_retries=retry_budget,
        )

    raised: str | None = None
    result: object = None
    call_blocked_while_callback_waited: bool | None = None
    async with client:
        with correlation_id(_CORRELATION_ID):
            operation_task = asyncio.create_task(operation(client))
            if expected_events:
                await asyncio.wait_for(collector.entered.wait(), timeout=2.0)
                await asyncio.sleep(0)
                call_blocked_while_callback_waited = not operation_task.done()
                collector.release.set()
            try:
                value = await operation_task
                if hasattr(value, "answer") and hasattr(value, "conversation_id"):
                    result = {
                        "answer": value.answer,
                        "conversation_id": value.conversation_id,
                    }
                else:
                    result = value
            except Exception as exc:  # noqa: BLE001 - the baseline records the public surface
                raised = type(exc).__qualname__
        snapshot = client.metrics_snapshot()

    assert not pending, f"scenario left unused wire outcomes: {pending!r}"
    assert collector.started == collector.completed == len(collector.events) == expected_events
    if expected_events:
        assert call_blocked_while_callback_waited is True
    return {
        "result": result,
        "raised": raised,
        "http_attempts": attempts,
        "refresh_calls": refresh_calls,
        "callback": {
            "started": collector.started,
            "completed_before_call_returned": collector.completed,
            "call_blocked_while_callback_waited": call_blocked_while_callback_waited,
        },
        "events": _normalized_events(collector.events),
        "metrics_snapshot": _normalized_snapshot(snapshot),
    }


class _NoSourceIds:
    """Source-id resolver the workflow never reaches: every ask supplies its own."""

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        raise AssertionError("chat.ask leaf sequencing must not resolve source ids")


async def _derive_observability_baseline() -> dict[str, object]:
    async def notebook_list(client: Any) -> object:
        return await client.notebooks.list()

    async def notebook_delete(client: Any) -> object:
        return await client.notebooks.delete("notebook-1")

    async def chat_stream(client: Any) -> object:
        return await ChatWorkflowService(client._backend, notebooks=_NoSourceIds()).ask(
            ChatAskInput(
                notebook_id="notebook-1",
                question="What is the answer?",
                source_ids=("source-1",),
                post_conversation_id="conversation-1",
                resolved_conversation_id="conversation-1",
            )
        )

    scenarios = {
        "successful_read": await _run_scenario(
            outcomes=[_WireOutcome(RPCMethod.LIST_NOTEBOOKS.name, status_code=200)],
            operation=notebook_list,
        ),
        "successful_mutation": await _run_scenario(
            outcomes=[_WireOutcome(RPCMethod.DELETE_NOTEBOOK.name, status_code=200)],
            operation=notebook_delete,
        ),
        "rate_limit_retry_then_success": await _run_scenario(
            outcomes=[
                _WireOutcome(
                    RPCMethod.LIST_NOTEBOOKS.name,
                    status_code=429,
                    headers={"Retry-After": "0"},
                ),
                _WireOutcome(RPCMethod.LIST_NOTEBOOKS.name, status_code=200),
            ],
            operation=notebook_list,
        ),
        "server_retry_then_success": await _run_scenario(
            outcomes=[
                _WireOutcome(RPCMethod.LIST_NOTEBOOKS.name, status_code=500),
                _WireOutcome(RPCMethod.LIST_NOTEBOOKS.name, status_code=200),
            ],
            operation=notebook_list,
        ),
        "auth_refresh_then_success": await _run_scenario(
            outcomes=[
                _WireOutcome(RPCMethod.LIST_NOTEBOOKS.name, status_code=401),
                _WireOutcome(RPCMethod.LIST_NOTEBOOKS.name, status_code=200),
            ],
            operation=notebook_list,
            refresh_enabled=True,
        ),
        "terminal_transport_error": await _run_scenario(
            outcomes=[
                _WireOutcome(
                    RPCMethod.LIST_NOTEBOOKS.name,
                    error_type=httpx.ConnectError,
                )
            ],
            operation=notebook_list,
            retry_budget=0,
        ),
        "decode_error_after_transport_success": await _run_scenario(
            outcomes=[_WireOutcome(RPCMethod.LIST_NOTEBOOKS.name, status_code=200)],
            operation=notebook_list,
            decode_error=True,
        ),
        "chat_stream_success": await _run_scenario(
            outcomes=[
                _WireOutcome(
                    _CHAT_STREAM_LABEL,
                    status_code=200,
                    content=_chat_response_body(),
                )
            ],
            operation=chat_stream,
            expected_events=0,
        ),
    }
    return {
        "schema_version": 1,
        "client_metrics_snapshot_fields": [
            field.name for field in dataclasses.fields(ClientMetricsSnapshot)
        ],
        "rpc_telemetry_event_fields": [
            field.name for field in dataclasses.fields(RpcTelemetryEvent)
        ],
        "scenarios": scenarios,
    }


@pytest.mark.asyncio
async def test_semantic_web_observability_matches_pre_p7_baseline(
    update_baselines: bool,
) -> None:
    observed = await _derive_observability_baseline()
    scenarios = observed["scenarios"]

    assert set(scenarios) == {
        "successful_read",
        "successful_mutation",
        "rate_limit_retry_then_success",
        "server_retry_then_success",
        "auth_refresh_then_success",
        "terminal_transport_error",
        "decode_error_after_transport_success",
        "chat_stream_success",
    }
    assert (
        scenarios["rate_limit_retry_then_success"]["metrics_snapshot"]["rpc_rate_limit_retries"]
        == 1
    )
    assert (
        scenarios["server_retry_then_success"]["metrics_snapshot"]["rpc_server_error_retries"] == 1
    )
    assert scenarios["auth_refresh_then_success"]["metrics_snapshot"]["rpc_auth_retries"] == 1
    assert scenarios["terminal_transport_error"]["events"][0]["status"] == "error"
    assert scenarios["decode_error_after_transport_success"]["events"][0]["status"] == "success"
    assert (
        scenarios["decode_error_after_transport_success"]["metrics_snapshot"]["rpc_decode_errors"]
        == 1
    )
    assert scenarios["chat_stream_success"]["events"] == []
    zero_lock_wait_cells = {
        (scenario, field)
        for scenario, result in scenarios.items()
        for field in _LOCK_WAIT_FIELDS
        if result["metrics_snapshot"][field] == "zero-float"
    }
    assert zero_lock_wait_cells == _P8_ORDINARY_RPC_LOCK_WAIT_CELLS
    for scenario in ("auth_refresh_then_success", "chat_stream_success"):
        metrics = scenarios[scenario]["metrics_snapshot"]
        assert metrics["lock_wait_seconds_total"] == "positive-float"
        assert metrics["lock_wait_seconds_max"] == "positive-float"

    historical_comparison = _normalize_approved_p8_lock_wait_delta(observed)
    if update_baselines:
        _BASELINE_PATH.write_text(
            json.dumps(historical_comparison, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    expected = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    assert historical_comparison == expected
